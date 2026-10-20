# TBox architecture

TBox is a web-based industrial monitoring platform by Technical Power Systems. It has four separate modules.

```
 PLANT NETWORK (private)                    INTERNET                      CLOUD / VPS
┌───────────────────────────┐                                  ┌─────────────────────────────────┐
│ PLC / meter / inverter /  │   Modbus TCP / RTU               │  TBox server                     │
│ generator controller / VFD│◄──────────────┐                  │  ├ web UI (static, no build)    │
└───────────────────────────┘               │                  │  ├ REST API  (/api/*)           │
                                   ┌────────┴─────────┐ HTTPS  │  ├ alarm + status engine        │
                                   │ TBox Edge Gateway │───────►│  ├ historian + reports          │
                                   │ (outbound only)   │ token  │  └ SQLite (WAL) + backups       │
                                   │ drivers • buffer  │        └──────────────▲──────────────────┘
                                   └───────────────────┘                       │ HTTPS (login)
                                                                  Windows PC / Android / tablet / browser
```

| Module | Folder | Responsibility |
|---|---|---|
| Web interface | `server/tbox_server/static` | Login, dashboards, device/tag managers, trends, alarms, reports. Plain ES modules, no build step, no inline scripts (strict CSP). |
| Cloud backend | `server/tbox_server` | Auth, RBAC, REST API, ingest, alarm engine, status monitor, reports, backups, retention. |
| Database | `server/tbox_server/migrations` | SQL migrations, applied automatically and recorded in `schema_migrations`. |
| Industrial drivers | `packages/modbus`, `gateway/tbox_gateway/drivers` | Protocol code. Used **only** by the gateway (and tests), never by the web app. |
| Edge Gateway | `gateway` | Polls devices, store-and-forward buffer, outbound HTTPS uplink, runs test/reconnect commands. |

## Technology choices (and why)

* **Python 3.10+, standard library only for the core.** One language for server, gateway and drivers; installs on Windows, Linux and small industrial PCs with no compiler or package problems; very small attack surface (no third-party web framework to patch). Optional packages add Excel/PDF export, credential encryption and RS-485 serial support.
* **SQLite in WAL mode** with SQL migrations. Zero administration, atomic backups through SQLite's online backup API, comfortable for tens of thousands of tags at 1-minute logging on one server. *It is the right start, not the end state:* the schema is plain SQL and all access goes through `db.py`, so moving the historian to PostgreSQL/TimescaleDB is a contained change when you need many gateways or multi-year per-second data (see "Scaling").
* **Gateway → cloud over outbound HTTPS with a per-gateway bearer token.** No inbound ports at the plant, works through NAT/firewalls. Tokens are stored only as SHA-256 hashes on the server and can be rotated.
* **Vanilla JS front end.** Nothing to build or deploy except files; works as an installable web app (PWA manifest) on Windows, Android and tablets.

## Data flow

