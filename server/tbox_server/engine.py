"""Core runtime: sample ingest, historian logging, alarm evaluation, device status,
simulated devices, notifications, retention and backups."""
import json
import logging
import math
import random
import smtplib
import threading
import time
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage

from .db import now_ms

log = logging.getLogger("tbox.engine")

PRIORITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
RANK_PRIORITY = {v: k for k, v in PRIORITY_RANK.items()}
KIND_LABEL = {"hihi": "high-high", "hi": "high", "lo": "low", "lolo": "low-low", "comm_lost": "communication lost"}
MAX_FUTURE_MS = 5 * 60 * 1000


# ------------------------------------------------------------------ simulator maths
def sim_value(sim, t, tag_id=0):
    """Deterministic simulated signal for time t (seconds). `sim` is the tag's generator JSON."""
    kind = sim.get("kind", "const")
    noise = sim.get("noise", 0.0)
    n = 0.0
    if noise:
        n = random.Random(tag_id * 1_000_003 + int(t)).uniform(-1, 1) * noise
    if kind == "const":
        return sim.get("value", 0) + n
    if kind == "sine":
        period = sim.get("period_s", 60)
        return sim.get("base", 0) + sim.get("amp", 1) * math.sin(2 * math.pi * (t / period) + sim.get("phase", 0)) + n
    if kind == "counter":
        return sim.get("start", 0) + sim.get("rate_per_h", 1) * (t % (30 * 86400)) / 3600.0
    if kind == "square":
        period = sim.get("period_s", 120)
        return 1.0 if (t % period) < period * sim.get("duty", 0.5) else 0.0
    if kind == "saw":  # falls from high to low then jumps back (fuel tank with refuelling)
        period = sim.get("period_s", 3600)
        frac = (t % period) / period
        return sim.get("high", 100) - (sim.get("high", 100) - sim.get("low", 20)) * frac + n
    if kind == "daylight":
        period = sim.get("period_s", 86400)
        s = math.sin(2 * math.pi * ((t % period) / period - 0.25))
        return max(0.0, s) * sim.get("peak", 10) + (abs(n) if s > 0.05 else 0.0)
    return 0.0


