"""API: device manager, tag manager (CSV import/export), profiles, live values, history."""
import csv
import io
import json
import re

from tbox_modbus.codec import BYTE_ORDERS, DATA_TYPES

from .db import now_ms
from .util import loads, need, require_site, site_clause, visible_sites
from .web import HttpError, Response, json_response

WRITE_ROLES = ("admin", "engineer")
PROTOCOLS = ("modbus_tcp", "modbus_rtu", "mqtt", "opcua")
CATEGORIES = ("plc", "energy_meter", "generator", "solar_inverter", "vfd", "pump", "motor", "sensor",
              "controller", "gateway_device", "other")
HOST_RE = re.compile(r"^[A-Za-z0-9._:\-\[\]]{1,253}$")
BAUDS = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200)
TABLES = ("coil", "discrete", "holding", "input")
CSV_COLUMNS = ["name", "description", "category", "unit", "table_type", "address", "address_ref", "data_type",
               "byte_order", "bit", "scale", "offset", "poll_interval_ms", "access", "aggregation",
               "log_interval_s", "disp_min", "disp_max", "lolo", "lo", "hi", "hihi", "deadband",
               "alarm_priority", "alarm_enabled", "write_min", "write_max"]
SIM_KINDS = ("const", "sine", "counter", "square", "saw", "daylight")


def _opt_float(v, name):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise HttpError(400, "'%s' must be a number" % name)


def _int_range(v, name, lo, hi, default):
    if v in (None, ""):
        return default
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise HttpError(400, "'%s' must be an integer" % name)
    if not lo <= v <= hi:
        raise HttpError(400, "'%s' must be between %d and %d" % (name, lo, hi))
    return v


def clean_connection(protocol, c):
    c = c or {}
    out = {"poll_interval_ms": _int_range(c.get("poll_interval_ms"), "poll_interval_ms", 100, 3_600_000, 1000),
           "timeout_ms": _int_range(c.get("timeout_ms"), "timeout_ms", 100, 60_000, 3000),
           "retries": _int_range(c.get("retries"), "retries", 0, 10, 2)}
    if protocol in ("modbus_tcp", "mqtt", "opcua") and protocol != "opcua":
        host = str(c.get("host", "")).strip()
        if not HOST_RE.match(host):
            raise HttpError(400, "A valid host name or IP address is required")
        out["host"] = host
        out["port"] = _int_range(c.get("port"), "port", 1, 65535, 502 if protocol == "modbus_tcp" else 1883)
    if protocol == "modbus_tcp":
        out["slave_id"] = _int_range(c.get("slave_id"), "slave_id", 0, 247, 1)
    elif protocol == "modbus_rtu":
        sp = str(c.get("serial_port", "")).strip()
        if not sp or len(sp) > 100:
            raise HttpError(400, "Serial port is required (e.g. COM3 or /dev/ttyUSB0 on the gateway)")
        out["serial_port"] = sp
        out["baud_rate"] = _int_range(c.get("baud_rate"), "baud_rate", 1200, 115200, 9600)
        if out["baud_rate"] not in BAUDS:
            raise HttpError(400, "Baud rate must be one of %s" % ", ".join(map(str, BAUDS)))
        par = str(c.get("parity", "N")).upper()
        if par not in ("N", "E", "O"):
            raise HttpError(400, "Parity must be N, E or O")
        out["parity"] = par
        out["stop_bits"] = _int_range(c.get("stop_bits"), "stop_bits", 1, 2, 1)
        out["data_bits"] = _int_range(c.get("data_bits"), "data_bits", 7, 8, 8)
        out["slave_id"] = _int_range(c.get("slave_id"), "slave_id", 1, 247, 1)
    elif protocol == "mqtt":
        out["tls"] = bool(c.get("tls", False))
        out["client_id"] = str(c.get("client_id", ""))[:100]
        out["base_topic"] = str(c.get("base_topic", ""))[:200]
    elif protocol == "opcua":
        ep = str(c.get("endpoint", "")).strip()
        if not ep.startswith("opc.tcp://"):
            raise HttpError(400, "OPC UA endpoint must start with opc.tcp://")
        out["endpoint"] = ep[:300]
        out["security_policy"] = str(c.get("security_policy", "None"))[:60]
        out["security_mode"] = str(c.get("security_mode", "None"))[:30]
    return out


