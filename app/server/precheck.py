#!/usr/bin/env python3
"""fn-cockpit precheck: direct request to cockpit.sock (bypassing relay/gateway).
v0.1.5: request path must keep a trailing slash after UrlRoot stripping (empty
path aborts cockpit-ws).
v0.1.8: cockpit.socket speaks TLS on Debian 12 (cockpit-tls) but may speak plain
HTTP on other cockpit builds; probe TLS first, fall back to plain HTTP.
Prints OK:<status line> if cockpit-ws answers, FAIL:<reason> otherwise."""
import os
import ssl
import socket

APPDEST = os.environ.get("TRIM_APPDEST") or "/var/apps/fn-cockpit/target"
APPNAME = os.environ.get("TRIM_APPNAME") or "fn-cockpit"
sock = os.path.join(APPDEST, "cockpit.sock")
REQ = "GET /app/%s/ HTTP/1.1\r\nHost: precheck\r\nConnection: close\r\n\r\n" % APPNAME


def probe(use_tls):
    """Returns (status_line, None) on any HTTP answer, or (None, reason)."""
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(8)
    try:
        s.connect(sock)
        if use_tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            s = ctx.wrap_socket(s, server_hostname="cockpit-local")
        s.sendall(REQ.encode())
        data = s.recv(500)
        if not data:
            return None, "empty response from cockpit"
        return data.split(b"\r\n")[0].decode(errors="replace"), None
    finally:
        try:
            s.close()
        except Exception:
            pass


try:
    status = err = None
    try:
        status, err = probe(use_tls=True)
    except ssl.SSLError:
        # non-TLS cockpit build: retry plain HTTP
        status, err = probe(use_tls=False)
    if status is not None:
        print("OK:" + status)
    else:
        print("FAIL:" + str(err))
except Exception as e:
    print("FAIL:" + repr(e))
