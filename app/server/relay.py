#!/usr/bin/env python3
"""fn-cockpit relay: fnOS gateway -> app.sock -> [this relay] -> cockpit.sock -> cockpit-ws.

The fnOS app gateway strips the /app/<appname> prefix before forwarding to
app.sock; cockpit (UrlRoot=/app/<appname>) requires the prefix on the wire.
This relay re-adds the prefix and forwards over a second unix socket.

Handles:
  - plain HTTP with keep-alive, Content-Length and chunked bodies
  - WebSocket upgrade (rewrites request line, then blind bidirectional tunnel)
Stdlib only. Logs to TRIM_PKGVAR/relay.log
"""
import os
import socket
import selectors
import sys
import threading
import traceback

import ssl

_UNIX = getattr(socket, 'AF_UNIX', None)

APPDEST = os.environ.get("TRIM_APPDEST") or "/var/apps/fn-cockpit/target"
APPNAME = os.environ.get("TRIM_APPNAME") or "fn-cockpit"
PKGVAR = os.environ.get("TRIM_PKGVAR") or os.path.join(os.path.dirname(APPDEST.rstrip("/")), "var")
# Test hook: RELAY_TCP_PORT uses loopback TCP instead of AF_UNIX (Windows test rigs).
_TCP_PORT = os.environ.get("RELAY_TCP_PORT")
if _TCP_PORT:
    LISTEN = ("127.0.0.1", int(_TCP_PORT))
    BACKEND = ("127.0.0.1", int(_TCP_PORT) + 1)
    SOCKFAM = socket.AF_INET
else:
    LISTEN = os.path.join(APPDEST, "app.sock")
    BACKEND = os.path.join(APPDEST, "cockpit.sock")
    SOCKFAM = _UNIX
PREFIX = ("/app/" + APPNAME).encode()

# v0.1.5: cockpit.service on fnOS runs cockpit-tls (TLS terminator). The unix socket
# only speaks TLS; plain HTTP gets the connection dropped. Wrap the backend in TLS.
TLS_CTX = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
TLS_CTX.check_hostname = False
TLS_CTX.verify_mode = ssl.CERT_NONE

# v0.1.8: other cockpit builds may run cockpit-ws in plain-HTTP mode on the unix
# socket. Probe TLS first; on ssl.SSLError retry plain. Remember what worked to
# avoid a double handshake per request, re-probing if the cached mode breaks.
_BACKEND_TLS = None  # None=unknown, True=TLS, False=plain


def connect_backend():
    """Connect to BACKEND, return a ready socket (TLS-wrapped or plain)."""
    global _BACKEND_TLS
    if _TCP_PORT:
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(60)
        raw.connect(BACKEND)
        return raw  # test rig: plain TCP
    modes = [True] if _BACKEND_TLS is True else \
            [False] if _BACKEND_TLS is False else [True, False]
    last_exc = None
    for use_tls in modes:
        raw = None
        try:
            raw = socket.socket(SOCKFAM, socket.SOCK_STREAM)
            raw.settimeout(60)
            raw.connect(BACKEND)
            if use_tls:
                sock = TLS_CTX.wrap_socket(raw, server_hostname="cockpit-local")
                _BACKEND_TLS = True
                return sock
            _BACKEND_TLS = False
            return raw
        except ssl.SSLError as e:
            last_exc = e
            if raw is not None:
                try:
                    raw.close()
                except Exception:
                    pass
            continue
        except OSError:
            if raw is not None:
                try:
                    raw.close()
                except Exception:
                    pass
            raise  # backend down: mode probing won't help
    raise last_exc

try:
    LOG = open(os.path.join(PKGVAR, "relay.log"), "a", buffering=1)
except Exception:
    LOG = sys.stderr


def log(*a):
    import time
    print(time.strftime("[%F %T]"), *a, file=LOG, flush=True)


