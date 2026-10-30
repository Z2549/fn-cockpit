#!/usr/bin/env python3
"""Local test (TCP loopback via RELAY_TCP_PORT hook): fake cockpit backend + relay + client."""
import os, socket, sys, threading, time, subprocess

PORT = 38710
LISTEN_EP = ("127.0.0.1", PORT)
BACKEND_EP = ("127.0.0.1", PORT + 1)
RECEIVED = []

def backend_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(BACKEND_EP); srv.listen(8)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=backend_handle, args=(conn,), daemon=True).start()

def read_headers(conn):
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = conn.recv(65536)
        if not c: return None, b""
        buf += c
    head, rest = buf.split(b"\r\n\r\n", 1)
    return head, rest

def backend_handle(conn):
    buf = b""
    while True:
        head, rest = read_headers(conn)
        if head is None: conn.close(); return
        lines = head.split(b"\r\n")
        hdrs = {}
        for l in lines[1:]:
            if b":" in l:
                k, v = l.split(b":", 1); hdrs[k.strip().lower()] = v.strip()
        method, path, ver = lines[0].decode().split(" ")
        cl = int(hdrs.get(b"content-length", b"0"))
        while len(rest) < cl:
            rest += conn.recv(65536)
        body = rest[:cl]; rest = rest[cl:]
        RECEIVED.append((method, path, hdrs.get(b"x-forwarded-proto", b"<none>").decode(), body[:40]))
        if hdrs.get(b"upgrade", b"").lower() == b"websocket":
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            try:
                conn.sendall(b"BACKOK")
                data = conn.recv(65536)
                if data: conn.sendall(b"TUNNEL:" + data[:20])
                time.sleep(0.3)
            except OSError: pass
            conn.close(); return
        resp = f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(body)+len(path)}\r\nConnection: close\r\n\r\nOKPATH{path}".encode()
        conn.sendall(resp)
        conn.close(); return

threading.Thread(target=backend_server, daemon=True).start()

env = dict(os.environ)
env["RELAY_TCP_PORT"] = str(PORT)
relay = subprocess.Popen([sys.executable,
    r"C:\Users\ZJY\WorkBuddy\2026-10-30-23-07-06\native-ui-application-sample\app\server\relay.py"],
    env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
time.sleep(0.8)
print("[T] relay up")

def client_req(method, path, body=b"", ws=False):
    c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    c.connect(LISTEN_EP)
    hdr = f"{method} {path} HTTP/1.1\r\nHost: 192.168.31.182:5667\r\n"
    if ws:
        hdr += "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n"
    if body:
        hdr += f"Content-Length: {len(body)}\r\n"
    req = hdr.encode() + b"\r\n" + body
    c.sendall(req)
    time.sleep(0.4)
    c.settimeout(2)
    data = b""
    try:
        data = c.recv(262144)
    except socket.timeout:
        data = b"<timeout>"
    c.close()
    return data

r = client_req("GET", "/")
print("[T1] GET / ->", r.split(b"\r\n")[0], "| body:", r.split(b"\r\n\r\n",1)[-1][:40])
r = client_req("GET", "/cockpit/static/login.css")
print("[T2] GET css ->", r.split(b"\r\n")[0], "| body:", r.split(b"\r\n\r\n",1)[-1][:50])
r = client_req("POST", "/cockpit/login", body=b"user=admin&password=x")
print("[T3] POST login ->", r.split(b"\r\n")[0], "| body:", r.split(b"\r\n\r\n",1)[-1][:40])

# WS tunnel
c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
c.connect(LISTEN_EP)
c.sendall(b"GET /cockpit/socket HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n")
time.sleep(0.5)
c.settimeout(2)
resp = c.recv(65536)
print("[T4] WS handshake ->", resp.split(b"\r\n")[0])
c.sendall(b"PINGPAYLOAD")
time.sleep(0.4)
try:
    print("[T4] WS tunnel echo ->", c.recv(100))
except socket.timeout:
    print("[T4] WS tunnel echo -> <timeout>")
c.close()

print("\n[T] backend received (method, path, x-forwarded-proto, body):")
for m in RECEIVED:
    print("   ", m)

relay.terminate()
try:
    out = relay.stdout.read().decode(errors="replace")
    print("\n[T] relay stdout/stderr:\n", out[-800:])
except Exception:
    pass
