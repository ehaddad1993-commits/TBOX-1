"""API: authentication, company/site/building hierarchy, users, gateways, settings."""
import time

from . import security
from .db import now_ms
from .util import loads, need, public_user, require_site, site_clause, visible_sites
from .web import HttpError

ADMIN = ("admin",)
ADMIN_ENG = ("admin", "engineer")


def register(app):
    db, eng, cfg = app.db, app.engine, app.cfg
    R = app.router.route

    # ------------------------------------------------------------ auth
    @R("POST", "/api/auth/login", auth="public")
    def login(req):
        b = req.json()
        username = str(b.get("username", "")).strip()
        password = str(b.get("password", ""))
        key = "%s|%s" % (req.ip, username.lower())
        if app.limiter.blocked(key):
            raise HttpError(429, "Too many failed attempts. Try again in a few minutes.")
        u = db.one("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,))
        ok = False
        if u and u["active"]:
            ok = security.verify_password(password, u["password_hash"])
        else:
            security.burn_time(password)
        if not ok:
            app.limiter.fail(key)
            eng.log_event("security", "Failed login for '%s' from %s" % (username[:50], req.ip), "warning", username=username[:50])
            raise HttpError(401, "Invalid username or password")
        app.limiter.reset(key)
        db.execute("UPDATE users SET last_login=? WHERE id=?", (now_ms(), u["id"]))
        eng.log_event("security", "User '%s' logged in from %s" % (u["username"], req.ip), username=u["username"])
        now = int(time.time())
        token = security.jwt_encode({"sub": u["id"], "tv": u["token_version"], "iat": now, "exp": now + cfg.token_ttl_s}, cfg.secret)
        return {"token": token, "expires_in": cfg.token_ttl_s, "user": public_user(db, u)}

    @R("GET", "/api/auth/me")
    def me(req):
        return public_user(db, req.user)

    @R("POST", "/api/auth/change-password")
    def change_password(req):
        b = req.json()
        if not security.verify_password(str(b.get("current_password", "")), req.user["password_hash"]):
            raise HttpError(400, "Current password is incorrect")
        new = str(b.get("new_password", ""))
        problem = security.check_password_policy(new)
        if problem:
            raise HttpError(400, problem)
        if new == b.get("current_password"):
            raise HttpError(400, "New password must differ from the current one")
        db.execute("UPDATE users SET password_hash=?, must_change_password=0, token_version=token_version+1 WHERE id=?",
                   (security.hash_password(new), req.user["id"]))
        eng.log_event("security", "Password changed", username=req.user["username"])
        u = db.one("SELECT * FROM users WHERE id=?", (req.user["id"],))
        now = int(time.time())
        token = security.jwt_encode({"sub": u["id"], "tv": u["token_version"], "iat": now, "exp": now + cfg.token_ttl_s}, cfg.secret)
        return {"token": token, "user": public_user(db, u)}

    # ------------------------------------------------------------ hierarchy
    @R("GET", "/api/companies")
    def companies(req):
        u = req.user
        if u["company_id"] is None and u["role"] == "admin":
            return db.q("SELECT * FROM companies ORDER BY name")
        ids = visible_sites(db, u)
        cl, p = site_clause(ids, "s.id")
        return db.q("SELECT DISTINCT c.* FROM companies c JOIN sites s ON s.company_id=c.id WHERE %s ORDER BY c.name" % cl, p)

    @R("POST", "/api/companies", roles=ADMIN)
    def company_create(req):
        if req.user["company_id"] is not None:
            raise HttpError(403, "Only a platform administrator can create companies")
        name = need(req.json(), "name")
        try:
            cur = db.execute("INSERT INTO companies(name,created_at) VALUES(?,?)", (name, now_ms()))
        except Exception:
            raise HttpError(409, "A company with this name already exists")
        eng.log_event("audit", "Company created: %s" % name, username=req.user["username"])
        return {"id": cur.lastrowid, "name": name}

    @R("GET", "/api/sites")
    def sites(req):
        cl, p = site_clause(visible_sites(db, req.user), "s.id")
        return db.q("SELECT s.*, c.name AS company_name FROM sites s JOIN companies c ON c.id=s.company_id WHERE %s ORDER BY c.name, s.name" % cl, p)

    @R("POST", "/api/sites", roles=ADMIN)
    def site_create(req):
        b = req.json()
        company_id = req.user["company_id"] if req.user["company_id"] is not None else need(b, "company_id", int)
        if not db.one("SELECT 1 FROM companies WHERE id=?", (company_id,)):
            raise HttpError(400, "Unknown company")
        name = need(b, "name")
        try:
            cur = db.execute("INSERT INTO sites(company_id,name,description,address,timezone,created_at) VALUES(?,?,?,?,?,?)",
                             (company_id, name, need(b, "description", str, 500, False, ""), need(b, "address", str, 300, False, ""),
                              need(b, "timezone", str, 60, False, "UTC"), now_ms()))
        except Exception:
            raise HttpError(409, "A site with this name already exists in the company")
        eng.log_event("audit", "Site created: %s" % name, site_id=cur.lastrowid, username=req.user["username"])
        return {"id": cur.lastrowid}

    @R("PUT", "/api/sites/<int:id>", roles=ADMIN)
    def site_update(req):
        require_site(db, req.user, req.params["id"])
        b = req.json()
        db.execute("UPDATE sites SET name=?, description=?, address=?, timezone=? WHERE id=?",
                   (need(b, "name"), need(b, "description", str, 500, False, ""), need(b, "address", str, 300, False, ""),
                    need(b, "timezone", str, 60, False, "UTC"), req.params["id"]))
        eng.log_event("audit", "Site updated: %s" % b.get("name"), site_id=req.params["id"], username=req.user["username"])
        return {"ok": True}

    @R("DELETE", "/api/sites/<int:id>", roles=ADMIN)
    def site_delete(req):
        require_site(db, req.user, req.params["id"])
        if db.scalar("SELECT COUNT(*) FROM devices WHERE site_id=?", (req.params["id"],), 0):
            raise HttpError(409, "Site still has devices. Remove or archive them first.")
        db.execute("DELETE FROM sites WHERE id=?", (req.params["id"],))
        eng.log_event("audit", "Site %d deleted" % req.params["id"], username=req.user["username"])
        return {"ok": True}

    @R("GET", "/api/buildings")
    def buildings(req):
        cl, p = site_clause(visible_sites(db, req.user), "site_id")
        sid = req.int_q("site_id")
        extra = ""
        if sid:
            extra, p = " AND site_id=?", p + [sid]
        return db.q("SELECT * FROM buildings WHERE %s%s ORDER BY name" % (cl, extra), p)

    @R("POST", "/api/buildings", roles=ADMIN_ENG)
    def building_create(req):
        b = req.json()
        site_id = need(b, "site_id", int)
        require_site(db, req.user, site_id)
        try:
            cur = db.execute("INSERT INTO buildings(site_id,name,description) VALUES(?,?,?)",
                             (site_id, need(b, "name"), need(b, "description", str, 500, False, "")))
        except Exception:
            raise HttpError(409, "A building with this name already exists on the site")
        eng.log_event("audit", "Building created: %s" % b["name"], site_id=site_id, username=req.user["username"])
        return {"id": cur.lastrowid}

    @R("DELETE", "/api/buildings/<int:id>", roles=ADMIN_ENG)
    def building_delete(req):
        r = db.one("SELECT * FROM buildings WHERE id=?", (req.params["id"],))
        if not r:
            raise HttpError(404, "Not found")
        require_site(db, req.user, r["site_id"])
        db.execute("DELETE FROM buildings WHERE id=?", (r["id"],))
        eng.log_event("audit", "Building deleted: %s" % r["name"], site_id=r["site_id"], username=req.user["username"])
        return {"ok": True}

    @R("GET", "/api/tree")
    def tree(req):
        ids = visible_sites(db, req.user)
        cl, p = site_clause(ids, "s.id")
        out = []
        for s in db.q("SELECT s.*, c.name AS company_name FROM sites s JOIN companies c ON c.id=s.company_id WHERE %s ORDER BY c.name, s.name" % cl, p):
            s["buildings"] = db.q("SELECT id,name FROM buildings WHERE site_id=? ORDER BY name", (s["id"],))
            s["gateways"] = db.q("SELECT id,name,last_seen FROM gateways WHERE site_id=? ORDER BY name", (s["id"],))
            s["devices"] = db.q("SELECT id,name,building_id,gateway_id,status,source,category FROM devices WHERE site_id=? AND archived=0 ORDER BY name", (s["id"],))
            out.append(s)
        return out

    # ------------------------------------------------------------ gateways
    def gw_public(g):
        off_after = int(db.get_setting("gateway_offline_after_s")) * 1000
        g = dict(g)
        g.pop("token_hash", None)
        g["protocols"] = loads(g.get("protocols"), [])
        g["online"] = bool(g["last_seen"] and now_ms() - g["last_seen"] <= off_after)
        g["device_count"] = db.scalar("SELECT COUNT(*) FROM devices WHERE gateway_id=? AND archived=0", (g["id"],), 0)
        return g

    @R("GET", "/api/gateways")
    def gateways(req):
        cl, p = site_clause(visible_sites(db, req.user), "site_id")
        return [gw_public(g) for g in db.q("SELECT * FROM gateways WHERE %s ORDER BY name" % cl, p)]

    @R("POST", "/api/gateways", roles=ADMIN)
    def gateway_create(req):
        b = req.json()
        site_id = need(b, "site_id", int)
        require_site(db, req.user, site_id)
        token = security.new_gateway_token()
        try:
            cur = db.execute("INSERT INTO gateways(site_id,name,description,token_hash,created_at) VALUES(?,?,?,?,?)",
                             (site_id, need(b, "name"), need(b, "description", str, 500, False, ""), security.hash_token(token), now_ms()))
        except Exception:
            raise HttpError(409, "A gateway with this name already exists on the site")
        eng.log_event("audit", "Gateway created: %s" % b["name"], site_id=site_id, username=req.user["username"])
        return {"id": cur.lastrowid, "token": token, "note": "Copy this token now. It is shown only once."}

    def get_gw(req):
        g = db.one("SELECT * FROM gateways WHERE id=?", (req.params["id"],))
        if not g:
            raise HttpError(404, "Not found")
        require_site(db, req.user, g["site_id"])
        return g

    @R("POST", "/api/gateways/<int:id>/rotate-token", roles=ADMIN)
    def gateway_rotate(req):
        g = get_gw(req)
        token = security.new_gateway_token()
        db.execute("UPDATE gateways SET token_hash=? WHERE id=?", (security.hash_token(token), g["id"]))
        eng.log_event("audit", "Gateway token rotated: %s" % g["name"], site_id=g["site_id"], username=req.user["username"])
        return {"token": token, "note": "The previous token no longer works. Update the gateway configuration."}

    @R("PUT", "/api/gateways/<int:id>", roles=ADMIN)
    def gateway_update(req):
        g = get_gw(req)
        b = req.json()
        db.execute("UPDATE gateways SET name=?, description=?, enabled=? WHERE id=?",
                   (need(b, "name"), need(b, "description", str, 500, False, ""), 1 if b.get("enabled", True) else 0, g["id"]))
        eng.bump_gateway_config(g["id"])
        return {"ok": True}

    @R("DELETE", "/api/gateways/<int:id>", roles=ADMIN)
    def gateway_delete(req):
        g = get_gw(req)
        db.execute("DELETE FROM gateways WHERE id=?", (g["id"],))
        eng.log_event("audit", "Gateway deleted: %s" % g["name"], site_id=g["site_id"], username=req.user["username"])
        return {"ok": True}

    # ------------------------------------------------------------ users
    def user_scope(req):
        if req.user["company_id"] is None:
            return "1=1", []
        return "company_id=?", [req.user["company_id"]]

    @R("GET", "/api/users", roles=ADMIN)
    def users(req):
        cl, p = user_scope(req)
        return [public_user(db, u) for u in db.q("SELECT * FROM users WHERE %s ORDER BY username" % cl, p)]

    def clean_user(req, b, creating):
        role = need(b, "role")
        if role not in ("admin", "engineer", "operator"):
            raise HttpError(400, "role must be admin, engineer or operator")
        company_id = b.get("company_id")
        if req.user["company_id"] is not None:
            company_id = req.user["company_id"]
        elif company_id in ("", None):
            company_id = None
        site_ids = [int(x) for x in b.get("site_ids", [])]
        allowed = visible_sites(db, req.user)
        for s in site_ids:
            if allowed is not None and s not in allowed:
                raise HttpError(400, "Site %d is not available" % s)
        if role != "admin" and company_id is None and req.user["company_id"] is None:
            # non-admin users need a company so their scope is explicit; derive it from the first site
            if site_ids:
                company_id = db.scalar("SELECT company_id FROM sites WHERE id=?", (site_ids[0],))
        return role, company_id, site_ids

    @R("POST", "/api/users", roles=ADMIN)
    def user_create(req):
        b = req.json()
        username = need(b, "username", str, 50)
        pw = str(b.get("password", ""))
        problem = security.check_password_policy(pw)
        if problem:
            raise HttpError(400, problem)
        role, company_id, site_ids = clean_user(req, b, True)
        try:
            with db.tx():
                cur = db.execute(
                    "INSERT INTO users(username,full_name,email,password_hash,role,company_id,must_change_password,created_at) VALUES(?,?,?,?,?,?,1,?)",
                    (username, need(b, "full_name", str, 100, False, ""), need(b, "email", str, 120, False, ""),
                     security.hash_password(pw), role, company_id, now_ms()))
                for s in site_ids:
                    db.execute("INSERT INTO user_sites(user_id,site_id) VALUES(?,?)", (cur.lastrowid, s))
        except HttpError:
            raise
        except Exception:
            raise HttpError(409, "Username already exists")
        eng.log_event("audit", "User created: %s (%s)" % (username, role), username=req.user["username"])
        return {"id": cur.lastrowid}

    @R("PUT", "/api/users/<int:id>", roles=ADMIN)
    def user_update(req):
        cl, p = user_scope(req)
        u = db.one("SELECT * FROM users WHERE id=? AND %s" % cl, [req.params["id"]] + p)
        if not u:
            raise HttpError(404, "Not found")
        b = req.json()
        role, company_id, site_ids = clean_user(req, b, False)
        active = 1 if b.get("active", True) else 0
        if u["id"] == req.user["id"] and (role != "admin" or not active):
            raise HttpError(400, "You cannot demote or deactivate your own account")
        if u["role"] == "admin" and (role != "admin" or not active):
            others = db.scalar("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND id<>?", (u["id"],), 0)
            if not others:
                raise HttpError(400, "At least one active administrator is required")
        with db.tx():
            sql = "UPDATE users SET full_name=?, email=?, role=?, company_id=?, active=?, token_version=token_version+1 WHERE id=?"
            db.execute(sql, (need(b, "full_name", str, 100, False, ""), need(b, "email", str, 120, False, ""), role, company_id, active, u["id"]))
            if b.get("password"):
                problem = security.check_password_policy(str(b["password"]))
                if problem:
                    raise HttpError(400, problem)
                db.execute("UPDATE users SET password_hash=?, must_change_password=1 WHERE id=?", (security.hash_password(b["password"]), u["id"]))
            db.execute("DELETE FROM user_sites WHERE user_id=?", (u["id"],))
            for s in site_ids:
                db.execute("INSERT INTO user_sites(user_id,site_id) VALUES(?,?)", (u["id"], s))
        eng.log_event("audit", "User updated: %s" % u["username"], username=req.user["username"])
        return {"ok": True}

    @R("DELETE", "/api/users/<int:id>", roles=ADMIN)
    def user_delete(req):
        cl, p = user_scope(req)
        u = db.one("SELECT * FROM users WHERE id=? AND %s" % cl, [req.params["id"]] + p)
        if not u:
            raise HttpError(404, "Not found")
        if u["id"] == req.user["id"]:
            raise HttpError(400, "You cannot delete your own account")
        if u["role"] == "admin" and not db.scalar("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND id<>?", (u["id"],), 0):
            raise HttpError(400, "At least one active administrator is required")
        db.execute("DELETE FROM users WHERE id=?", (u["id"],))
        eng.log_event("audit", "User deleted: %s" % u["username"], username=req.user["username"])
        return {"ok": True}

    # ------------------------------------------------------------ settings / notifications
    NUMERIC = {"retention_samples_days": (1, 36500), "retention_events_days": (1, 36500),
               "retention_alarms_days": (1, 36500), "backup_keep": (1, 365), "gateway_offline_after_s": (10, 3600)}

    @R("GET", "/api/settings", roles=ADMIN)
    def settings_get(req):
        return {k: db.get_setting(k) for k in db.DEFAULT_SETTINGS}

    @R("PUT", "/api/settings", roles=ADMIN)
    def settings_put(req):
        b = req.json()
        for k, v in b.items():
            if k in NUMERIC:
                lo, hi = NUMERIC[k]
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise HttpError(400, "%s must be a number" % k)
                if not lo <= v <= hi:
                    raise HttpError(400, "%s must be between %d and %d" % (k, lo, hi))
                db.set_setting(k, v)
            elif k == "backup_enabled":
                db.set_setting(k, bool(v))
            elif k == "control_enabled":
                db.set_setting(k, bool(v))
                eng.log_event("audit", "REMOTE CONTROL %s by %s" % ("ENABLED" if v else "DISABLED", req.user["username"]),
                              "warning" if v else "info", username=req.user["username"])
            else:
                raise HttpError(400, "Unknown setting '%s'" % k)
        eng.log_event("audit", "Settings changed: %s" % ", ".join(b.keys()), username=req.user["username"])
        return {"ok": True}

    @R("GET", "/api/notifications", roles=ADMIN)
    def notif_list(req):
        rows = db.q("SELECT * FROM notification_channels ORDER BY name")
        for r in rows:
            c = loads(r["config"], {})
            if "password" in c:
                c["password"] = ""
                c["has_password"] = True
            r["config"] = c
        return rows

    @R("POST", "/api/notifications", roles=ADMIN)
    def notif_create(req):
        b = req.json()
        typ = need(b, "type")
        c = b.get("config") or {}
        if typ == "webhook":
            if not str(c.get("url", "")).startswith(("http://", "https://")):
                raise HttpError(400, "Webhook URL must start with http:// or https://")
        elif typ == "email":
            if not c.get("host") or not c.get("to"):
                raise HttpError(400, "Email needs an SMTP host and at least one recipient")
            if isinstance(c["to"], str):
                c["to"] = [x.strip() for x in c["to"].split(",") if x.strip()]
        else:
            raise HttpError(400, "type must be webhook or email")
        mp = b.get("min_priority", "high")
        if mp not in ("low", "medium", "high", "critical"):
            raise HttpError(400, "Invalid priority")
        import json as _j
        cur = db.execute("INSERT INTO notification_channels(name,type,config,min_priority,site_id,enabled) VALUES(?,?,?,?,?,1)",
                         (need(b, "name"), typ, _j.dumps(c), mp, b.get("site_id") or None))
        eng.log_event("audit", "Notification channel created: %s" % b["name"], username=req.user["username"])
        return {"id": cur.lastrowid}

    @R("DELETE", "/api/notifications/<int:id>", roles=ADMIN)
    def notif_delete(req):
        db.execute("DELETE FROM notification_channels WHERE id=?", (req.params["id"],))
        return {"ok": True}

    @R("GET", "/api/backups", roles=ADMIN)
    def backups(req):
        return eng.list_backups()

    @R("POST", "/api/backups", roles=ADMIN)
    def backup_create(req):
        info = eng.backup_now("manual by %s" % req.user["username"])
        eng.log_event("audit", "Manual backup created", username=req.user["username"])
        return info

    @R("GET", "/api/backups/<str:name>/download", roles=ADMIN)
    def backup_download(req):
        import re
        from .web import Response
        name = req.params["name"]
        if not re.fullmatch(r"tbox-\d{8}-\d{6}\.db", name):
            raise HttpError(404, "Not found")
        f = cfg.backup_dir / name
        if not f.is_file():
            raise HttpError(404, "Not found")
        eng.log_event("audit", "Backup downloaded: %s" % name, username=req.user["username"])
        return Response(f.read_bytes(), 200, "application/octet-stream", {"Content-Disposition": 'attachment; filename="%s"' % name})