def recv_headers(conn, bufsize=65536):
    """Read from socket until CRLFCRLF. Returns (head_bytes, leftover_bytes)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = conn.recv(bufsize)
        if not chunk:
            return None, b""
        buf += chunk
        if len(buf) > 1048576:
            raise ValueError("headers too large")
    head, leftover = buf.split(b"\r\n\r\n", 1)
    return head, leftover


def parse_head(head):
    lines = head.split(b"\r\n")
    reqline = lines[0]
    headers = {}
    for ln in lines[1:]:
        if b":" in ln:
            k, v = ln.split(b":", 1)
            headers[k.strip().lower()] = v.strip()
    parts = reqline.split(b" ")
    return parts, headers


def read_body(conn, headers, leftover):
    """Read a request body per headers. Returns (body_bytes, leftover_after_body)."""
    te = headers.get(b"transfer-encoding", b"").lower()
    if b"chunked" in te:
        buf = leftover
        body = b""
        while True:
            while b"\r\n" not in buf:
                buf += conn.recv(65536)
            size_line, buf = buf.split(b"\r\n", 1)
            size = int(size_line.split(b";")[0], 16)
            if size == 0:
                # trailing CRLF (and possible trailers)
                while not buf.endswith(b"\r\n"):
                    buf += conn.recv(65536)
                return body, b""
            while len(buf) < size + 2:
                buf += conn.recv(65536)
            body += buf[:size]
            buf = buf[size + 2:]  # skip chunk data + CRLF
    cl = headers.get(b"content-length")
    if cl:
        n = int(cl)
        buf = leftover
        while len(buf) < n:
            buf += conn.recv(65536)
        return buf[:n], buf[n:]
    return b"", leftover


def safe_backend_path(path):
    """Map a gateway-stripped path to the path cockpit should see.
    Root/empty must gain a trailing slash (cockpit UrlRoot assertion), other
    absolute paths get the prefix re-added."""
    if path == b"" or path == b"/":
        return PREFIX + b"/"
    if path.startswith(b"/"):
        return PREFIX + path
    return path


def rewrite_origin_lines(head_lines, headers):
    """v0.1.7: cockpit compares the Origin header against the Host header it
    receives. The fnOS gateway may rewrite Host, so Origin (the browser's)
    fails the check -> "received request from bad Origin" -> WS handshake
    rejected after login. Rewrite Origin to https://<Host> so the check
    always passes; the real access control is the gateway's session check."""
    host = headers.get(b"host")
    if not host or not any(k == b"origin" for k in headers):
        return head_lines
    new_origin = b"Origin: https://" + host
    for i, ln in enumerate(head_lines):
        if ln[:7].lower() == b"origin:":
            head_lines[i] = new_origin
    return head_lines


def rebuild_and_send(backend, reqline_parts, headers, head_raw, body_bytes=b""):
    """Send rewritten request (headers + body) to backend."""
    parts = list(reqline_parts)
    # parts: [METHOD, PATH, VERSION]  (http.client style 3-token line)
    if len(parts) >= 2:
        path = parts[1]
        if not path.startswith(PREFIX):
            # v0.1.5: root path must gain a trailing slash. cockpit strips UrlRoot
            # (= /app/<appname>) from the path; stripping "/" leaves "" which hits
            # an assertion in cockpitwebserver.c and ABORTs cockpit-ws.
            parts[1] = safe_backend_path(path)
    head_lines = head_raw.split(b"\r\n")
    head_lines[0] = b" ".join(parts)
    head_lines = rewrite_origin_lines(head_lines, headers)

    has_xfp = any(k == b"x-forwarded-proto" for k in headers)
    if not has_xfp:
        head_lines.append(b"X-Forwarded-Proto: https")
    head_lines.append(b"Connection: close")
    out = b"\r\n".join(head_lines) + b"\r\n\r\n"
    if body_bytes:
        out += body_bytes
    backend.sendall(out)
    return None


def pump(src, dst, extra=b""):
    """Blind bidirectional copy between two sockets until EOF on either."""
    if extra:
        dst.sendall(extra)
    sel = selectors.DefaultSelector()
    sel.register(src, selectors.EVENT_READ)
    sel.register(dst, selectors.EVENT_READ)
    try:
        while True:
            for key, _ in sel.select(timeout=120):
                s = key.fileobj
                try:
                    data = s.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                other = dst if s is src else src
                try:
                    other.sendall(data)
                except OSError:
                    return
    finally:
        sel.close()


def handle(conn):
    """Handle one client connection (keep-alive loop for plain HTTP)."""
    try:
        while True:
            head, leftover = recv_headers(conn)
            if head is None:
                return
            parts, headers = parse_head(head)
            method = parts[0].upper() if parts else b"GET"
            path = parts[1] if len(parts) > 1 else b"/"
            is_ws = (headers.get(b"upgrade", b"").lower() == b"websocket")
            try:
                backend = connect_backend()
            except OSError as e:
                log("backend connect failed:", e)
                try:
                    conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                except OSError:
                    pass
                return

            if is_ws:
                log("WS tunnel", path.decode(errors="replace"))
                # rewrite request line + Origin, forward, then blind tunnel
                lines = head.split(b"\r\n")
                p = list(parts)
                if len(p) >= 2 and not p[1].startswith(PREFIX):
                    p[1] = safe_backend_path(p[1])
                lines[0] = b" ".join(p)
                lines = rewrite_origin_lines(lines, headers)
                if not any(k == b"x-forwarded-proto" for k in headers):
                    lines.append(b"X-Forwarded-Proto: https")
                try:
                    backend.sendall(b"\r\n".join(lines) + b"\r\n\r\n" + leftover)
                    conn.settimeout(None)
                    backend.settimeout(None)
                    pump(conn, backend)
                except Exception:
                    log("ws tunnel error:\n", traceback.format_exc())
                finally:
                    backend.close()
                    return
            else:
                # plain http: consume body, forward, stream response back
                try:
                    body_bytes = b""
                    if method in (b"POST", b"PUT", b"PATCH", b"DELETE"):
                        body_bytes, leftover = read_body(conn, headers, leftover)
                    rebuild_and_send(backend, parts, headers, head, body_bytes)
                    # stream response
                    conn.settimeout(120)
                    first = True
                    while True:
                        data = backend.recv(65536)
                        if not data:
                            break
                        conn.sendall(data)
                        first = False
                except Exception:
                    log("http relay error:\n", traceback.format_exc())
                    try:
                        conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    except OSError:
                        pass
                    return
                finally:
                    backend.close()
                # continue keep-alive loop
    except Exception:
        log("connection error:\n", traceback.format_exc())
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main():
    if SOCKFAM == _UNIX and os.path.exists(LISTEN):
        os.unlink(LISTEN)
    srv = socket.socket(SOCKFAM, socket.SOCK_STREAM)
    srv.bind(LISTEN)
    if SOCKFAM == _UNIX:
        os.chmod(LISTEN, 0o666)
    srv.listen(64)
    log(f"relay listening on {LISTEN} -> {BACKEND} (prefix {PREFIX.decode()})")
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
