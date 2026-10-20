"""Application assembly: wiring, authentication, bootstrap (admin user, profiles, demo data)."""
import json
import logging
import logging.handlers
import secrets
import time

from . import api_core, api_devices, api_ops, security
from .config import Config
from .db import Database, now_ms
from .engine import Engine
from .web import HttpError, Request, Response, Router, TBoxHTTPServer, json_response, make_handler

log = logging.getLogger("tbox.app")
PASSWORD_CHANGE_OK = ("/api/auth/me", "/api/auth/change-password", "/api/health")


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.ensure_dirs()
        self.db = Database(cfg.db_path)
        self.db.migrate(cfg.migrations_dir)
        self.vault = security.Vault(cfg.secret)
        self.limiter = security.RateLimiter(cfg.login_max_failures, cfg.login_window_s)
        self.engine = Engine(self.db, cfg, self.vault)
        self.router = Router()
        self.profiles = load_profiles(cfg.profiles_dir)
        for mod in (api_core, api_devices, api_ops):
            mod.register(self)
        self.initial_admin_password = bootstrap_admin(self)
        if cfg.seed_demo:
            seed_demo(self)
        self.httpd = None

    # --------------------------------------------------------------- request pipeline
    def dispatch(self, req: Request):
        handler, auth, roles, params = self.router.match(req.method, req.path)
        req.params = params
        if auth == "user":
            self._auth_user(req)
            if req.user["must_change_password"] and req.path not in PASSWORD_CHANGE_OK:
                raise HttpError(403, "You must change your password before continuing", code="password_change_required")
            if roles and req.user["role"] not in roles:
                raise HttpError(403, "Your role does not permit this action")
        elif auth == "gateway":
            self._auth_gateway(req)
        result = handler(req)
        return result if isinstance(result, Response) else json_response(result)

    @staticmethod
    def _bearer(req):
        h = req.headers.get("Authorization", "")
        if not h.lower().startswith("bearer "):
            raise HttpError(401, "Authentication required")
        return h[7:].strip()

    def _auth_user(self, req):
        token = self._bearer(req)
        try:
            payload = security.jwt_decode(token, self.cfg.secret)
        except security.AuthError:
            raise HttpError(401, "Session expired or invalid. Please sign in again.")
        u = self.db.one("SELECT * FROM users WHERE id=?", (payload.get("sub"),))
        if not u or not u["active"] or u["token_version"] != payload.get("tv"):
            raise HttpError(401, "Session expired or invalid. Please sign in again.")
        req.user = u

    def _auth_gateway(self, req):
        token = self._bearer(req)
        gw = self.db.one("SELECT * FROM gateways WHERE token_hash=?", (security.hash_token(token),))
        if not gw or not gw["enabled"]:
            raise HttpError(401, "Invalid gateway token")
        req.gateway = gw

    # --------------------------------------------------------------- lifecycle
    def serve(self, host=None, port=None, block=True):
        self.engine.start()
        self.httpd = TBoxHTTPServer((host or self.cfg.host, port if port is not None else self.cfg.port), make_handler(self))
        log.info("TBox listening on http://%s:%d", *self.httpd.server_address[:2])
        if block:
            try:
                self.httpd.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                self.shutdown()
        else:
            import threading
            threading.Thread(target=self.httpd.serve_forever, daemon=True, name="http").start()

    def shutdown(self):
        self.engine.stop()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()


def setup_logging(cfg, level=logging.INFO):
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not root.handlers:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(cfg.log_dir / "tbox.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


def load_profiles(directory):
    out = {}
    try:
        files = sorted(directory.glob("*.json"))
    except OSError:
        files = []
    for f in files:
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
            out[p["id"]] = p
        except Exception as e:
            log.error("cannot load profile %s: %s", f.name, e)
    return out


def bootstrap_admin(app):
    db = app.db
    if db.scalar("SELECT COUNT(*) FROM users", (), 0):
        return None
    pw = app.cfg.admin_password
    generated = False
    if not pw:
        generated = True
        while True:
            pw = secrets.token_urlsafe(12)
            if not security.check_password_policy(pw):
                break
    problem = security.check_password_policy(pw)
    if problem:
        raise SystemExit("TBOX_ADMIN_PASSWORD rejected: " + problem)
    db.execute("INSERT INTO users(username,full_name,password_hash,role,company_id,must_change_password,created_at) VALUES(?,?,?,?,NULL,?,?)",
               (app.cfg.admin_user, "Administrator", security.hash_password(pw), "admin", 1 if generated else 0, now_ms()))
    app.engine.log_event("system", "Initial administrator account created: %s" % app.cfg.admin_user)
    return pw if generated else None


def seed_demo(app):
    db = app.db
    if db.scalar("SELECT COUNT(*) FROM companies", (), 0):
        return
    now = now_ms()
    with db.tx():
        cid = db.execute("INSERT INTO companies(name,created_at) VALUES('Technical Power Systems',?)", (now,)).lastrowid
        sid = db.execute("INSERT INTO sites(company_id,name,description,timezone,created_at) VALUES(?,?,?,?,?)",
                         (cid, "Demo Site (SIMULATED)", "Built-in demonstration site. All values are simulated.", "UTC", now)).lastrowid
        bid = db.execute("INSERT INTO buildings(site_id,name,description) VALUES(?,?,?)", (sid, "Plant Room", "Demo building")).lastrowid
        demo = [("sim-power-meter", "Main Incomer Meter"), ("sim-generator", "Generator G1"),
                ("sim-pump-vfd", "Water Pump P1 + VFD"), ("sim-solar-inverter", "Solar Inverter INV1")]
        for pid, name in demo:
            p = app.profiles.get(pid)
            if not p:
                continue
            conn = json.dumps({"poll_interval_ms": 1000, "timeout_ms": 3000, "retries": 0})
            did = db.execute(
                "INSERT INTO devices(site_id,building_id,name,description,location,manufacturer,model,category,profile_id,source,protocol,connection,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'simulated','modbus_tcp',?,?,?)",
                (sid, bid, name + " (SIMULATED)", p.get("notes", ""), "Plant Room", p.get("manufacturer", ""), p.get("model", ""),
                 p.get("category", "other"), pid, conn, now, now)).lastrowid
            dev = db.one("SELECT * FROM devices WHERE id=?", (did,))
            for tb in p["tags"]:
                t = api_devices.clean_tag(tb, dev)
                cols = ["device_id"] + api_devices.TAG_FIELDS
                db.execute("INSERT INTO tags(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols))),
                           [did] + [t[f] for f in api_devices.TAG_FIELDS])
    app.engine.invalidate()
    app.engine.log_event("system", "Demo site with SIMULATED devices created")