class Engine:
    def __init__(self, db, cfg, vault):
        self.db, self.cfg, self.vault = db, cfg, vault
        self._lock = threading.RLock()
        self._tags = {}
        self._tags_dirty = True
        self._state = {}
        self._active = {}                 # (device_id, tag_id|0, kind) -> alarm id
        self._gw_offline = set()
        self._stop = threading.Event()
        self._threads = []
        self._last_maintenance = 0
        self._last_purge = 0
        self.started_at = time.time()
        self._load_active_alarms()

    # ------------------------------------------------------------ cache
    def invalidate(self):
        self._tags_dirty = True

    def _tag_cache(self):
        if self._tags_dirty:
            with self._lock:
                rows = self.db.q(
                    "SELECT t.*, d.site_id, d.name AS device_name, d.source, d.gateway_id, d.connection "
                    "FROM tags t JOIN devices d ON d.id=t.device_id WHERE d.archived=0")
                self._tags = {r["id"]: r for r in rows}
                self._tags_dirty = False
        return self._tags

    def _load_active_alarms(self):
        for r in self.db.q("SELECT id, device_id, tag_id, kind FROM alarms WHERE active=1"):
            self._active[(r["device_id"], r["tag_id"] or 0, r["kind"])] = r["id"]

    # ------------------------------------------------------------ events
    def log_event(self, category, message, severity="info", site_id=None, device_id=None,
                  tag_id=None, username=None, detail=None):
        self.db.execute(
            "INSERT INTO events(ts,category,severity,site_id,device_id,tag_id,username,message,detail) VALUES(?,?,?,?,?,?,?,?,?)",
            (now_ms(), category, severity, site_id, device_id, tag_id, username, message,
             json.dumps(detail) if detail is not None else None))

    # ------------------------------------------------------------ ingest
    def ingest_batch(self, items, now=None, allowed_device_ids=None):
        """items: iterable of (tag_id, ts_ms, value|None, quality). Returns (accepted, rejected)."""
        now = now or now_ms()
        cache = self._tag_cache()
        accepted = rejected = 0
        notifications = []
        dirty = set()
        with self.db.tx():
            for tag_id, ts, value, quality in sorted(items, key=lambda x: (x[0], x[1])):
                tag = cache.get(tag_id)
                if (tag is None or not isinstance(ts, (int, float)) or ts > now + MAX_FUTURE_MS
                        or (allowed_device_ids is not None and tag["device_id"] not in allowed_device_ids)):
                    rejected += 1
                    continue
                ts = int(ts)
                quality = quality if quality in ("good", "bad") else "bad"
                if isinstance(value, bool):
                    value = 1.0 if value else 0.0
                if value is not None:
                    try:
                        value = float(value)
                        if math.isnan(value) or math.isinf(value):
                            value, quality = None, "bad"
                    except (TypeError, ValueError):
                        value, quality = None, "bad"
                if value is None:
                    quality = "bad"
                accepted += 1
                st = self._get_state(tag_id)
                newer = st is None or ts >= st["ts"]
                if newer:
                    prev = st or {"last_logged_ts": None, "last_logged_quality": None}
                    st = {"value": value, "quality": quality, "ts": ts, "received_ts": now,
                          "last_logged_ts": prev["last_logged_ts"], "last_logged_quality": prev["last_logged_quality"]}
                    self._state[tag_id] = st
                    dirty.add(tag_id)
                    if quality == "good":
                        self.db.execute("UPDATE devices SET last_comm_ts=? WHERE id=? AND (last_comm_ts IS NULL OR last_comm_ts<?)",
                                        (ts, tag["device_id"], ts))
                    if ts >= now - 120_000:
                        self._eval_alarms(tag, value, quality, ts, notifications)
                    need = (st["last_logged_ts"] is None or quality != st["last_logged_quality"]
                            or ts - st["last_logged_ts"] >= tag["log_interval_s"] * 1000)
                else:
                    need = True  # late / back-filled data: keep it, duplicates are ignored by the primary key
                if need:
                    self.db.execute("INSERT OR IGNORE INTO samples(tag_id,ts,value,quality) VALUES(?,?,?,?)",
                                    (tag_id, ts, value, quality))
                    if newer:
                        st["last_logged_ts"], st["last_logged_quality"] = ts, quality
            for tag_id in dirty:
                s = self._state[tag_id]
                self.db.execute(
                    "INSERT INTO tag_state(tag_id,value,quality,ts,received_ts,last_logged_ts,last_logged_quality) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(tag_id) DO UPDATE SET value=excluded.value, quality=excluded.quality, ts=excluded.ts, "
                    "received_ts=excluded.received_ts, last_logged_ts=excluded.last_logged_ts, last_logged_quality=excluded.last_logged_quality",
                    (tag_id, s["value"], s["quality"], s["ts"], s["received_ts"], s["last_logged_ts"], s["last_logged_quality"]))
        for a in notifications:
            self.notify(a)
        return accepted, rejected

    def _get_state(self, tag_id):
        st = self._state.get(tag_id)
        if st is None:
            st = self.db.one("SELECT value,quality,ts,received_ts,last_logged_ts,last_logged_quality FROM tag_state WHERE tag_id=?", (tag_id,))
            if st:
                self._state[tag_id] = st
        return st

    # ------------------------------------------------------------ alarms
    def _eval_alarms(self, tag, value, quality, ts, notifications):
        if not tag["alarm_enabled"] or not tag["enabled"] or quality != "good" or value is None:
            return
        pr = PRIORITY_RANK[tag["alarm_priority"]]
        for kind, limit, high in (("hihi", tag["hihi"], True), ("hi", tag["hi"], True),
                                  ("lo", tag["lo"], False), ("lolo", tag["lolo"], False)):
            if limit is None:
                continue
            key = (tag["device_id"], tag["id"], kind)
            tripped = value >= limit if high else value <= limit
            active_id = self._active.get(key)
            if tripped and active_id is None:
                prio = RANK_PRIORITY[min(4, pr + 1)] if kind in ("hihi", "lolo") else tag["alarm_priority"]
                unit = (" " + tag["unit"]) if tag["unit"] else ""
                msg = "%s / %s: %s%s is %s than the %s limit (%s%s)" % (
                    tag["device_name"], tag["name"], _fmt(value), unit, "higher" if high else "lower",
                    KIND_LABEL[kind], _fmt(limit), unit)
                a = self._raise(tag["site_id"], tag["device_id"], tag["id"], kind, prio, msg, value, ts)
                notifications.append(a)
            elif active_id is not None:
                db_ok = value < limit - tag["deadband"] if high else value > limit + tag["deadband"]
                if db_ok:
                    self._clear(key, ts, tag)

    def _raise(self, site_id, device_id, tag_id, kind, priority, message, value, ts):
        cur = self.db.execute(
            "INSERT INTO alarms(site_id,device_id,tag_id,kind,priority,message,value,raised_ts,active) VALUES(?,?,?,?,?,?,?,?,1)",
            (site_id, device_id, tag_id, kind, priority, message, value, ts))
        aid = cur.lastrowid
        self._active[(device_id, tag_id or 0, kind)] = aid
        self.log_event("alarm", "ALARM: " + message, "error" if PRIORITY_RANK[priority] >= 3 else "warning",
                       site_id, device_id, tag_id)
        return {"id": aid, "site_id": site_id, "device_id": device_id, "tag_id": tag_id, "kind": kind,
                "priority": priority, "message": message, "value": value, "raised_ts": ts}

    def _clear(self, key, ts, tag=None):
        aid = self._active.pop(key, None)
        if aid is None:
            return
        self.db.execute("UPDATE alarms SET active=0, cleared_ts=? WHERE id=?", (ts, aid))
        row = self.db.one("SELECT site_id, message FROM alarms WHERE id=?", (aid,))
        self.log_event("alarm", "Returned to normal: " + row["message"], "info", row["site_id"], key[0], key[1] or None)

    def device_has_alarm(self, device_id):
        return any(k[0] == device_id and k[2] != "comm_lost" for k in self._active)

    def ack_alarm(self, alarm_id, username):
        with self.db.tx():
            r = self.db.one("SELECT * FROM alarms WHERE id=?", (alarm_id,))
            if not r or r["acked_ts"]:
                return False
            self.db.execute("UPDATE alarms SET acked_ts=?, acked_by=? WHERE id=?", (now_ms(), username, alarm_id))
            self.log_event("alarm", "Alarm acknowledged: " + r["message"], "info", r["site_id"], r["device_id"],
                           r["tag_id"], username)
        return True

    # ------------------------------------------------------------ device status
    def effective_status(self, dev, gw, now, offline_after_ms):
        if dev["archived"] or not dev["enabled"]:
            return "offline"
        if dev["source"] == "simulated":
            if dev["last_comm_ts"] is None:
                return "offline"
            base = "online" if now - dev["last_comm_ts"] < 5000 else "comm_lost"
        else:
            if gw is None or not gw["enabled"] or gw["last_seen"] is None:
                return "offline"
            if now - gw["last_seen"] > offline_after_ms:
                return "comm_lost"
            rs = dev["reported_status"]
            base = {"online": "online", "comm_lost": "comm_lost", "error": "comm_lost"}.get(rs, "offline")
        if base == "online" and self.device_has_alarm(dev["id"]):
            return "alarm"
        return base

    def monitor_once(self, now=None):
        now = now or now_ms()
        offline_after = int(self.db.get_setting("gateway_offline_after_s")) * 1000
        gws = {g["id"]: g for g in self.db.q("SELECT * FROM gateways")}
        # gateways
        for g in gws.values():
            seen = g["last_seen"] is not None
            offline = (not seen) or now - g["last_seen"] > offline_after
            if seen and offline and g["id"] not in self._gw_offline:
                self._gw_offline.add(g["id"])
                self.log_event("system", "Gateway '%s' is offline (no heartbeat)" % g["name"], "warning", g["site_id"])
            elif seen and not offline and g["id"] in self._gw_offline:
                self._gw_offline.discard(g["id"])
                self.log_event("system", "Gateway '%s' is back online" % g["name"], "info", g["site_id"])
        # devices
        for dev in self.db.q("SELECT * FROM devices WHERE archived=0"):
            st = self.effective_status(dev, gws.get(dev["gateway_id"]), now, offline_after)
            comm_bad = st == "comm_lost"
            key = (dev["id"], 0, "comm_lost")
            with self.db.tx():
                if comm_bad and key not in self._active:
                    a = self._raise(dev["site_id"], dev["id"], None, "comm_lost", "high",
                                    "%s: communication lost%s" % (dev["name"], (" (" + dev["last_error"] + ")") if dev["last_error"] else ""),
                                    None, now)
                    self.notify(a)
                elif not comm_bad and key in self._active:
                    self._clear(key, now)
                if st != dev["status"]:
                    self.db.execute("UPDATE devices SET status=?, status_ts=? WHERE id=?", (st, now, dev["id"]))
                    if {st, dev["status"]} - {"online", "alarm"}:  # alarm<->online flips are logged as alarms
                        sev = "warning" if st in ("comm_lost",) else "info"
                        self.log_event("device", "Device '%s' is now %s" % (dev["name"], st.replace("_", " ")), sev,
                                       dev["site_id"], dev["id"])
        self.db.execute("UPDATE gateway_commands SET status='expired', done_at=? WHERE status IN ('pending','sent') AND created_at<?",
                        (now, now - 120_000))

    # ------------------------------------------------------------ simulated devices
    def simulate_once(self, now=None):
        now = now or now_ms()
        t = now / 1000.0
        devs = self.db.q("SELECT id FROM devices WHERE source='simulated' AND enabled=1 AND archived=0")
        if not devs:
            return
        ids = {d["id"] for d in devs}
        items = []
        for tag in self._tag_cache().values():
            if tag["device_id"] in ids and tag["enabled"] and tag["sim"]:
                try:
                    sim = json.loads(tag["sim"])
                except ValueError:
                    continue
                v = sim_value(sim, t, tag["id"])
                if tag["data_type"] == "bool":
                    v = 1.0 if v >= 0.5 else 0.0
                items.append((tag["id"], now, v, "good"))
        self.ingest_batch(items, now)
        with self.db.tx():
            for d in ids:
                self.db.execute("UPDATE devices SET reported_status='online', last_comm_ts=?, last_error='' WHERE id=?", (now, d))

    # ------------------------------------------------------------ gateway support
    def bump_gateway_config(self, gateway_id):
        if gateway_id:
            self.db.execute("UPDATE gateways SET config_version=config_version+1 WHERE id=?", (gateway_id,))

    def gateway_config(self, gw):
        devices = []
        for d in self.db.q("SELECT * FROM devices WHERE gateway_id=? AND source='gateway' AND archived=0", (gw["id"],)):
            conn = json.loads(d["connection"] or "{}")
            sec = self.db.one("SELECT secret FROM device_secrets WHERE device_id=?", (d["id"],))
            secrets_ = self.vault.unseal(sec["secret"]) if sec else {}
            tags = self.db.q("SELECT id,name,table_type,address,address_ref,data_type,byte_order,bit,scale,offset,"
                             "poll_interval_ms,log_interval_s,access,write_min,write_max FROM tags WHERE device_id=? AND enabled=1", (d["id"],))
            devices.append({"id": d["id"], "name": d["name"], "protocol": d["protocol"], "enabled": bool(d["enabled"]),
                            "connection": conn, "credentials": secrets_, "tags": tags})
        return {"gateway_id": gw["id"], "version": gw["config_version"], "devices": devices}

    def queue_command(self, gateway_id, device_id, command, username, payload=None):
        cur = self.db.execute(
            "INSERT INTO gateway_commands(gateway_id,device_id,command,requested_by,created_at,payload) VALUES(?,?,?,?,?,?)",
            (gateway_id, device_id, command, username, now_ms(), json.dumps(payload) if payload is not None else None))
        return cur.lastrowid

    def take_commands(self, gateway_id):
        rows = self.db.q("SELECT id, device_id, command, payload FROM gateway_commands WHERE gateway_id=? AND status='pending'", (gateway_id,))
        for r in rows:
            self.db.execute("UPDATE gateway_commands SET status='sent' WHERE id=?", (r["id"],))
            r["payload"] = json.loads(r["payload"]) if r["payload"] else None
        return rows

    def command_result(self, gateway_id, cmd_id, ok, message, detail):
        row = self.db.one("SELECT c.*, d.site_id, d.name AS dname FROM gateway_commands c JOIN devices d ON d.id=c.device_id "
                          "WHERE c.id=? AND c.gateway_id=?", (cmd_id, gateway_id))
        if row and row["command"] == "write":
            self.log_event("audit", "WRITE %s on '%s' (requested by %s): %s" % ("CONFIRMED" if ok else "FAILED", row["dname"], row["requested_by"], message),
                           "info" if ok else "error", row["site_id"], row["device_id"], None, row["requested_by"], detail)
        self.db.execute(
            "UPDATE gateway_commands SET status=?, result=?, done_at=? WHERE id=? AND gateway_id=?",
            ("done" if ok else "failed", json.dumps({"ok": bool(ok), "message": message, "detail": detail}), now_ms(),
             cmd_id, gateway_id))

    # ------------------------------------------------------------ notifications
    def notify(self, alarm):
        try:
            chans = self.db.q("SELECT * FROM notification_channels WHERE enabled=1 AND (site_id IS NULL OR site_id=?)",
                              (alarm["site_id"],))
        except Exception:
            log.exception("notification lookup failed")
            return
        for ch in chans:
            if PRIORITY_RANK[alarm["priority"]] >= PRIORITY_RANK[ch["min_priority"]]:
                threading.Thread(target=self._send, args=(ch, alarm), daemon=True).start()

    def _send(self, ch, alarm):
        cfg = json.loads(ch["config"] or "{}")
        try:
            if ch["type"] == "webhook":
                body = json.dumps({"product": "TBox", "event": "alarm_raised", "alarm": alarm}).encode()
                req = urllib.request.Request(cfg["url"], data=body, headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=8).read()
            else:
                msg = EmailMessage()
                msg["Subject"] = "[TBox %s] %s" % (alarm["priority"].upper(), alarm["message"][:120])
                msg["From"] = cfg.get("from", "tbox@localhost")
                msg["To"] = ", ".join(cfg.get("to", []))
                msg.set_content(alarm["message"])
                with smtplib.SMTP(cfg["host"], int(cfg.get("port", 587)), timeout=10) as s:
                    if cfg.get("starttls", True):
                        s.starttls()
                    if cfg.get("username"):
                        s.login(cfg["username"], cfg.get("password", ""))
                    s.send_message(msg)
            self.log_event("system", "Notification sent via '%s'" % ch["name"], "info", alarm["site_id"], alarm["device_id"])
        except Exception as e:
            log.warning("notification via %s failed: %s", ch["name"], e)
            self.log_event("system", "Notification via '%s' FAILED: %s" % (ch["name"], e), "error", alarm["site_id"], alarm["device_id"])

    # ------------------------------------------------------------ retention & backups
    def purge_old(self, now=None):
        now = now or now_ms()
        day = 86400_000
        s = self.db.execute("DELETE FROM samples WHERE ts < ?", (now - int(self.db.get_setting("retention_samples_days")) * day,)).rowcount
        e = self.db.execute("DELETE FROM events WHERE ts < ?", (now - int(self.db.get_setting("retention_events_days")) * day,)).rowcount
        a = self.db.execute("DELETE FROM alarms WHERE active=0 AND raised_ts < ?",
                            (now - int(self.db.get_setting("retention_alarms_days")) * day,)).rowcount
        if s or e or a:
            self.log_event("system", "Retention purge removed %d samples, %d events, %d alarms" % (s, e, a))
        return s, e, a

    def backup_now(self, reason="manual"):
        self.cfg.backup_dir.mkdir(parents=True, exist_ok=True)
        name = "tbox-%s.db" % datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = self.cfg.backup_dir / name
        self.db.backup_to(path)
        keep = int(self.db.get_setting("backup_keep"))
        files = sorted(self.cfg.backup_dir.glob("tbox-*.db"))
        for old in files[:-keep] if keep > 0 else []:
            old.unlink(missing_ok=True)
        self.log_event("system", "Database backup created (%s): %s" % (reason, name))
        return {"name": name, "size": path.stat().st_size}

    def list_backups(self):
        out = []
        for f in sorted(self.cfg.backup_dir.glob("tbox-*.db"), reverse=True):
            st = f.stat()
            out.append({"name": f.name, "size": st.st_size, "modified": int(st.st_mtime * 1000)})
        return out

    def maintenance_once(self, now=None):
        now = now or now_ms()
        if now - self._last_purge > 86400_000:
            self.purge_old(now)
            self._last_purge = now
        if self.db.get_setting("backup_enabled"):
            latest = self.list_backups()
            if not latest or now - latest[0]["modified"] > 86400_000:
                self.backup_now("scheduled")

    # ------------------------------------------------------------ lifecycle
    def start(self):
        def loop(fn, period, name):
            def run():
                while not self._stop.wait(period):
                    try:
                        fn()
                    except Exception:
                        log.exception("%s loop error", name)
            t = threading.Thread(target=run, daemon=True, name=name)
            t.start()
            self._threads.append(t)
        if self.cfg.run_simulator:
            loop(self.simulate_once, 1.0, "simulator")
        loop(self.monitor_once, 2.0, "monitor")
        loop(self.maintenance_once, 300.0, "maintenance")
        self.log_event("system", "TBox server started")

    def stop(self):
        self._stop.set()


def _fmt(v):
    return ("%.3f" % v).rstrip("0").rstrip(".") if isinstance(v, float) else str(v)
