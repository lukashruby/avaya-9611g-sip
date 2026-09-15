# Avaya 96x1 → SIP, zero-touch provisioning to a third-party PBX

Convert an Avaya 9608/9611G/9621G/9641G deskphone from H.323 to SIP and
auto-register it to a **non-Avaya** SIP server (tested with a 9611G "D02B" against
a third-party SIP trunk), with **no typing on the phone keypad** — everything
comes from a local HTTP file server + DHCP.

The one thing that made this hard is documented in **[Lessons learned](#lessons-learned)**.
If you only read one section, read that: **Python's `http.server` does not work
as a provisioning server for the 96x1 SIP firmware.**

> Scope: tested on a single 9611GD02B, firmware H.323 6.4.0.14 → SIP 7.1.1 → 7.1.8.
> The findings should apply to the whole 96x1 SIP family, but treat versions as examples.

## What's in here

| File | Purpose |
|------|---------|
| `serve_avaya.py` | Raw-socket HTTP provisioning server (the working one). No deps. |
| `46xxsettings.example.txt` | Sample settings for a third-party SIP server. |
| `.sip.example` | Format of the SIP account file (`server;username;password`). |

Not included (get them yourself): the **Avaya firmware** (`96x1-IPT-SIP-R7_1_*.zip`,
licensed — download from Avaya Support or your distributor) and your own `.sip`.

## Prerequisites

- The phone and a computer on the same L2 network/VLAN, with DHCP you control
  (a MikroTik in this write-up).
- The firmware ZIP(s) for your target SIP release, unzipped into a `stage1/`
  directory that the server will serve. The upgrade path matters if you jump
  several releases — Avaya documents a bridge release for old H.323 loads.
- `stage1/96x1Supgrade.txt` **must stay the original from the ZIP** (Avaya
  forbids editing it; edits are ignored). Put site config in `46xxsettings.txt`.

## Quick start

```bash
# 1. Unzip firmware into stage1/ (keep 96x1Supgrade.txt as-is)
mkdir -p stage1 && unzip -o 96x1-IPT-SIP-R7_1_8_0-*.zip -d stage1

# 2. Settings + account
cp 46xxsettings.example.txt stage1/46xxsettings.txt   # then edit
cp .sip.example .sip && chmod 600 .sip                # then add server;user;pass

# 3. Run the server (bind to the IP the phone will reach; port > 1024 = no sudo)
HTTP_PORT=8611 STAGE1=./stage1 ACCOUNTS=./.sip \
  ALLOW=<phone-ip>,<server-ip> python3 serve_avaya.py

# 4. Point the phone at it via DHCP option 242 (see below), then reboot the phone
```

### DHCP option 242 (site-specific option)

Give the phone the HTTP server and SIP mode. On MikroTik RouterOS, option 242
is a hex string; scope it to the phone's lease, not the whole network:

```rsc
# value = ASCII "HTTPSRVR=<srv>,HTTPPORT=8611,SIG=2" as hex (0x...)
/ip dhcp-server option add name=avaya code=242 value=0x...
/ip dhcp-server lease make-static [find mac-address=AA:BB:CC:DD:EE:FF]
/ip dhcp-server lease set [find mac-address=AA:BB:CC:DD:EE:FF] dhcp-option=avaya
```

Only a fixed set of parameters works via option 242 (SSON). Confirmed useful:
`HTTPSRVR`, `HTTPPORT`, `TLSSRVR`, `TLSPORT`, `TLSSRVRID`, `SIG`,
`SIP_CONTROLLER_LIST`, `HTTPDIR`, `TLSDIR`, `L2Q`, `L2QVLAN`, `VLANTEST`.
**`CONFIG_SERVER_SECURE_MODE`, `SIPDOMAIN`, `ENABLE_*` are NOT accepted over
DHCP** — they must go in `46xxsettings.txt`.

### First-time H.323 → SIP conversion

If the phone still runs H.323, in the craft menu (`Mute` `2 7 2 3 8 #`) set the
HTTP server (`ADDR`) and switch `SIG` to SIP, or push `SIG=2` via option 242,
then reboot. It downloads the SIP image and converts. The upgrade path may need
a bridge release (e.g. old H.323 → SIP 7.1.1 → 7.1.8); check the release readme.

## Lessons learned

### 1. Python `http.server` breaks 96x1 SIP provisioning — use a real HTTP shape

This cost the most time. Symptom: the phone downloads `96x1Supgrade.txt` and
**stops** — never fetches `46xxsettings.txt` or the firmware. Remote syslog
(see below) showed the smoking gun:

```
END file download from http://.../96x1Supgrade.txt, size [1026], HTTP_response[0]
CScriptDataAdaptor::FetchURL failed: Code = 0 Result = 0. Unable to download file
```

The **whole body transfers** (size = file size), but the phone reads the HTTP
status code as **0** and discards the file. The 96x1 SIP firmware uses
libcurl 7.43 plus a strict custom header parser that rejects the responses
produced by Python's `http.server` — over **both HTTP and HTTPS**. (An old
H.323 6.4 load accepted the same Python server; the SIP firmware does not.)

