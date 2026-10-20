"""API: alarms, events, dashboard, reports, gateway (edge) endpoints, health/system."""
import json
import os
import platform
import sys
import time

from . import __version__, security
from . import reports as rpt
from .db import now_ms
from .util import loads, need, site_clause, visible_sites
from .web import HttpError, Response, json_response

PRIOS = ("critical", "high", "medium", "low")
STATUS_MAP = {"online": "online", "comm_lost": "comm_lost", "error": "error", "disabled": "unknown"}


def register(app):
    db, eng, cfg = app.db, app.engine, app.cfg
    R = app.router.route

    # ------------------------------------------------------------ health
    @R("GET", "/api/health", auth="public")
    def health(req):
        db.scalar("SELECT 1")
        return {"ok": True, "product": "TBox", "version": __version__, "time": now_ms()}

    @R("GET", "/api/system", roles=("admin",))
    def system(req):
        gws = db.q("SELECT last_seen FROM gateways")
        off = int(db.get_setting("gateway_offline_after_s")) * 1000
        return {"version": __version__, "python": sys.version.split()[0], "platform": platform.platform(),
                "uptime_s": int(time.time() - eng.started_at),
                "db_size_bytes": os.path.getsize(cfg.db_path),
                "samples": db.scalar("SELECT COUNT(*) FROM samples", (), 0),
                "tags": db.scalar("SELECT COUNT(*) FROM tags", (), 0),
                "devices": db.scalar("SELECT COUNT(*) FROM devices WHERE archived=0", (), 0),
                "gateways_total": len(gws),
                "gateways_online": sum(1 for g in gws if g["last_seen"] and now_ms() - g["last_seen"] <= off),
                "credential_encryption": app.vault.encrypted,
                "simulator_enabled": cfg.run_simulator,
                "remote_control_enabled": bool(db.get_setting("control_enabled")),
                "latest_backup": (eng.list_backups() or [None])[0],
                "warnings": ([] if app.vault.encrypted else
                             ["Device credentials are stored without encryption because the 'cryptography' package is not installed."])
                            + (["The server is bound to all interfaces without TLS awareness. Put it behind an HTTPS reverse proxy."]
                               if cfg.host in ("0.0.0.0", "::") and not cfg.trust_proxy else [])}

    # ------------------------------------------------------------ alarms
    @R("GET", "/api/alarms")
    def alarms(req):
        cl, p = site_clause(visible_sites(db, req.user), "a.site_id")
        where = cl
        state = req.query.get("state", "active")
        if state == "active":
            where += " AND (a.active=1 OR a.acked_ts IS NULL)"
        if req.int_q("site_id"):
            where += " AND a.site_id=?"
            p.append(req.int_q("site_id"))
        if req.int_q("device_id"):
            where += " AND a.device_id=?"
            p.append(req.int_q("device_id"))
        if req.query.get("priority") in PRIOS:
            where += " AND a.priority=?"
            p.append(req.query["priority"])
        limit = max(1, min(req.int_q("limit", 200), 1000))
        rows = db.q(
            "SELECT a.*, d.name AS device_name, d.source, t.name AS tag_name, t.unit, s.name AS site_name FROM alarms a "
            "JOIN devices d ON d.id=a.device_id JOIN sites s ON s.id=a.site_id LEFT JOIN tags t ON t.id=a.tag_id "
            "WHERE %s ORDER BY a.raised_ts DESC LIMIT %d" % (where, limit), p)
        for r in rows:
            r["active"] = bool(r["active"])
            r["simulated"] = r["source"] == "simulated"
        return rows

    def ack_allowed(req, alarm_id):
        a = db.one("SELECT site_id FROM alarms WHERE id=?", (alarm_id,))
        ids = visible_sites(db, req.user)
        if not a or (ids is not None and a["site_id"] not in ids):
            raise HttpError(404, "Not found")

    @R("POST", "/api/alarms/<int:id>/ack")
    def alarm_ack(req):
        ack_allowed(req, req.params["id"])
        return {"ok": eng.ack_alarm(req.params["id"], req.user["username"])}

    @R("POST", "/api/alarms/ack-all")
    def alarm_ack_all(req):
        cl, p = site_clause(visible_sites(db, req.user), "site_id")
        n = 0
        for a in db.q("SELECT id FROM alarms WHERE acked_ts IS NULL AND %s" % cl, p):
            n += 1 if eng.ack_alarm(a["id"], req.user["username"]) else 0
        return {"acknowledged": n}

    # ------------------------------------------------------------ events
    @R("GET", "/api/events")
    def events(req):
        ids = visible_sites(db, req.user)
        cl, p = site_clause(ids, "site_id")
        where = cl
        if req.user["role"] == "admin" and ids is None:
            where = "(%s OR site_id IS NULL)" % cl
        elif req.user["role"] == "admin":
            where = "(%s OR site_id IS NULL)" % cl
        if req.user["role"] != "admin":
            where += " AND category<>'security'"
        for f in ("category", "severity"):
            if req.query.get(f):
                where += " AND %s=?" % f
                p.append(req.query[f])
        if req.int_q("device_id"):
            where += " AND device_id=?"
            p.append(req.int_q("device_id"))
        if req.int_q("site_id"):
            where += " AND site_id=?"
            p.append(req.int_q("site_id"))
        if req.int_q("before_id"):
            where += " AND id<?"
            p.append(req.int_q("before_id"))
        limit = max(1, min(req.int_q("limit", 100), 500))
        return db.q("SELECT * FROM events WHERE %s ORDER BY id DESC LIMIT %d" % (where, limit), p)

    # ------------------------------------------------------------ dashboard
    @R("GET", "/api/dashboard")
    def dashboard(req):
        ids = visible_sites(db, req.user)
        cl, p = site_clause(ids, "d.site_id")
        counts = {"online": 0, "offline": 0, "alarm": 0, "comm_lost": 0}
        for r in db.q("SELECT d.status, COUNT(*) n FROM devices d WHERE d.archived=0 AND %s GROUP BY d.status" % cl, p):
            counts[r["status"]] = r["n"]
        cla, pa = site_clause(ids, "a.site_id")
        prio = {k: 0 for k in PRIOS}
        for r in db.q("SELECT a.priority, COUNT(*) n FROM alarms a WHERE a.active=1 AND %s GROUP BY a.priority" % cla, pa):
            prio[r["priority"]] = r["n"]
        unacked = db.scalar("SELECT COUNT(*) FROM alarms a WHERE a.acked_ts IS NULL AND %s" % cla, pa, 0)
        cls, ps = site_clause(ids, "s.id")
        sites = db.q("SELECT s.id, s.name, c.name AS company_name FROM sites s JOIN companies c ON c.id=s.company_id WHERE %s ORDER BY c.name, s.name" % cls, ps)
        for s in sites:
            s["devices"] = {"online": 0, "offline": 0, "alarm": 0, "comm_lost": 0}
            for r in db.q("SELECT status, COUNT(*) n FROM devices WHERE site_id=? AND archived=0 GROUP BY status", (s["id"],)):
                s["devices"][r["status"]] = r["n"]
            s["active_alarms"] = db.scalar("SELECT COUNT(*) FROM alarms WHERE site_id=? AND active=1", (s["id"],), 0)
        clg, pg = site_clause(ids, "site_id")
        off = int(db.get_setting("gateway_offline_after_s")) * 1000
        gws = db.q("SELECT last_seen FROM gateways WHERE %s" % clg, pg)
        sim = db.scalar("SELECT COUNT(*) FROM devices d WHERE d.archived=0 AND d.source='simulated' AND %s" % cl, p, 0)
        recent = db.q(
            "SELECT a.id,a.priority,a.message,a.raised_ts,a.active,a.acked_ts FROM alarms a WHERE %s ORDER BY a.raised_ts DESC LIMIT 6" % cla, pa)
        return {"devices": counts, "total_devices": sum(counts.values()), "alarms": prio, "unacked": unacked, "sites": sites,
                "gateways": {"total": len(gws), "online": sum(1 for g in gws if g["last_seen"] and now_ms() - g["last_seen"] <= off)},
                "simulated_devices": sim, "recent_alarms": recent}

    @R("GET", "/api/dashboard/layout")
    def layout_get(req):
        r = db.one("SELECT layout FROM user_dashboards WHERE user_id=?", (req.user["id"],))
        return loads(r["layout"], {"widgets": []}) if r else {"widgets": []}

    @R("PUT", "/api/dashboard/layout")
    def layout_put(req):
        widgets = req.json().get("widgets", [])
        if not isinstance(widgets, list) or len(widgets) > 60:
            raise HttpError(400, "widgets must be a list of at most 60 items")
        clean = []
        for w in widgets:
            if w.get("type") not in ("gauge", "value", "trend", "status", "bar") or not isinstance(w.get("tag_id"), int):
                raise HttpError(400, "Invalid widget")
            clean.append({"type": w["type"], "tag_id": w["tag_id"]})
        db.execute("INSERT INTO user_dashboards(user_id,layout,updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET layout=excluded.layout, updated_at=excluded.updated_at",
                   (req.user["id"], json.dumps({"widgets": clean}), now_ms()))
        return {"ok": True}

    # ------------------------------------------------------------ reports
    def parse_range(req):
        now = now_ms()
        to = req.float_q("to", now)
        frm = req.float_q("from", to - 24 * 3600_000)
        if to <= frm:
            raise HttpError(400, "'to' must be after 'from'")
        if to - frm > 5 * 366 * 86400_000:
            raise HttpError(400, "Range is too large (max 5 years)")
        period = req.query.get("period", "day")
        if period not in rpt.PERIOD_FMT:
            raise HttpError(400, "period must be hour, day, month or year")
        tz = max(-840, min(req.int_q("tz", 0), 840))
        return frm, to, period, tz

    def visible_tag(req, tag_id):
        t = db.one("SELECT t.*, d.site_id, d.name AS device_name, d.source FROM tags t JOIN devices d ON d.id=t.device_id WHERE t.id=?", (tag_id,))
        ids = visible_sites(db, req.user)
        if not t or (ids is not None and t["site_id"] not in ids):
            raise HttpError(404, "Not found")
        return t

    @R("GET", "/api/reports/summary")
    def report_summary(req):
        frm, to, period, tz = parse_range(req)
        t = visible_tag(req, req.int_q("tag_id"))
        return {"tag": {"id": t["id"], "name": t["name"], "unit": t["unit"], "device": t["device_name"], "aggregation": t["aggregation"],
                        "simulated": t["source"] == "simulated"},
                "period": period, "rows": rpt.summary_rows(db, t, frm, to, period, tz)}

    def consumption_rows(req, frm, to, period, tz):
        cl, p = site_clause(visible_sites(db, req.user), "d.site_id")
        where = cl + " AND d.archived=0 AND t.aggregation IN ('counter','level')"
        if req.int_q("site_id"):
            where += " AND d.site_id=?"
            p.append(req.int_q("site_id"))
        if req.int_q("device_id"):
            where += " AND d.id=?"
            p.append(req.int_q("device_id"))
        if req.query.get("category"):
            where += " AND t.category=?"
            p.append(req.query["category"])
        rows = []
        for t in db.q("SELECT t.id,t.name,t.unit,t.aggregation,t.category,d.name AS device_name,d.source FROM tags t JOIN devices d ON d.id=t.device_id WHERE %s ORDER BY d.name,t.name" % where, p):
            acc = rpt.consumption_by_period(db, t["id"], t["aggregation"], frm, to, period, tz)
            for label in sorted(acc):
                rows.append({"period": label, "device": t["device_name"], "tag": t["name"], "unit": t["unit"],
                             "type": "Counter" if t["aggregation"] == "counter" else "Fuel/level drop",
                             "consumption": acc[label], "simulated": t["source"] == "simulated"})
        return rows

    @R("GET", "/api/reports/consumption")
    def report_consumption(req):
        frm, to, period, tz = parse_range(req)
        return {"period": period, "rows": consumption_rows(req, frm, to, period, tz)}

    @R("GET", "/api/reports/export")
    def report_export(req):
        kind = req.query.get("kind", "")
        fmt = req.query.get("format", "csv")
        frm, to, period, tz = parse_range(req)
        meta = [("Generated (UTC):", rpt.iso(now_ms())), ("From (UTC):", rpt.iso(frm)), ("To (UTC):", rpt.iso(to)),
                ("Time zone offset applied to periods (min):", str(tz))]
        if kind == "history":
            ids = [int(x) for x in req.query.get("tag_ids", "").split(",") if x.strip().isdigit()][:20]
            if not ids:
                raise HttpError(400, "tag_ids is required")
            cols = ["Timestamp (UTC)", "Device", "Tag", "Value", "Unit", "Quality", "Source"]
            rows = []
            for tid in ids:
                t = visible_tag(req, tid)
                for ts, v, q in db.conn().execute("SELECT ts,value,quality FROM samples WHERE tag_id=? AND ts>=? AND ts<=? ORDER BY ts LIMIT 200000",
                                                  (tid, int(frm), int(to))):
                    rows.append([rpt.iso(ts), t["device_name"], t["name"], v, t["unit"], q, "SIMULATED" if t["source"] == "simulated" else "Device"])
            title = "Historical data"
        elif kind == "alarms":
            cl, p = site_clause(visible_sites(db, req.user), "a.site_id")
            where = cl + " AND a.raised_ts>=? AND a.raised_ts<=?"
            p += [int(frm), int(to)]
            if req.int_q("site_id"):
                where += " AND a.site_id=?"
                p.append(req.int_q("site_id"))
            cols = ["Raised (UTC)", "Priority", "Site", "Device", "Tag", "Type", "Message", "Value", "Cleared (UTC)", "Acknowledged (UTC)", "Acknowledged by", "Source"]
            rows = [[rpt.iso(a["raised_ts"]), a["priority"], a["site_name"], a["device_name"], a["tag_name"] or "", a["kind"], a["message"], a["value"],
                     rpt.iso(a["cleared_ts"]), rpt.iso(a["acked_ts"]), a["acked_by"] or "", "SIMULATED" if a["source"] == "simulated" else "Device"]
                    for a in db.q("SELECT a.*, d.name AS device_name, d.source, t.name AS tag_name, s.name AS site_name FROM alarms a "
                                  "JOIN devices d ON d.id=a.device_id JOIN sites s ON s.id=a.site_id LEFT JOIN tags t ON t.id=a.tag_id "
                                  "WHERE %s ORDER BY a.raised_ts LIMIT 50000" % where, p)]
            title = "Alarm history"
        elif kind == "consumption":
            cols = ["Period", "Device", "Tag", "Unit", "Calculation", "Consumption / production", "Source"]
            rows = [[r["period"], r["device"], r["tag"], r["unit"], r["type"], round(r["consumption"], 4),
                     "SIMULATED" if r["simulated"] else "Device"] for r in consumption_rows(req, frm, to, period, tz)]
            title = "Energy / fuel consumption per %s" % period
            meta.append(("Method:", "Counters: sum of positive increments. Fuel level: sum of drops (refuelling ignored). Good-quality readings only."))
        elif kind == "summary":
            t = visible_tag(req, req.int_q("tag_id"))
            cols = ["Period", "Samples", "Min", "Average", "Max", "Consumption"] 
            rows = [[r["period"], r["samples"], r["min"], r["avg"], r["max"], r["consumption"]] for r in rpt.summary_rows(db, t, frm, to, period, tz)]
            title = "%s / %s (%s) per %s" % (t["device_name"], t["name"], t["unit"], period)
            if t["source"] == "simulated":
                meta.append(("DATA SOURCE:", "SIMULATED"))
        else:
            raise HttpError(400, "kind must be history, alarms, consumption or summary")
        data, ctype, ext = rpt.render(fmt, title, cols, rows, meta)
        eng.log_event("audit", "Report exported: %s (%s, %d rows)" % (kind, fmt, len(rows)), username=req.user["username"])
        return Response(data, 200, ctype, {"Content-Disposition": 'attachment; filename="tbox-%s-%s.%s"' % (kind, time.strftime("%Y%m%d-%H%M%S"), ext)})

    # ------------------------------------------------------------ gateway (edge) API
    @R("GET", "/api/gateway/config", auth="gateway")
    def gw_config(req):
        gw = req.gateway
        db.execute("UPDATE gateways SET last_seen=? WHERE id=?", (now_ms(), gw["id"]))
        have = req.int_q("version", -1)
        out = {"version": gw["config_version"], "server_time": now_ms(), "changed": have != gw["config_version"]}
        if out["changed"]:
            out.update(eng.gateway_config(gw))
        return out

    @R("POST", "/api/gateway/ingest", auth="gateway")
    def gw_ingest(req):
        gw = req.gateway
        b = req.json()
        now = now_ms()
        gi = b.get("gateway") or {}
        sent = b.get("sent_at")
        skew = int(sent - now) if isinstance(sent, (int, float)) else None
        db.execute("UPDATE gateways SET last_seen=?, version=?, host_info=?, protocols=?, buffer_depth=?, clock_skew_ms=? WHERE id=?",
                   (now, str(gi.get("version", ""))[:40], str(gi.get("host", ""))[:200], json.dumps(gi.get("protocols", [])[:20]),
                    int(gi.get("buffer_depth", 0) or 0), skew, gw["id"]))
        devs = {d["id"] for d in db.q("SELECT id FROM devices WHERE gateway_id=? AND archived=0", (gw["id"],))}
        items = []
        for s in (b.get("samples") or [])[:5000]:
            try:
                items.append((int(s["tag_id"]), int(s["ts"]), s.get("value"), s.get("quality", "good")))
            except (KeyError, TypeError, ValueError):
                continue
        accepted, rejected = eng.ingest_batch(items, now, allowed_device_ids=devs)
        with db.tx():
            for st in (b.get("device_status") or [])[:500]:
                did = st.get("device_id")
                if did not in devs:
                    continue
                rs = STATUS_MAP.get(st.get("status"), "unknown")
                diag = {k: st.get(k) for k in ("latency_ms", "stats", "since", "driver") if st.get(k) is not None}
                db.execute("UPDATE devices SET reported_status=?, last_error=?, diagnostics=?, last_comm_ts=CASE WHEN ?='online' THEN ? ELSE last_comm_ts END WHERE id=?",
                           (rs, str(st.get("last_error") or "")[:300], json.dumps(diag), rs, now, did))
            for cr in (b.get("command_results") or [])[:100]:
                eng.command_result(gw["id"], cr.get("id"), cr.get("ok"), str(cr.get("message", ""))[:500], cr.get("detail"))
        return {"ok": True, "accepted": accepted, "rejected": rejected, "config_version": gw["config_version"],
                "commands": eng.take_commands(gw["id"]), "server_time": now}