def clean_tag(b, dev, existing=None):
    proto = dev["protocol"]
    name = need(b, "name", str, 100)
    t = {"name": name,
         "description": need(b, "description", str, 300, False, ""),
         "category": need(b, "category", str, 50, False, ""),
         "unit": need(b, "unit", str, 20, False, "")}
    modbus = proto in ("modbus_tcp", "modbus_rtu")
    if modbus:
        table = b.get("table_type") or "holding"
        if table not in TABLES:
            raise HttpError(400, "table_type must be one of %s" % ", ".join(TABLES))
        dt = "bool" if table in ("coil", "discrete") else (b.get("data_type") or "uint16")
        t["address_ref"] = ""
        t["address"] = _int_range(b.get("address"), "address", 0, 65535, 0)
    else:
        table = "node" if proto == "opcua" else "topic"
        dt = b.get("data_type") or "float64"
        ref = need(b, "address_ref", str, 300)
        t["address_ref"], t["address"] = ref, 0
    if dt not in DATA_TYPES:
        raise HttpError(400, "data_type must be one of %s" % ", ".join(DATA_TYPES))
    t["table_type"], t["data_type"] = table, dt
    order = b.get("byte_order") or "ABCD"
    if order not in BYTE_ORDERS:
        raise HttpError(400, "byte_order must be one of %s" % ", ".join(BYTE_ORDERS))
    t["byte_order"] = order
    bit = b.get("bit")
    if bit in (None, ""):
        t["bit"] = None
    else:
        if not (modbus and table in ("holding", "input") and dt == "bool"):
            raise HttpError(400, "Bit index is only valid for Boolean tags read from a holding/input register")
        t["bit"] = _int_range(bit, "bit", 0, 15, None)
    scale = _opt_float(b.get("scale"), "scale")
    t["scale"] = 1.0 if scale is None else scale
    if t["scale"] == 0:
        raise HttpError(400, "scale cannot be 0")
    off = _opt_float(b.get("offset"), "offset")
    t["offset"] = 0.0 if off is None else off
    t["poll_interval_ms"] = _int_range(b.get("poll_interval_ms"), "poll_interval_ms", 100, 3_600_000, None)
    access = b.get("access") or "R"
    if access not in ("R", "RW"):
        raise HttpError(400, "access must be R or RW")
    if access == "RW" and (not modbus or table not in ("coil", "holding")):
        raise HttpError(400, "Only coil and holding-register tags can be marked read/write")
    t["access"] = access
    agg = b.get("aggregation") or "avg"
    if agg not in ("avg", "counter", "level"):
        raise HttpError(400, "aggregation must be avg, counter or level")
    t["aggregation"] = agg
    t["log_interval_s"] = _int_range(b.get("log_interval_s"), "log_interval_s", 1, 86400, 60)
    for f in ("disp_min", "disp_max", "lolo", "lo", "hi", "hihi"):
        t[f] = _opt_float(b.get(f), f)
    chain = [(f, t[f]) for f in ("lolo", "lo", "hi", "hihi") if t[f] is not None]
    for (fa, va), (fb, vb) in zip(chain, chain[1:]):
        if va > vb:
            raise HttpError(400, "Alarm limits must satisfy lolo <= lo <= hi <= hihi (%s > %s)" % (fa, fb))
    t["write_min"], t["write_max"] = _opt_float(b.get("write_min"), "write_min"), _opt_float(b.get("write_max"), "write_max")
    if t["write_min"] is not None and t["write_max"] is not None and t["write_min"] > t["write_max"]:
        raise HttpError(400, "write_min cannot be greater than write_max")
    db_ = _opt_float(b.get("deadband"), "deadband")
    t["deadband"] = 0.0 if db_ is None else abs(db_)
    prio = b.get("alarm_priority") or "medium"
    if prio not in ("low", "medium", "high", "critical"):
        raise HttpError(400, "alarm_priority must be low, medium, high or critical")
    t["alarm_priority"] = prio
    ae = b.get("alarm_enabled", True)
    t["alarm_enabled"] = 0 if str(ae).lower() in ("0", "false", "no", "") else 1
    t["enabled"] = 0 if b.get("enabled") in (0, False, "0", "false") else 1
    sim = b.get("sim")
    if sim and dev["source"] == "simulated":
        if not isinstance(sim, dict) or sim.get("kind") not in SIM_KINDS:
            raise HttpError(400, "sim.kind must be one of %s" % ", ".join(SIM_KINDS))
        t["sim"] = json.dumps({k: v for k, v in sim.items() if isinstance(v, (int, float, str))})
    else:
        t["sim"] = None
    return t


TAG_FIELDS = ["name", "description", "category", "unit", "table_type", "address", "address_ref", "data_type",
              "byte_order", "bit", "scale", "offset", "poll_interval_ms", "access", "aggregation", "log_interval_s",
              "disp_min", "disp_max", "lolo", "lo", "hi", "hihi", "deadband", "alarm_priority", "alarm_enabled",
              "enabled", "sim", "write_min", "write_max"]


