# Installing, deploying, backing up

## 0. Open TBox like an app
`python launch_tbox.py` starts the server in the background and opens TBox in its own Chrome window (no address bar). `python launch_tbox.py --install-shortcut` creates a Desktop icon (Windows) or app-menu entry (Linux). `--lan` makes it reachable from phones/tablets on the same Wi-Fi at `http://<PC-IP>:8080` (plain HTTP, trusted networks only). `--stop` stops it. In Chrome you can also use menu > Save and share > Install page as app. A link that works from *anywhere* needs the server on the internet with HTTPS (section 4).

## 1. Local install (Windows or Linux) — 2 minutes
Requirements: Python 3.10+. Optional but recommended: `pip install -r server/requirements.txt`.

```
python run_tests.py            # optional: all automated tests
cd server && python run_server.py
```
Windows: double-click `start_server.bat`. Open http://127.0.0.1:8080. On first start the console prints the **one-time admin password** (you must change it at first login). To choose it yourself: `TBOX_ADMIN_PASSWORD=...`.

The demo site with **SIMULATED** devices is created on first start (`TBOX_SEED_DEMO=0` disables it; `TBOX_SIMULATOR=0` stops the generator).

## 2. Try the real Modbus path end to end (no hardware)
```
python tools/run_sim_device.py                 # terminal 1: Modbus TCP simulator on port 1502
cd server && python run_server.py              # terminal 2
```
In TBox: Gateways → *Add gateway* (copy the token) → Devices → *Add device*: source "Real device via TBox Edge Gateway", profile "TBox Modbus simulator meter", host `127.0.0.1`, port `1502`, assign the gateway. Then:
```
cp gateway/gateway.example.json gateway/gateway.json   # set server_url + token
cd gateway && python run_gateway.py --config gateway.json   # terminal 3
```
The device turns Online and values appear **without** the SIMULATED badge. Stop the simulator to see *Communication lost* + alarm; stop the gateway's internet (or server) to watch buffering and back-fill.

## 3. Connecting a real PLC / meter
1. Put the gateway on a PC in the plant network that can reach the device (Ethernet) or has a USB-RS485 adapter (`pip install pyserial`; serial port e.g. `COM3`, `/dev/ttyUSB0`; Linux user needs the `dialout` group).
2. Add the device with the exact IP/port/slave ID or serial parameters (baud, parity, stop bits) the device is configured for.
3. Load registers from the **manufacturer's register document** (CSV import or manual tags). Use *Test connection* and compare a few values with the device's own display before trusting it.
4. Schneider M221: enable the Ethernet Modbus TCP server in the controller project; start from the `schneider-m221` starter profile and confirm addressing.

## 4a. Render (easiest, automatic HTTPS)
See `docs/RENDER.md` and `render.yaml`.

## 4. Cloud deployment (VPS with a domain)
Recommended: any small Linux VPS (1–2 vCPU, 2 GB RAM), Ubuntu LTS.
1. `git clone` the repo to `/opt/tbox`; `pip install -r server/requirements.txt`; create user `tbox`, data dir `/var/lib/tbox`.
2. `/etc/tbox/tbox.env` containing `TBOX_ADMIN_PASSWORD=<strong>`; install `deploy/tbox-server.service`; `systemctl enable --now tbox-server`.
3. HTTPS: install Caddy and use `deploy/Caddyfile` (automatic certificates), or nginx + certbot, proxying to `127.0.0.1:8080`. Keep `TBOX_TRUST_PROXY=1` only when a proxy you control sits in front.
4. Firewall: allow 80/443 only. **Never** open the PLC ports, or 8080, to the internet.
5. Docker alternative: `deploy/docker-compose.yml` (untested here; review first).
6. Gateways use `https://your.domain` as `server_url` and verify TLS (default).

Hardening checklist: unique strong admin password; named accounts per person with least-privilege roles; install `cryptography`; disk encryption on the server; copy `data/backups` off the machine; keep Python and OS patched; restrict SSH.

## 5. Gateway as a service
* Linux: `deploy/tbox-gateway.service`.
* Windows: run `start_gateway.bat` from Task Scheduler "at startup", or wrap `python run_gateway.py` with NSSM as a service.
The gateway caches its configuration, so it keeps polling and buffering after a reboot even if the internet is down. The buffer lives in `gateway_data/buffer.db` (default cap 2 M records, oldest dropped first with an error log).

## 6. Backup and recovery
* Automatic: a verified online backup daily into `data/backups` (keep N, set in Settings). Manual: Settings → *Back up now*.
* Also copy `data/secret.key` (signing key; losing it only logs everyone out, but credential encryption derives from it, so **keep it with the backups**).
* Restore: stop the server, then
```
python tools/restore_backup.py data/backups/tbox-YYYYMMDD-HHMMSS.db --data-dir data
```
The tool verifies integrity, keeps the current DB as `tbox.db.before-restore-*`, then swaps. Start the server; gateways resume and back-fill.
* Retention (Settings): measurements 365 d, events 730 d, alarms 1095 d by default.

## 7. Stage 7 checklist (needs your real infrastructure)
Remote access from phone/PC over the internet, TLS certificate, gateway at a real site over a real WAN, power/internet-cut test, restore drill on a second machine. None of this can be verified from the build environment.

## 8. Enabling writes (remote control)
1. Gateway: set `"allow_writes": true` in `gateway.json` (or `TBOX_GATEWAY_ALLOW_WRITES=1`) and restart it.
2. TBox → Settings → *Enable remote control*.
3. Tag Manager: set the tag to *Read/Write* (coil or holding register), set write min/max, save.
4. Use the **Write** button on the tag (Tags page, or the widget on Live). Confirm; TBox reports whether the device read-back matches.
Test on a spare register/coil first. Never use this for emergency stop or interlocks.