1. Engineer creates a **gateway** in TBox (token shown once) and installs the gateway at the site.
2. Engineer adds a **device** (connection settings + profile or CSV register map) and assigns the gateway. The gateway pulls its configuration (`GET /api/gateway/config`) including credentials, and starts one worker per device.
3. Each worker reads blocks of registers, decodes (data type, byte order, bit), scales and tags Good/Bad quality. Values go into a durable local SQLite queue.
4. The uplink loop posts batches (`POST /api/gateway/ingest`) with per-device status. A batch is deleted locally only after the server acknowledges it. During an outage samples keep accumulating (at each tag's logging resolution) and are back-filled with their original timestamps when the link returns.
5. The server validates that each sample belongs to a device of that gateway, updates live state, logs history by the tag's logging interval, evaluates alarms and derives device status.

## Device status

* **Online** – communicating, no active limit alarm.
* **Alarm** – communicating, at least one active limit alarm.
* **Communication lost** – the device stopped answering (reported by the gateway) or the gateway itself missed its heartbeat. A high-priority *communication lost* alarm is raised and cleared automatically.
* **Offline** – disabled/disconnected by a user, no gateway assigned, or never connected.

**Quality** is Good (fresh, valid), Bad (read failed, decode error, missing value) or Stale (last Good value older than max(10 s, 3 × poll interval)). Stale is computed at read time, so it is correct even if the gateway vanishes.

## Data model (summary)

`companies → sites → buildings`; `gateways` and `devices` belong to a site; `tags` belong to a device. `tag_state` holds the live value; `samples` is the historian (`tag_id, ts_ms_utc, value, quality`); `alarms`, `events` (system/device/alarm/audit/security), `gateway_commands`, `notification_channels`, `settings`, `user_dashboards`. Deleting a device or tag that has history **archives/disables** it instead, so history keeps its device identity (a hard delete is an explicit admin-only `purge`).

## Security model

* Passwords: PBKDF2-SHA256 (240k iterations), policy ≥10 chars with mixed case + digit; first-run admin password is random and must be changed.
* Sessions: HS256 JWT (8 h) carrying a per-user token version, so password change/role change/deactivation revokes old tokens immediately. Login is rate-limited per user+IP.
* RBAC: **admin** (users, companies, sites, gateways, settings), **engineer** (devices, tags, buildings within assigned sites), **operator** (view, acknowledge alarms). Non-admin users only ever see their assigned sites; out-of-scope IDs return 404.
* Gateways: separate bearer tokens, can only write samples for tags of their own devices (verified server-side), cannot call user endpoints, and vice-versa.
* Device credentials are write-only through the API (never returned), encrypted at rest when `cryptography` is installed (the System page warns when it is not).
* HTTP: strict CSP (no inline script), `X-Frame-Options: DENY`, `nosniff`, `no-store` on API responses; all UI text is inserted with `textContent` (no `innerHTML`). Run it behind HTTPS (see DEPLOYMENT.md); the server binds to localhost by default.
* **Remote control (writes) is off by default** and needs every one of these layers to agree: (1) an administrator turns on the master switch in Settings; (2) the tag is set to *Read/Write* (coils and holding registers only) with optional min/max limits; (3) the user is an Administrator or Engineer; (4) the dialog requires an explicit confirmation; (5) the gateway has `allow_writes: true` in its **own local** config, so the plant side can always veto the cloud; (6) the gateway re-checks permission and range, writes, then **reads the value back** and reports verified/mismatch. Writes are never queued for later (gateway offline = refused). Every request, refusal and result is written to the audit log. Simulated devices cannot be written. This is a convenience channel, **not a safety system**: interlocks, limits and emergency logic must stay in the PLC.

## Extending TBox

* **New device type:** add a JSON file to `profiles/` (name, manufacturer, model, protocol defaults, tags) or use *Tags → Import CSV*. No code change.
* **New protocol:** implement a driver class in `gateway/tbox_gateway/drivers/` exposing `open()`, `close()`, `read(tags) -> {tag_id: (value, quality, error)}` and `test(tags)`, register it in `drivers/__init__.py`. The gateway advertises only protocols it can actually run, and the UI warns when a device's protocol is missing on its gateway. MQTT and OPC UA are selectable in the UI but **have no driver yet**; a device configured with them shows an explicit "driver not available" error rather than fake data.
* **New report/export:** `reports.py` + a branch in `api_ops.report_export`.

## Scaling (honest limits)

Single-process server + SQLite is appropriate for roughly: tens of gateways, ≤ ~20k tags, 1-minute logging (≈ 30 M rows/year; fine), or a few hundred tags at 1-second logging. Beyond that: move `samples` to TimescaleDB/PostgreSQL, run the API behind a process manager with several workers, and replace 2-second UI polling with server-sent events/WebSocket. The Modbus polling load lives on gateways, not on the server.

## Known limitations of this release

* Modbus TCP and Modbus RTU are implemented. RTU is tested against an in-memory RS-485 loopback; **a physical USB-RS485 adapter and slave have not been exercised**.
* Vendor profiles for Deye, Socomec, Deep Sea Electronics and VFDs are **templates without register maps** (see `profiles/*.json`). Register addresses must come from the manufacturer's document for the exact model and firmware; TBox will not guess them.
* The Schneider M221 profile is a **starter** with `%MW`/`%M` example tags that must be confirmed on your controller.
* MQTT / OPC UA drivers are not implemented.
* Notification delivery (webhook/SMTP) is implemented but not tested against a live mail server.
* Docker/Caddy deployment files are provided but untested in the build environment.