def register(app):
    db, eng = app.db, app.engine
    R = app.router.route

    def get_device(req, dev_id=None):
        d = db.one("SELECT * FROM devices WHERE id=?", (dev_id if dev_id is not None else req.params["id"],))
        if not d:
            raise HttpError(404, "Not found")
        require_site(db, req.user, d["site_id"])
        return d

    def device_view(d, with_secrets=True):
        d = dict(d)
        d["connection"] = loads(d["connection"], {})
        d["diagnostics"] = loads(d["diagnostics"], {})
        d["simulated"] = d["source"] == "simulated"
        d["enabled"] = bool(d["enabled"])
        if with_secrets:
            s = db.one("SELECT secret FROM device_secrets WHERE device_id=?", (d["id"],))
            if s:
                sec = app.vault.unseal(s["secret"])
                d["credentials"] = {"username": sec.get("username", ""), "has_password": bool(sec.get("password")),
                                    "has_certificate": bool(sec.get("certificate")), "has_token": bool(sec.get("token"))}
        return d

    def store_credentials(device_id, creds, keep_existing):
        if creds is None:
            return
        old = {}
        s = db.one("SELECT secret FROM device_secrets WHERE device_id=?", (device_id,))
        if s and keep_existing:
            old = app.vault.unseal(s["secret"])
        new = dict(old)
        for k in ("username", "password", "certificate", "token"):
            v = creds.get(k)
            if k == "username" and v is not None:
                new[k] = str(v)[:200]
            elif v:  # blank secret fields mean "unchanged"
                new[k] = str(v)[:20000]
        if not any(new.values()):
            db.execute("DELETE FROM device_secrets WHERE device_id=?", (device_id,))
            return
        db.execute("INSERT INTO device_secrets(device_id,secret) VALUES(?,?) ON CONFLICT(device_id) DO UPDATE SET secret=excluded.secret",
                   (device_id, app.vault.seal(new)))

    def clean_device(req, b, existing=None):
        site_id = need(b, "site_id", int) if existing is None else existing["site_id"]
        require_site(db, req.user, site_id)
        source = b.get("source") or "gateway"
        if source not in ("gateway", "simulated"):
            raise HttpError(400, "source must be 'gateway' or 'simulated'")
        protocol = b.get("protocol") or ("modbus_tcp")
        if protocol not in PROTOCOLS:
            raise HttpError(400, "protocol must be one of %s" % ", ".join(PROTOCOLS))
        category = b.get("category") or "other"
        if category not in CATEGORIES:
            raise HttpError(400, "category must be one of %s" % ", ".join(CATEGORIES))
        building_id = b.get("building_id") or None
        if building_id and not db.one("SELECT 1 FROM buildings WHERE id=? AND site_id=?", (building_id, site_id)):
            raise HttpError(400, "Building does not belong to this site")
        gateway_id = b.get("gateway_id") or None
        if source == "gateway":
            if not gateway_id:
                raise HttpError(400, "Assign a gateway. Real devices are read by a TBox Edge Gateway, never directly by the web app.")
            if not db.one("SELECT 1 FROM gateways WHERE id=? AND site_id=?", (gateway_id, site_id)):
                raise HttpError(400, "Gateway does not belong to this site")
            connection = clean_connection(protocol, b.get("connection"))
        else:
            gateway_id, connection = None, clean_connection(protocol, {**(b.get("connection") or {}), "host": "simulated"}) \
                if protocol in ("modbus_tcp", "mqtt") else {"poll_interval_ms": 1000, "timeout_ms": 3000, "retries": 0}
        return {"site_id": site_id, "building_id": building_id, "gateway_id": gateway_id,
                "name": need(b, "name"), "description": need(b, "description", str, 500, False, ""),
                "location": need(b, "location", str, 200, False, ""),
                "manufacturer": need(b, "manufacturer", str, 100, False, ""), "model": need(b, "model", str, 100, False, ""),
                "category": category, "profile_id": need(b, "profile_id", str, 100, False, ""),
                "source": source, "protocol": protocol, "connection": json.dumps(connection)}

    def insert_tags(device, tags):
        n = 0
        for tb in tags:
            t = clean_tag(tb, device)
            cols = ["device_id"] + TAG_FIELDS
            db.execute("INSERT INTO tags(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols))),
                       [device["id"]] + [t[f] for f in TAG_FIELDS])
            n += 1
        return n

    # ------------------------------------------------------------ profiles
    @R("GET", "/api/profiles")
    def profiles(req):
        return [{k: p.get(k) for k in ("id", "name", "manufacturer", "model", "category", "protocol", "source",
                                       "status", "notes", "defaults")} | {"tag_count": len(p.get("tags", []))}
                for p in app.profiles.values()]

    @R("GET", "/api/profiles/<str:id>")
    def profile_get(req):
        p = app.profiles.get(req.params["id"])
        if not p:
            raise HttpError(404, "Not found")
        return p

    # ------------------------------------------------------------ devices
    @R("GET", "/api/devices")
    def devices(req):
        cl, p = site_clause(visible_sites(db, req.user), "d.site_id")
        sql = ("SELECT d.*, s.name AS site_name, b.name AS building_name, g.name AS gateway_name, "
               "(SELECT COUNT(*) FROM tags t WHERE t.device_id=d.id) AS tag_count "
               "FROM devices d JOIN sites s ON s.id=d.site_id LEFT JOIN buildings b ON b.id=d.building_id "
               "LEFT JOIN gateways g ON g.id=d.gateway_id WHERE %s" % cl)
        if not req.query.get("include_archived"):
            sql += " AND d.archived=0"
        if req.int_q("site_id"):
            sql += " AND d.site_id=?"
            p.append(req.int_q("site_id"))
        if req.query.get("status"):
            sql += " AND d.status=?"
            p.append(req.query["status"])
        if req.query.get("q"):
            sql += " AND (d.name LIKE ? OR d.model LIKE ? OR d.manufacturer LIKE ?)"
            p += ["%" + req.query["q"] + "%"] * 3
        return [device_view(d, False) for d in db.q(sql + " ORDER BY s.name, d.name", p)]

    @R("POST", "/api/devices", roles=WRITE_ROLES)
    def device_create(req):
        b = req.json()
        v = clean_device(req, b)
        now = now_ms()
        try:
            with db.tx():
                cols = list(v) + ["created_at", "updated_at"]
                cur = db.execute("INSERT INTO devices(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols))),
                                 list(v.values()) + [now, now])
                dev_id = cur.lastrowid
                dev = db.one("SELECT * FROM devices WHERE id=?", (dev_id,))
                store_credentials(dev_id, b.get("credentials"), False)
                ntags = 0
                prof = app.profiles.get(v["profile_id"]) if b.get("apply_profile", True) and v["profile_id"] else None
                if prof:
                    ntags += insert_tags(dev, prof.get("tags", []))
                if b.get("tags"):
                    ntags += insert_tags(dev, b["tags"])
                eng.bump_gateway_config(v["gateway_id"])
        except HttpError:
            raise
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HttpError(409, "A device with this name already exists on the site")
            raise
        eng.invalidate()
        eng.log_event("audit", "Device created: %s (%s)" % (v["name"], v["protocol"]), site_id=v["site_id"],
                      device_id=dev_id, username=req.user["username"])
        return {"id": dev_id, "tags_created": ntags}

    @R("GET", "/api/devices/<int:id>")
    def device_get(req):
        d = get_device(req)
        out = device_view(d)
        out["tag_count"] = db.scalar("SELECT COUNT(*) FROM tags WHERE device_id=?", (d["id"],), 0)
        return out

    @R("PUT", "/api/devices/<int:id>", roles=WRITE_ROLES)
    def device_update(req):
        d = get_device(req)
        b = req.json()
        v = clean_device(req, b, existing=d)
        v.pop("site_id")
        old_gw = d["gateway_id"]
        try:
            with db.tx():
                sets = ",".join("%s=?" % k for k in v) + ",updated_at=?"
                db.execute("UPDATE devices SET %s WHERE id=?" % sets, list(v.values()) + [now_ms(), d["id"]])
                store_credentials(d["id"], b.get("credentials"), True)
                eng.bump_gateway_config(old_gw)
                eng.bump_gateway_config(v["gateway_id"])
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HttpError(409, "A device with this name already exists on the site")
            raise
        eng.invalidate()
        eng.log_event("audit", "Device updated: %s" % v["name"], site_id=d["site_id"], device_id=d["id"], username=req.user["username"])
        return {"ok": True}

    @R("DELETE", "/api/devices/<int:id>", roles=WRITE_ROLES)
    def device_delete(req):
        d = get_device(req)
        purge = req.query.get("purge") == "1"
        has_history = db.scalar("SELECT 1 FROM samples s JOIN tags t ON t.id=s.tag_id WHERE t.device_id=? LIMIT 1", (d["id"],), 0)
        if purge:
            if req.user["role"] != "admin":
                raise HttpError(403, "Only an administrator can permanently delete a device and its history")
            db.execute("DELETE FROM devices WHERE id=?", (d["id"],))
            msg = "Device permanently deleted: %s" % d["name"]
        elif has_history:
            with db.tx():
                db.execute("UPDATE devices SET archived=1, enabled=0, status='offline', updated_at=? WHERE id=?", (now_ms(), d["id"]))
                for k in [k for k in eng._active if k[0] == d["id"]]:
                    eng._clear(k, now_ms())
            msg = "Device archived (history kept): %s" % d["name"]
        else:
            db.execute("DELETE FROM devices WHERE id=?", (d["id"],))
            msg = "Device deleted: %s" % d["name"]
        eng.bump_gateway_config(d["gateway_id"])
        eng.invalidate()
        eng.log_event("audit", msg, site_id=d["site_id"], username=req.user["username"])
        return {"ok": True, "archived": bool(has_history and not purge)}

    @R("POST", "/api/devices/<int:id>/duplicate", roles=WRITE_ROLES)
    def device_duplicate(req):
        d = get_device(req)
        name = need(req.json(), "name", str, 100, False, None) or d["name"] + " (copy)"
        now = now_ms()
        with db.tx():
            cur = db.execute(
                "INSERT INTO devices(site_id,building_id,gateway_id,name,description,location,manufacturer,model,category,profile_id,"
                "source,protocol,connection,enabled,created_at,updated_at) SELECT site_id,building_id,gateway_id,?,description,location,"
                "manufacturer,model,category,profile_id,source,protocol,connection,0,?,? FROM devices WHERE id=?",
                (name, now, now, d["id"]))
            nid = cur.lastrowid
            cols = ",".join(TAG_FIELDS)
            db.execute("INSERT INTO tags(device_id,%s) SELECT ?,%s FROM tags WHERE device_id=?" % (cols, cols), (nid, d["id"]))
            sec = db.one("SELECT secret FROM device_secrets WHERE device_id=?", (d["id"],))
            if sec:
                db.execute("INSERT INTO device_secrets(device_id,secret) VALUES(?,?)", (nid, sec["secret"]))
            eng.bump_gateway_config(d["gateway_id"])
        eng.invalidate()
        eng.log_event("audit", "Device duplicated: %s -> %s (created disabled)" % (d["name"], name), site_id=d["site_id"],
                      device_id=nid, username=req.user["username"])
        return {"id": nid, "note": "The copy is disabled. Review the IP address / slave ID, then press Connect."}

    def _gateway_command(req, command):
        d = get_device(req)
        if d["source"] == "simulated":
            return {"done": True, "ok": True, "message": "Simulated device: no physical connection. Values are generated by TBox."}
        gw = db.one("SELECT * FROM gateways WHERE id=?", (d["gateway_id"],))
        if not gw:
            raise HttpError(409, "No gateway is assigned to this device")
        off_after = int(db.get_setting("gateway_offline_after_s")) * 1000
        if not gw["last_seen"] or now_ms() - gw["last_seen"] > off_after:
            raise HttpError(409, "Gateway '%s' is offline, so the device cannot be reached right now" % gw["name"])
        cid = eng.queue_command(gw["id"], d["id"], command, req.user["username"])
        eng.log_event("audit", "Device %s requested: %s" % (command, d["name"]), site_id=d["site_id"], device_id=d["id"],
                      username=req.user["username"])
        return {"done": False, "command_id": cid}

    @R("POST", "/api/devices/<int:id>/test", roles=WRITE_ROLES)
    def device_test(req):
        return _gateway_command(req, "test")

    @R("POST", "/api/devices/<int:id>/reconnect", roles=WRITE_ROLES)
    def device_reconnect(req):
        return _gateway_command(req, "reconnect")

    @R("POST", "/api/devices/<int:id>/disconnect", roles=WRITE_ROLES)
    def device_disconnect(req):
        d = get_device(req)
        db.execute("UPDATE devices SET enabled=0, status='offline', status_ts=?, reported_status='unknown' WHERE id=?", (now_ms(), d["id"]))
        eng.bump_gateway_config(d["gateway_id"])
        eng.invalidate()
        eng.log_event("device", "Device disconnected by user: %s" % d["name"], site_id=d["site_id"], device_id=d["id"], username=req.user["username"])
        return {"ok": True}

    @R("POST", "/api/devices/<int:id>/connect", roles=WRITE_ROLES)
    def device_connect(req):
        d = get_device(req)
        if d["archived"]:
            raise HttpError(409, "Archived devices cannot be connected")
        db.execute("UPDATE devices SET enabled=1 WHERE id=?", (d["id"],))
        eng.bump_gateway_config(d["gateway_id"])
        eng.invalidate()
        eng.log_event("device", "Device connected by user: %s" % d["name"], site_id=d["site_id"], device_id=d["id"], username=req.user["username"])
        return {"ok": True}

    @R("GET", "/api/commands/<int:id>")
    def command_get(req):
        c = db.one("SELECT * FROM gateway_commands WHERE id=?", (req.params["id"],))
        if not c:
            raise HttpError(404, "Not found")
        get_device(req, c["device_id"])
        return {"id": c["id"], "command": c["command"], "status": c["status"], "result": loads(c["result"]),
                "done": c["status"] in ("done", "failed", "expired")}

    @R("GET", "/api/devices/<int:id>/diagnostics")
    def device_diag(req):
        d = get_device(req)
        out = device_view(d, False)
        gw = db.one("SELECT id,name,last_seen,version,buffer_depth,clock_skew_ms,protocols FROM gateways WHERE id=?", (d["gateway_id"],))
        if gw:
            gw["protocols"] = loads(gw["protocols"], [])
        out["gateway"] = gw
        out["quality"] = {r["q"]: r["n"] for r in db.q(
            "SELECT COALESCE(s.quality,'no_data') AS q, COUNT(*) AS n FROM tags t LEFT JOIN tag_state s ON s.tag_id=t.id "
            "WHERE t.device_id=? GROUP BY q", (d["id"],))}
        out["active_alarms"] = db.q("SELECT id,kind,priority,message,raised_ts FROM alarms WHERE device_id=? AND active=1", (d["id"],))
        out["recent_events"] = db.q("SELECT ts,category,severity,message FROM events WHERE device_id=? ORDER BY id DESC LIMIT 15", (d["id"],))
        out["server_time"] = now_ms()
        return out

    # ------------------------------------------------------------ tags
    def tag_row(req, tag_id=None):
        t = db.one("SELECT t.*, d.site_id, d.protocol, d.source, d.name AS device_name FROM tags t JOIN devices d ON d.id=t.device_id WHERE t.id=?",
                   (tag_id if tag_id is not None else req.params["id"],))
        if not t:
            raise HttpError(404, "Not found")
        require_site(db, req.user, t["site_id"])
        return t

    ALARM_PRECEDENCE = ("hihi", "lolo", "hi", "lo")

    def tag_views(where, params, limit=5000):
        now = now_ms()
        rows = db.q(
            "SELECT t.*, d.name AS device_name, d.site_id, d.source, d.connection, s.value AS v, s.quality AS q, s.ts AS vts "
            "FROM tags t JOIN devices d ON d.id=t.device_id LEFT JOIN tag_state s ON s.tag_id=t.id "
            "WHERE d.archived=0 AND %s ORDER BY d.name, t.name LIMIT %d" % (where, limit), params)
        conns = {}
        out = []
        for r in rows:
            conn = conns.setdefault(r["device_id"], loads(r["connection"], {}))
            poll = r["poll_interval_ms"] or conn.get("poll_interval_ms") or 1000
            q = r["q"] or "bad"
            if q == "good" and now - r["vts"] > max(10_000, 3 * poll):
                q = "stale"
            alarm = next((k for k in ALARM_PRECEDENCE if (r["device_id"], r["id"], k) in eng._active), None)
            r["value"], r["quality"], r["ts"] = r.pop("v"), q, r.pop("vts")
            r.pop("q"), r.pop("connection")
            r["alarm"] = alarm
            r["simulated"] = r["source"] == "simulated"
            r["no_data"] = r["ts"] is None
            out.append(r)
        return out

    @R("GET", "/api/tags")
    def tags(req):
        cl, p = site_clause(visible_sites(db, req.user), "d.site_id")
        where = cl
        if req.int_q("device_id"):
            where += " AND t.device_id=?"
            p.append(req.int_q("device_id"))
        if req.int_q("site_id"):
            where += " AND d.site_id=?"
            p.append(req.int_q("site_id"))
        if req.query.get("q"):
            where += " AND (t.name LIKE ? OR t.description LIKE ?)"
            p += ["%" + req.query["q"] + "%"] * 2
        return tag_views(where, p)

    @R("POST", "/api/tags", roles=WRITE_ROLES)
    def tag_create(req):
        b = req.json()
        d = get_device(req, need(b, "device_id", int))
        t = clean_tag(b, d)
        try:
            cols = ["device_id"] + TAG_FIELDS
            cur = db.execute("INSERT INTO tags(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols))),
                             [d["id"]] + [t[f] for f in TAG_FIELDS])
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HttpError(409, "A tag with this name already exists on the device")
            raise
        eng.bump_gateway_config(d["gateway_id"])
        eng.invalidate()
        eng.log_event("audit", "Tag created: %s / %s" % (d["name"], t["name"]), site_id=d["site_id"], device_id=d["id"],
                      tag_id=cur.lastrowid, username=req.user["username"])
        return {"id": cur.lastrowid}

    @R("PUT", "/api/tags/<int:id>", roles=WRITE_ROLES)
    def tag_update(req):
        old = tag_row(req)
        d = db.one("SELECT * FROM devices WHERE id=?", (old["device_id"],))
        t = clean_tag(req.json(), d, old)
        try:
            db.execute("UPDATE tags SET %s WHERE id=?" % ",".join("%s=?" % f for f in TAG_FIELDS),
                       [t[f] for f in TAG_FIELDS] + [old["id"]])
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HttpError(409, "A tag with this name already exists on the device")
            raise
        changed = [f for f in ("unit", "scale", "offset", "table_type", "address", "data_type", "byte_order") if old[f] != t[f]]
        eng.bump_gateway_config(d["gateway_id"])
        eng.invalidate()
        eng.log_event("audit", "Tag updated: %s / %s%s" % (d["name"], t["name"], (" (changed: %s)" % ", ".join(changed)) if changed else ""),
                      site_id=d["site_id"], device_id=d["id"], tag_id=old["id"], username=req.user["username"])
        return {"ok": True}

    @R("DELETE", "/api/tags/<int:id>", roles=WRITE_ROLES)
    def tag_delete(req):
        t = tag_row(req)
        has = db.scalar("SELECT 1 FROM samples WHERE tag_id=? LIMIT 1", (t["id"],), 0)
        if has and req.query.get("purge") != "1":
            db.execute("UPDATE tags SET enabled=0 WHERE id=?", (t["id"],))
            msg, archived = "Tag disabled (history kept): %s / %s" % (t["device_name"], t["name"]), True
        else:
            if has and req.user["role"] != "admin":
                raise HttpError(403, "Only an administrator can permanently delete a tag and its history")
            db.execute("DELETE FROM tags WHERE id=?", (t["id"],))
            msg, archived = "Tag deleted: %s / %s" % (t["device_name"], t["name"]), False
        dev = db.one("SELECT gateway_id FROM devices WHERE id=?", (t["device_id"],))
        eng.bump_gateway_config(dev["gateway_id"])
        eng.invalidate()
        eng.log_event("audit", msg, site_id=t["site_id"], device_id=t["device_id"], username=req.user["username"])
        return {"ok": True, "disabled": archived}

    @R("POST", "/api/tags/<int:id>/write", roles=WRITE_ROLES)
    def tag_write(req):
        """Controlled write. Every refusal and every request is audited. The gateway re-checks everything locally."""
        t = tag_row(req)
        b = req.json()
        label = "%s / %s" % (t["device_name"], t["name"])

        def refuse(status, msg, code):
            eng.log_event("audit", "BLOCKED write to %s: %s" % (label, msg), "warning", t["site_id"], t["device_id"], t["id"], req.user["username"])
            raise HttpError(status, msg, code=code)
        if not db.get_setting("control_enabled"):
            refuse(403, "Remote control is switched off. An administrator can enable it in Settings. Safety-critical control must stay in the PLC.", "control_disabled")
        if t["access"] != "RW":
            refuse(403, "This tag is read-only. Set its permission to Read/Write in the Tag Manager first.", "tag_read_only")
        if t["table_type"] not in ("coil", "holding"):
            refuse(400, "Only coils and holding registers can be written.", "not_writable")
        if t["bit"] is not None:
            refuse(400, "Writing a single bit of a register is not supported; write the whole register or use a coil.", "not_writable")
        if b.get("confirm") is not True:
            raise HttpError(400, "Confirmation is required to send a write.", code="confirm_required")
        raw = b.get("value")
        if t["data_type"] == "bool":
            if raw in (True, 1, "1", "true", "on", "ON"):
                value = 1.0
            elif raw in (False, 0, "0", "false", "off", "OFF"):
                value = 0.0
            else:
                raise HttpError(400, "Value must be ON or OFF for a Boolean tag")
        else:
            try:
                value = float(raw)
                if isinstance(raw, bool) or value != value or value in (float("inf"), float("-inf")):
                    raise ValueError
            except (TypeError, ValueError):
                raise HttpError(400, "Value must be a number")
            if t["write_min"] is not None and value < t["write_min"]:
                refuse(400, "Value %s is below this tag's allowed minimum (%s)." % (value, t["write_min"]), "out_of_range")
            if t["write_max"] is not None and value > t["write_max"]:
                refuse(400, "Value %s is above this tag's allowed maximum (%s)." % (value, t["write_max"]), "out_of_range")
        if t["source"] == "simulated":
            raise HttpError(409, "This is a simulated device. Nothing physical can be written.", code="simulated")
        dev = db.one("SELECT * FROM devices WHERE id=?", (t["device_id"],))
        if not dev["enabled"]:
            raise HttpError(409, "The device is disconnected.")
        gw = db.one("SELECT * FROM gateways WHERE id=?", (dev["gateway_id"],))
        off_after = int(db.get_setting("gateway_offline_after_s")) * 1000
        if not gw or not gw["last_seen"] or now_ms() - gw["last_seen"] > off_after:
            raise HttpError(409, "The gateway is offline. Writes are never queued for later; try again when it is online.")
        st = db.one("SELECT value FROM tag_state WHERE tag_id=?", (t["id"],))
        eng.log_event("audit", "WRITE requested by %s: %s = %s %s (current value %s)" % (req.user["username"], label, value, t["unit"], st["value"] if st else "unknown"),
                      "warning", t["site_id"], t["device_id"], t["id"], req.user["username"])
        cid = eng.queue_command(gw["id"], dev["id"], "write", req.user["username"], {"tag_id": t["id"], "value": value})
        return {"command_id": cid, "done": False}

    @R("GET", "/api/tags/export")
    def tags_export(req):
        d = get_device(req, req.int_q("device_id"))
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(CSV_COLUMNS)
        for t in db.q("SELECT * FROM tags WHERE device_id=? ORDER BY name", (d["id"],)):
            w.writerow(["" if t[c] is None else t[c] for c in CSV_COLUMNS])
        return Response(out.getvalue().encode("utf-8-sig"), 200, "text/csv; charset=utf-8",
                        {"Content-Disposition": 'attachment; filename="tbox-tags-%s.csv"' % re.sub(r"\W+", "_", d["name"])})

    @R("POST", "/api/tags/import", roles=WRITE_ROLES)
    def tags_import(req):
        d = get_device(req, req.int_q("device_id"))
        text = req.body.decode("utf-8-sig", errors="replace")
        rdr = csv.DictReader(io.StringIO(text))
        if not rdr.fieldnames or "name" not in [f.strip().lower() for f in rdr.fieldnames]:
            raise HttpError(400, "CSV must have a header row with at least a 'name' column")
        created = updated = 0
        errors = []
        with db.tx():
            for i, raw in enumerate(rdr, start=2):
                if i > 5001:
                    errors.append({"row": i, "error": "Row limit (5000) exceeded"})
                    break
                row = {(k or "").strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in raw.items()}
                if not any(row.values()):
                    continue
                try:
                    t = clean_tag(row, d)
                except HttpError as e:
                    errors.append({"row": i, "error": e.message})
                    continue
                ex = db.one("SELECT id FROM tags WHERE device_id=? AND name=?", (d["id"], t["name"]))
                if ex:
                    db.execute("UPDATE tags SET %s WHERE id=?" % ",".join("%s=?" % f for f in TAG_FIELDS),
                               [t[f] for f in TAG_FIELDS] + [ex["id"]])
                    updated += 1
                else:
                    cols = ["device_id"] + TAG_FIELDS
                    db.execute("INSERT INTO tags(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols))),
                               [d["id"]] + [t[f] for f in TAG_FIELDS])
                    created += 1
            if errors and req.query.get("all_or_nothing") == "1":
                raise HttpError(400, "Import rejected: %d row(s) have errors, nothing was imported" % len(errors))
        eng.bump_gateway_config(d["gateway_id"])
        eng.invalidate()
        eng.log_event("audit", "Tag import for %s: %d created, %d updated, %d rejected" % (d["name"], created, updated, len(errors)),
                      site_id=d["site_id"], device_id=d["id"], username=req.user["username"])
        return {"created": created, "updated": updated, "errors": errors}

    # ------------------------------------------------------------ live + history
    @R("GET", "/api/live")
    def live(req):
        cl, p = site_clause(visible_sites(db, req.user), "d.site_id")
        where = cl
        ids = req.query.get("tag_ids")
        if ids:
            try:
                idl = [int(x) for x in ids.split(",")][:300]
            except ValueError:
                raise HttpError(400, "tag_ids must be a comma-separated list of integers")
            where += " AND t.id IN (%s)" % ",".join("?" * len(idl))
            p += idl
        if req.int_q("device_id"):
            where += " AND t.device_id=?"
            p.append(req.int_q("device_id"))
        if req.int_q("site_id"):
            where += " AND d.site_id=?"
            p.append(req.int_q("site_id"))
        if not (ids or req.int_q("device_id") or req.int_q("site_id")):
            raise HttpError(400, "Provide tag_ids, device_id or site_id")
        tv = tag_views(where + " AND t.enabled=1", p, 1000)
        dev_ids = sorted({t["device_id"] for t in tv})
        devs = []
        if dev_ids:
            devs = db.q("SELECT id,name,status,source,last_comm_ts,last_error FROM devices WHERE id IN (%s)" % ",".join("?" * len(dev_ids)), dev_ids)
        slim = ("id", "device_id", "device_name", "name", "unit", "value", "quality", "ts", "data_type", "alarm", "simulated",
                "lolo", "lo", "hi", "hihi", "disp_min", "disp_max", "no_data", "aggregation", "access", "description", "write_min", "write_max")
        return {"now": now_ms(), "tags": [{k: t[k] for k in slim} for t in tv], "devices": devs}

    @R("GET", "/api/tags/<int:id>/history")
    def history(req):
        t = tag_row(req)
        now = now_ms()
        to = req.float_q("to", now)
        frm = req.float_q("from", to - 3600_000)
        points = max(10, min(req.int_q("points", 600), 3000))
        if to <= frm:
            raise HttpError(400, "'to' must be after 'from'")
        bucket = max(1, int((to - frm) / points))
        rows = db.conn().execute(
            "SELECT (ts-?)/? AS b, AVG(value), MIN(value), MAX(value), COUNT(*), SUM(quality!='good') FROM samples "
            "WHERE tag_id=? AND ts>=? AND ts<=? GROUP BY b ORDER BY b", (int(frm), bucket, t["id"], int(frm), int(to))).fetchall()
        pts = [[int(frm) + r[0] * bucket + bucket // 2, r[1], r[2], r[3], r[4], r[5] or 0] for r in rows]
        return {"tag": {"id": t["id"], "name": t["name"], "unit": t["unit"], "device": t["device_name"],
                        "simulated": t["source"] == "simulated", "aggregation": t["aggregation"]},
                "from": int(frm), "to": int(to), "bucket_ms": bucket, "points": pts}
