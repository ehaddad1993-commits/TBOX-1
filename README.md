# TBox — online industrial monitoring (Technical Power Systems)

TBox is a web platform plus an Edge Gateway for monitoring PLCs and industrial devices from anywhere:
login & roles, multi-company/site/building hierarchy, device and tag managers (no code changes to add devices), live gauges, trends, historian, alarms with acknowledgment and notifications, energy/fuel reports (CSV/Excel/PDF), backups and retention. Monitoring is the default; **writing to a Modbus coil/register is available but switched off** until you enable it (see docs/ARCHITECTURE.md, Security model).

```
server/      cloud backend + web UI          gateway/   TBox Edge Gateway
packages/    tbox_modbus (TCP/RTU, codec, simulator)    profiles/  device profiles (JSON)
tests/       unit + end-to-end tests         docs/      ARCHITECTURE.md, DEPLOYMENT.md
tools/       restore_backup.py, run_sim_device.py        deploy/    systemd, Docker, Caddy
```
**Open as an app window:** `python launch_tbox.py` (Windows: double-click `TBox.bat`; `Install-TBox-Shortcut.bat` puts a TBox icon on your Desktop). Add `--lan` to open it from phones on the same Wi-Fi.

Quick start: `python run_tests.py` then `cd server && python run_server.py` → http://127.0.0.1:8080. See `docs/DEPLOYMENT.md`.

## Status by stage
| Stage | State |
|---|---|
| 1 Structure, architecture, UI | Done |
| 2 Dashboard, device manager, tag manager, simulated data | Done (simulated data is labelled everywhere) |
| 3 Backend, DB, history, alarms, auth | Done |
| 4 Real Modbus TCP vs simulator | Done, tested end-to-end (gateway→server) |
| 5 Edge Gateway, Modbus RTU | Gateway done (buffering, resync, commands). RTU framing tested on loopback; **needs a real RS-485 adapter test** |
| 6 Vendor profiles | M221 starter; Deye/Socomec/DSE/VFD are **templates — need the vendor register documents** |
| 7 Online deployment | Configs provided; **needs your VPS/domain to verify** |

Not implemented yet: MQTT and OPC UA drivers, HMI/P&ID editor, PostgreSQL backend.
