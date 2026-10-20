import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "server", ROOT / "gateway", ROOT / "packages" / "modbus"):
    sys.path.insert(0, str(p))

from tbox_server.app import App  # noqa: E402
from tbox_server.config import Config  # noqa: E402

ADMIN_PW = "Adm1nPassw0rd!"


class Api:
    def __init__(self, base, token=None):
        self.base, self.token = base, token

    def call(self, method, path, body=None, raw=None, headers=None, expect=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                status, payload, ctype = r.status, r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            status, payload, ctype = e.code, e.read(), e.headers.get("Content-Type", "")
        if expect is not None:
            assert status == expect, "%s %s -> %s (expected %s): %s" % (method, path, status, expect, payload[:300])
        if "json" in ctype and payload:
            return status, json.loads(payload)
        return status, payload

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body or {}, **kw)

    def put(self, path, body, **kw):
        return self.call("PUT", path, body, **kw)

    def delete(self, path, **kw):
        return self.call("DELETE", path, **kw)


def start_app(simulator=False, demo=False, **over):
    d = tempfile.mkdtemp(prefix="tbox-test-")
    cfg = Config.from_env(data_dir=Path(d), admin_password=ADMIN_PW, seed_demo=demo, run_simulator=simulator, port=0, **over)
    app = App(cfg)
    app.serve(host="127.0.0.1", port=0, block=False)
    base = "http://127.0.0.1:%d" % app.httpd.server_address[1]
    return app, base


def login(base, user="admin", pw=ADMIN_PW):
    st, r = Api(base).post("/api/auth/login", {"username": user, "password": pw})
    assert st == 200, r
    return Api(base, r["token"])