Fix: serve the response **byte for byte like a classic web server** —
`HTTP/1.1 200 OK`, explicit `Content-Type` / `Content-Length` / `Last-Modified`,
and `Connection: close`, then close the socket. That is exactly what
`serve_avaya.py` does, and what real deployments (Apache/nginx/IIS) do. With
that change the phone reported `HTTP_response[200]`, fetched the settings and
firmware, verified the signatures and upgraded — on the first try.

If you use Apache/nginx instead of this script, you don't hit the bug; it is
specific to lightweight/ad-hoc servers that emit a slightly non-standard reply.

### 2. HTTPS is a dead end with a self-signed cert

`CONFIG_SERVER_SECURE_MODE` defaults to `1` (always HTTPS), so the phone tries
HTTPS first. But its trust store contains **only Avaya CAs** (Avaya Product
Root CA, SIP Product CA, Avaya Call Server) — a self-signed cert won't validate,
and on top of that the HTTPS client hit the same `HTTP_response[0]` issue.
The phone falls back to HTTP after HTTPS fails, so the pragmatic fix is to
serve config over HTTP: set **`HTTPSRVR` (and not `TLSSRVR`)** via DHCP so the
phone goes straight to HTTP. Firmware images are HTTP-only anyway (they're
digitally signed, so Avaya doesn't require TLS for them).

### 3. Remote syslog from the phone is the essential diagnostic

Without it you're guessing. On the phone: craft menu → `LOG` → set level to
**Debug**, **Remote Logging = Enabled**, **Remote Log Server = <your IP>**,
save, reboot. Run any UDP/514 collector (a ~20-line Python script). Every
provisioning step, HTTP response code and signature check shows up there. Note
`DEBUG` in the craft menu is a serial-port toggle for the button module — not
logging; the logging item is `LOG`.

### 4. Don't confuse "file downloaded" with "file accepted"

`tcpdump` and `curl` will happily show `200 OK` and the full body while the
phone still rejects it. Trust the phone's own syslog
(`UPGRADE_FILE_EXECUTION_STATUS`, `HTTP_response[...]`), not the wire capture,
for whether provisioning actually advanced.

### 5. Zero-touch account = `FORCE_SIP_*`

`FORCE_SIP_USERNAME`, `FORCE_SIP_EXTENSION`, `FORCE_SIP_PASSWORD` in
`46xxsettings.txt` log the phone in without keypad entry. Password max is **13
characters** on this firmware. `serve_avaya.py` injects these from `.sip` over
HTTP only and never logs the password. `SIP_CONTROLLER_LIST` wants a **numeric
IPv4** — a DNS name may be ignored — so resolve it (the script does).

### 6. Craft-menu settings win over the settings file

Anything entered manually in the craft `SIP` menu takes precedence over
`46xxsettings.txt` and PPM. If you experimented by hand, `CLEAR` the phone
(craft menu) before relying on provisioning, or the stale manual values remain.

## Manual fallback (no server)

Craft menu → `SIP`: set SIP Domain, Proxy Policy = Manual, add a SIP Proxy
Server (IP / UDP / port), save, then enter user + password when prompted.
Works, but it's per-phone and defeats the point of provisioning.

## Security / privacy

- Never commit `.sip`, logs, captures, or firmware. See `.gitignore`.
- The account password lives only in `.sip` (chmod 600) and in the phone's
  flash; it is injected over HTTP on a trusted LAN segment only.

---

*Written up from a real conversion of a 9611GD02B to SIP on a third-party SIP
trunk. Provided as-is; verify against your own Avaya documentation and firmware.*
