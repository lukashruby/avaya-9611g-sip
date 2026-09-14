#!/usr/bin/env python3
"""Raw-socket HTTP provisioning server for Avaya 96x1 SIP deskphones.

Why this exists: Python's built-in ``http.server`` does NOT work as a file
server for the 96x1 SIP firmware (tested on 9611G, SIP R7.1.x). The phone's
HTTP client (libcurl 7.43 + a custom header parser) evaluates the response as
``HTTP_response[0]`` and discards the file, even though the whole body is
transferred. It only accepts a response written like a classic web server:
``HTTP/1.1``, explicit headers, ``Connection: close``. This server sends the
response byte for byte in that shape and closes immediately.

It serves a provisioning directory (``STAGE1``) and, for 46xxsettings.txt,
injects a SIP account read from ``ACCOUNTS`` (see .sip.example). The password
is never written to disk or logged. Runs without sudo (port > 1024).

Usage:
    HTTP_PORT=8611 STAGE1=./stage1 ACCOUNTS=./.sip python3 serve_avaya.py
"""
import datetime
import ipaddress
import os
import socket
import threading

BASE = os.path.dirname(os.path.abspath(__file__))
STAGE1 = os.environ.get("STAGE1", os.path.join(BASE, "stage1"))
ACCOUNTS = os.environ.get("ACCOUNTS", os.path.join(BASE, ".sip"))
LOG = os.environ.get("LOG_FILE", os.path.join(BASE, "avaya.log"))
PORT = int(os.environ.get("HTTP_PORT", "8611"))
# Comma-separated allowlist of client IPs; empty = allow all.
ALLOWED = {ip for ip in os.environ.get("ALLOW", "").split(",") if ip}
# Pick this account (username) from the accounts file; empty = first entry.
SIP_ACCOUNT = os.environ.get("SIP_ACCOUNT", "")
SETTINGS = "46xxsettings.txt"
DEFAULT_SIP_PORT = 5060
_lock = threading.Lock()

CTYPES = {".txt": "text/plain", ".tar": "application/octet-stream",
          ".bin": "application/octet-stream", ".xml": "text/xml",
          ".jpg": "image/jpeg", ".pdf": "application/pdf"}


def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    with _lock:
        print(line, flush=True)
        try:
            with open(LOG, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass


def load_account():
    """Return (server, username, password) from the accounts file, or None.

    File format: one ``server;username;password`` per line, ``#`` comments.
    ``server`` is ``host[:port]`` (host may be a DNS name or an IPv4 address).
    """
    try:
        lines = open(ACCOUNTS).read().splitlines()
    except FileNotFoundError:
        return None
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        parts = raw.split(";", 2)
        if len(parts) == 3 and all(parts[:2]) and parts[2]:
            if SIP_ACCOUNT and parts[1].strip() != SIP_ACCOUNT:
                continue
            return parts[0].strip(), parts[1].strip(), parts[2]
    return None


def is_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def settings_body():
    """Read 46xxsettings.txt and append FORCE_SIP_* lines for the account.

    The 96x1 wants a numeric IPv4 in SIP_CONTROLLER_LIST, so a DNS host is
    resolved here. Password max length is 13 on this firmware.
    """
    text = open(os.path.join(STAGE1, SETTINGS), "rb").read()
    acc = load_account()
    if not acc:
        log("46xxsettings: no account in accounts file, serving without login")
        return text
    server, user, pw = acc
    host, _, port = server.partition(":")
    port = port or str(DEFAULT_SIP_PORT)
    try:
        ip = host if is_ip(host) else socket.gethostbyname(host)
    except OSError as e:
        log(f"46xxsettings: DNS for {host} failed ({e!r}), serving without login")
        return text
    if len(pw) > 13:
        log(f"46xxsettings: account {user}: password > 13 chars, phone may reject it")
    extra = ["", "## account injected by serve_avaya.py"]
    if not is_ip(host):
        extra.append(f"SET SIPDOMAIN {host}")
    extra += [f"SET SIP_CONTROLLER_LIST {ip}:{port};transport=udp",
              f"SET FORCE_SIP_USERNAME {user}",
              f"SET FORCE_SIP_EXTENSION {user}",
              f'SET FORCE_SIP_PASSWORD "{pw}"']
    log(f"46xxsettings: account {user}@{host} -> {ip}:{port}")
    return text.rstrip(b"\n") + b"\n" + "\n".join(extra).encode() + b"\n"


def build_body(path):
    if path == "/" + SETTINGS:
        return settings_body(), "text/plain"
    rel = path.lstrip("/")
    full = os.path.normpath(os.path.join(STAGE1, rel))
    if not full.startswith(os.path.abspath(STAGE1)) or not os.path.isfile(full):
        return None, None
    ext = os.path.splitext(full)[1].lower()
    return open(full, "rb").read(), CTYPES.get(ext, "application/octet-stream")


def http_date():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S GMT")


def handle(conn, addr):
    try:
        conn.settimeout(20)
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = conn.recv(4096)
            if not chunk:
                return
            req += chunk
            if len(req) > 65536:
                return
        parts = req.split(b"\r\n", 1)[0].decode("latin-1").split()
        if len(parts) < 2:
            return
        method, path = parts[0], parts[1]
        head_only = method == "HEAD"
        body, ctype = build_body(path)
        if body is None:
            conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n"
                         b"Connection: close\r\n\r\n")
            log(f"{addr[0]} {method} {path} -> 404")
            return
        # Byte-for-byte like Apache: status line, explicit headers, close.
        headers = (
            f"HTTP/1.1 200 OK\r\n"
            f"Date: {http_date()}\r\n"
            f"Server: Apache\r\n"
            f"Last-Modified: {http_date()}\r\n"
            f"Accept-Ranges: bytes\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Content-Type: {ctype}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("latin-1")
        conn.sendall(headers if head_only else headers + body)
        log(f"{addr[0]} {method} {path} -> 200 {len(body)}B {ctype}")
    except OSError as e:
        log(f"{addr[0]} error: {e!r}")
    finally:
        try:
            conn.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        conn.close()


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT))
    srv.listen(16)
    log(f"serving {STAGE1} on 0.0.0.0:{PORT} (allow={ALLOWED or 'any'})")
    while True:
        conn, addr = srv.accept()
        if ALLOWED and addr[0] not in ALLOWED:
            log(f"REJECT {addr[0]}")
            conn.close()
            continue
        threading.Thread(target=handle, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    main()
