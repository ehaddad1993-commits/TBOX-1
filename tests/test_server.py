import csv
import io
import json
import time
import unittest
from pathlib import Path

import helpers
from helpers import Api, login, start_app
from tbox_server.db import now_ms


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.base = start_app()
        cls.admin = login(cls.base)
        _, c = cls.admin.get("/api/companies")
        cls.admin.post("/api/companies", {"name": "ACME"}, expect=200)
        cls.cid = [x for x in cls.admin.get("/api/companies")[1] if x["name"] == "ACME"][0]["id"]
        cls.site = cls.admin.post("/api/sites", {"company_id": cls.cid, "name": "Plant A"}, expect=200)[1]["id"]
        cls.site2 = cls.admin.post("/api/sites", {"company_id": cls.cid, "name": "Plant B"}, expect=200)[1]["id"]
        cls.dev = cls.admin.post("/api/devices", {"site_id": cls.site, "name": "Sim1", "source": "simulated", "protocol": "modbus_tcp",
                                                  "category": "energy_meter"}, expect=200)[1]["id"]

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown()

    def mktag(self, **kw):
        body = {"device_id": self.dev, "name": "T%d" % time.time_ns(), "table_type": "holding", "address": 0, "data_type": "float32", "unit": "V"}
        body.update(kw)
        return self.admin.post("/api/tags", body, expect=200)[1]["id"]

    # ---- auth
    def test_auth(self):
        api = Api(self.base)
        api.get("/api/devices", expect=401)
        api.post("/api/auth/login", {"username": "admin", "password": "nope"}, expect=401)
        api.get("/api/health", expect=200)
        Api(self.base, "garbage.token.here").get("/api/devices", expect=401)

    def test_rate_limit(self):
        api = Api(self.base)
        for _ in range(5):
            api.post("/api/auth/login", {"username": "ghost", "password": "x"}, expect=401)
        api.post("/api/auth/login", {"username": "ghost", "password": "x"}, expect=429)

    def test_security_headers_and_traversal(self):
        import urllib.request
        r = urllib.request.urlopen(self.base + "/api/health")
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        st, _ = Api(self.base).get("/../../etc/passwd")
        self.assertIn(st, (404,))

    def test_must_change_password_and_roles(self):
        pw = "Operat0rPass!!"
        self.admin.post("/api/users", {"username": "op1", "password": pw, "role": "operator", "site_ids": [self.site]}, expect=200)
        op = login(self.base, "op1", pw)
        op.get("/api/devices", expect=403)  # must change first
        _, r = op.post("/api/auth/change-password", {"current_password": pw, "new_password": "N3wOperatorPass!"}, expect=200)
        op2 = Api(self.base, r["token"])
        op.get("/api/auth/me", expect=401)  # old token revoked
        op2.get("/api/devices", expect=200)
        op2.post("/api/devices", {"site_id": self.site, "name": "x"}, expect=403)
        op2.get("/api/users", expect=403)
        op2.post("/api/tags/%d/write" % self.mktag(), {"value": 1}, expect=403)

    def test_site_scoping(self):
        pw = "Engin33rPass!!"
        uid = self.admin.post("/api/users", {"username": "eng1", "password": pw, "role": "engineer", "site_ids": [self.site2]}, expect=200)[1]["id"]
        _, r = login(self.base, "eng1", pw).post("/api/auth/change-password", {"current_password": pw, "new_password": "Engin33rPass!!2"})
        eng = Api(self.base, r["token"])
        self.assertEqual([s["name"] for s in eng.get("/api/sites")[1]], ["Plant B"])
        self.assertEqual(eng.get("/api/devices")[1], [])
        eng.get("/api/devices/%d" % self.dev, expect=404)
        eng.post("/api/devices", {"site_id": self.site, "name": "no", "source": "simulated"}, expect=404)
        eng.post("/api/devices", {"site_id": self.site2, "name": "Mine", "source": "simulated"}, expect=200)

    def test_password_policy(self):
        self.admin.post("/api/users", {"username": "weak", "password": "short", "role": "operator"}, expect=400)

    # ---- devices & validation
    def test_gateway_device_requires_gateway(self):
        st, r = self.admin.post("/api/devices", {"site_id": self.site, "name": "Real", "source": "gateway", "protocol": "modbus_tcp",
                                                  "connection": {"host": "10.0.0.5"}})
        self.assertEqual(st, 400)
        self.assertIn("gateway", r["error"].lower())

    def test_device_lifecycle(self):
        _, g = self.admin.post("/api/gateways", {"site_id": self.site, "name": "GW-%d" % time.time_ns()}, expect=200)
        self.assertTrue(g["token"].startswith("tbx_"))
        body = {"site_id": self.site, "name": "PLC-%d" % time.time_ns(), "source": "gateway", "gateway_id": g["id"], "protocol": "modbus_tcp",
                "category": "plc", "connection": {"host": "192.168.1.10", "port": 502, "slave_id": 1, "poll_interval_ms": 500},
                "credentials": {"username": "u", "password": "secret"}, "profile_id": "schneider-m221"}
        st, r = self.admin.post("/api/devices", body, expect=200)
        did = r["id"]
        self.assertEqual(r["tags_created"], 6)
        _, d = self.admin.get("/api/devices/%d" % did)
        self.assertEqual(d["connection"]["poll_interval_ms"], 500)
        self.assertNotIn("secret", json.dumps(d))
        self.assertTrue(d["credentials"]["has_password"])
        self.assertEqual(d["status"], "offline")
        # gateway never connected -> cannot test
        self.admin.post("/api/devices/%d/test" % did, expect=409)
        # config delivered to the gateway includes credentials, tags, version bump
        gw = Api(self.base, g["token"])
        _, cfg = gw.get("/api/gateway/config?version=-1", expect=200)
        dev = [x for x in cfg["devices"] if x["id"] == did][0]
        self.assertEqual(dev["credentials"]["password"], "secret")
        self.assertEqual(len(dev["tags"]), 6)
        _, cfg2 = gw.get("/api/gateway/config?version=%d" % cfg["version"])
        self.assertFalse(cfg2["changed"])
        self.admin.post("/api/devices/%d/disconnect" % did, expect=200)
        _, cfg3 = gw.get("/api/gateway/config?version=%d" % cfg["version"])
        self.assertTrue(cfg3["changed"])
        self.assertFalse([x for x in cfg3["devices"] if x["id"] == did][0]["enabled"])
        # duplicate is created disabled
        _, dup = self.admin.post("/api/devices/%d/duplicate" % did, {"name": "PLC copy %d" % time.time_ns()}, expect=200)
        _, dd = self.admin.get("/api/devices/%d" % dup["id"])
        self.assertFalse(dd["enabled"])
        self.assertEqual(dd["tag_count"], 6)
        # validation
        bad = dict(body, name="bad", connection={"host": "bad host!", "port": 502})
        self.admin.post("/api/devices", bad, expect=400)
        bad = dict(body, name="bad2", connection={"host": "10.0.0.1", "port": 99999})
        self.admin.post("/api/devices", bad, expect=400)
        # RTU settings
        rtu = dict(body, name="RTU-%d" % time.time_ns(), protocol="modbus_rtu", profile_id="",
                   connection={"serial_port": "COM3", "baud_rate": 19200, "parity": "E", "stop_bits": 1, "slave_id": 5})
        self.admin.post("/api/devices", rtu, expect=200)
        self.admin.post("/api/devices", dict(rtu, name="rtu-bad", connection={"serial_port": "COM3", "baud_rate": 12345}), expect=400)
        # gateway must belong to the same site
        _, g2 = self.admin.post("/api/gateways", {"site_id": self.site2, "name": "GW2-%d" % time.time_ns()})
        self.admin.post("/api/devices", dict(body, name="x%d" % time.time_ns(), gateway_id=g2["id"]), expect=400)
        # delete with no history = hard delete
        self.admin.delete("/api/devices/%d" % dup["id"], expect=200)
        self.admin.get("/api/devices/%d" % dup["id"], expect=404)

    def test_unsupported_driver_is_not_claimed(self):
        _, g = self.admin.post("/api/gateways", {"site_id": self.site, "name": "GW-op-%d" % time.time_ns()})
        st, r = self.admin.post("/api/devices", {"site_id": self.site, "name": "OPC", "source": "gateway", "gateway_id": g["id"],
                                                  "protocol": "opcua", "connection": {"endpoint": "opc.tcp://10.0.0.2:4840"}})
        self.assertEqual(st, 200)  # configurable, but the gateway reports the driver as missing (see gateway tests)

    # ---- tags
    def test_tag_validation(self):
        base = {"device_id": self.dev, "name": "v", "table_type": "holding", "address": 1, "data_type": "float32"}
        self.admin.post("/api/tags", dict(base, name="a%d" % time.time_ns(), scale=0), expect=400)
        self.admin.post("/api/tags", dict(base, name="b%d" % time.time_ns(), byte_order="XYZ"), expect=400)
        self.admin.post("/api/tags", dict(base, name="c%d" % time.time_ns(), lolo=10, lo=5), expect=400)
        self.admin.post("/api/tags", dict(base, name="d%d" % time.time_ns(), address=70000), expect=400)
        self.admin.post("/api/tags", dict(base, name="e%d" % time.time_ns(), table_type="input", access="RW"), expect=400)
        self.admin.post("/api/tags", dict(base, name="f%d" % time.time_ns(), data_type="float32", bit=3), expect=400)
        tid = self.mktag(name="dup-name")
        self.admin.post("/api/tags", dict(base, name="dup-name"), expect=409)

    def test_csv_import_export(self):
        csvtxt = ("name,unit,table_type,address,data_type,byte_order,scale,hi\n"
                  "ImpA,V,holding,10,float32,CDAB,1,250\n"
                  "ImpB,A,holding,12,uint16,ABCD,0.1,\n"
                  "ImpBad,A,holding,xx,uint16,ABCD,1,\n")
        _, r = self.admin.post("/api/tags/import?device_id=%d" % self.dev, raw=csvtxt.encode(), headers={"Content-Type": "text/csv"}, expect=200)
        self.assertEqual((r["created"], len(r["errors"])), (2, 1))
        self.assertEqual(r["errors"][0]["row"], 4)
        st, out = self.admin.get("/api/tags/export?device_id=%d" % self.dev, expect=200)
        rows = list(csv.DictReader(io.StringIO(out.decode("utf-8-sig"))))
        a = [x for x in rows if x["name"] == "ImpA"][0]
        self.assertEqual((a["byte_order"], a["hi"]), ("CDAB", "250.0"))
        # re-import is an update, not a duplicate
        _, r2 = self.admin.post("/api/tags/import?device_id=%d" % self.dev, raw=out, expect=200)
        self.assertEqual(r2["created"], 0)

    def test_remote_write_is_blocked_and_audited(self):
        tid = self.mktag(access="RW", table_type="holding")
        st, r = self.admin.post("/api/tags/%d/write" % tid, {"value": 5})
        self.assertEqual((st, r["code"]), (403, "control_disabled"))
        _, ev = self.admin.get("/api/events?category=audit&limit=20")
        self.assertTrue(any("BLOCKED write" in e["message"] for e in ev))

    # ---- ingest, quality, alarms, history
    def test_alarm_lifecycle_and_quality(self):
        tid = self.mktag(name="AlmV%d" % time.time_ns(), hi=250, hihi=260, lo=210, deadband=2, alarm_priority="medium", log_interval_s=1)
        eng = self.app.engine
        now = now_ms()
        eng.ingest_batch([(tid, now - 3000, 230.0, "good")], now)
        self.assertEqual(self.admin.get("/api/alarms?state=active")[1] and [a for a in self.admin.get("/api/alarms")[1] if a["tag_id"] == tid], [])
        eng.ingest_batch([(tid, now - 2000, 255.0, "good")], now)
        al = [a for a in self.admin.get("/api/alarms")[1] if a["tag_id"] == tid]
        self.assertEqual((len(al), al[0]["kind"], al[0]["priority"]), (1, "hi", "medium"))
        eng.ingest_batch([(tid, now - 1500, 256.0, "good")], now)  # no duplicate while active
        self.assertEqual(len([a for a in self.admin.get("/api/alarms")[1] if a["tag_id"] == tid]), 1)
        eng.ingest_batch([(tid, now - 1000, 262.0, "good")], now)
        kinds = sorted(a["kind"] for a in self.admin.get("/api/alarms")[1] if a["tag_id"] == tid)
        self.assertEqual(kinds, ["hi", "hihi"])
        eng.ingest_batch([(tid, now - 500, 249.0, "good")], now)  # inside deadband: hi stays
        self.assertTrue([a for a in self.admin.get("/api/alarms")[1] if a["tag_id"] == tid and a["kind"] == "hi" and a["active"]])
        eng.ingest_batch([(tid, now - 400, 245.0, "good")], now)  # below hi-deadband: clears
        hi = [a for a in self.admin.get("/api/alarms?state=history")[1] if a["tag_id"] == tid and a["kind"] == "hi"][0]
        self.assertFalse(hi["active"])
        self.assertIsNotNone(hi["cleared_ts"])
        self.admin.post("/api/alarms/%d/ack" % hi["id"], expect=200)
        _, h2 = self.admin.get("/api/alarms?state=history")
        self.assertEqual([a for a in h2 if a["id"] == hi["id"]][0]["acked_by"], "admin")
        # quality: fresh = good, old = stale, bad passthrough
        eng.ingest_batch([(tid, now_ms(), 230.0, "good")])
        q = lambda: [t for t in self.admin.get("/api/tags?device_id=%d" % self.dev)[1] if t["id"] == tid][0]
        self.assertEqual(q()["quality"], "good")
        self.assertTrue(q()["simulated"])
        eng.ingest_batch([(tid, now_ms() - 60_000, 230.0, "good")], now_ms())  # late data must not overwrite newer state
        self.assertEqual(q()["quality"], "good")
        eng.ingest_batch([(tid, now_ms(), None, "good")])  # missing value can never be Good
        self.assertEqual(q()["quality"], "bad")

    def test_history_logging_interval_and_dedupe(self):
        tid = self.mktag(name="Log%d" % time.time_ns(), log_interval_s=10)
        eng = self.app.engine
        t0 = now_ms() - 60_000
        eng.ingest_batch([(tid, t0 + i * 1000, float(i), "good") for i in range(35)], t0 + 40_000)
        n = self.app.db.scalar("SELECT COUNT(*) FROM samples WHERE tag_id=?", (tid,))
        self.assertEqual(n, 4)  # t0, +10, +20, +30
        eng.ingest_batch([(tid, t0, 0.0, "good")], t0 + 40_000)  # replay of buffered data: ignored duplicate
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM samples WHERE tag_id=?", (tid,)), 4)
        _, h = self.admin.get("/api/tags/%d/history?from=%d&to=%d&points=10" % (tid, t0 - 1000, t0 + 40_000))
        self.assertGreaterEqual(len(h["points"]), 3)
        self.assertEqual(h["tag"]["simulated"], True)

    # ---- reports
    def test_consumption_and_exports(self):
        ctr = self.mktag(name="Ctr%d" % time.time_ns(), unit="kWh", aggregation="counter", log_interval_s=1)
        lvl = self.mktag(name="Fuel%d" % time.time_ns(), unit="L", aggregation="level", log_interval_s=1)
        db = self.app.db
        base = 1_700_000_000_000  # 2023-11-14 22:13:20 UTC
        day = 86_400_000
        ctr_vals = [100, 110, 125, 5, 12]     # +10 +15, reset (skipped), +7 => 32
        lvl_vals = [500, 480, 900, 880, 850]  # -20, refuel (skipped), -20, -30 => 70
        for i in range(5):
            for tid, vals in ((ctr, ctr_vals), (lvl, lvl_vals)):
                db.execute("INSERT INTO samples(tag_id,ts,value,quality) VALUES(?,?,?,'good')", (tid, base + i * 3_600_000, vals[i]))
        db.execute("INSERT INTO samples(tag_id,ts,value,quality) VALUES(?,?,?,'bad')", (ctr, base + 5 * 3_600_000, None))
        q = "/api/reports/consumption?site_id=%d&period=day&from=%d&to=%d" % (self.site, base - 1000, base + day)
        _, r = self.admin.get(q, expect=200)
        by = {}
        for x in r["rows"]:
            by[x["tag"]] = by.get(x["tag"], 0) + x["consumption"]  # the data spans midnight UTC: two day-rows per tag
        self.assertEqual(len({x["period"] for x in r["rows"]}), 2)
        self.assertAlmostEqual(by[[t for t in by if t.startswith("Ctr")][0]], 32.0)
        self.assertAlmostEqual(by[[t for t in by if t.startswith("Fuel")][0]], 70.0)
        rng = "&from=%d&to=%d&period=day" % (base - 1000, base + day)
        st, data = self.admin.get("/api/reports/export?kind=consumption&format=csv&site_id=%d%s" % (self.site, rng), expect=200)
        self.assertIn(b"SIMULATED", data)
        st, data = self.admin.get("/api/reports/export?kind=history&format=xlsx&tag_ids=%d%s" % (ctr, rng), expect=200)
        self.assertTrue(data.startswith(b"PK"))
        st, data = self.admin.get("/api/reports/export?kind=summary&format=pdf&tag_id=%d%s" % (ctr, rng), expect=200)
        self.assertTrue(data.startswith(b"%PDF"))
        self.admin.get("/api/reports/export?kind=nope&format=csv", expect=400)

    # ---- backup / restore / retention
    def test_backup_restore_and_retention(self):
        import sqlite3, subprocess, sys, tempfile
        info = self.admin.post("/api/backups", expect=200)[1]
        path = self.app.cfg.backup_dir / info["name"]
        self.assertTrue(path.exists())
        self.assertEqual(sqlite3.connect(str(path)).execute("PRAGMA integrity_check").fetchone()[0], "ok")
        d = Path(tempfile.mkdtemp())
        r = subprocess.run([sys.executable, str(helpers.ROOT / "tools" / "restore_backup.py"), str(path), "--data-dir", str(d)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertGreater(sqlite3.connect(str(d / "tbox.db")).execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
        junk = d / "junk.db"
        junk.write_bytes(b"not a database")
        r = subprocess.run([sys.executable, str(helpers.ROOT / "tools" / "restore_backup.py"), str(junk), "--data-dir", str(d)], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        # retention
        tid = self.mktag(name="Old%d" % time.time_ns())
        self.app.db.execute("INSERT INTO samples(tag_id,ts,value,quality) VALUES(?,?,?,'good')", (tid, now_ms() - 400 * 86_400_000, 1.0))
        self.app.engine.purge_old()
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM samples WHERE tag_id=?", (tid,)), 0)

    def test_dashboard_and_layout(self):
        _, d = self.admin.get("/api/dashboard", expect=200)
        self.assertIn("devices", d)
        tid = self.mktag()
        self.admin.put("/api/dashboard/layout", {"widgets": [{"type": "gauge", "tag_id": tid}]}, expect=200)
        self.assertEqual(self.admin.get("/api/dashboard/layout")[1]["widgets"][0]["tag_id"], tid)
        self.admin.put("/api/dashboard/layout", {"widgets": [{"type": "evil", "tag_id": 1}]}, expect=400)

    def test_migrations_are_idempotent(self):
        self.app.db.migrate(self.app.cfg.migrations_dir)
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM schema_migrations"), len(list(self.app.cfg.migrations_dir.glob("*.sql"))))



class CloudHostTests(unittest.TestCase):
    def test_port_env_and_backup_download(self):
        import os
        from tbox_server.config import Config
        os.environ["PORT"] = "10000"
        try:
            self.assertEqual(Config.from_env().port, 10000)
        finally:
            del os.environ["PORT"]
        app, base = start_app()
        try:
            adm = login(base)
            name = adm.post("/api/backups", expect=200)[1]["name"]
            st, data = adm.get("/api/backups/%s/download" % name, expect=200)
            self.assertTrue(data.startswith(b"SQLite format 3"))
            adm.get("/api/backups/..%2Ftbox.db/download", expect=404)
            adm.get("/api/backups/evil.db/download", expect=404)
        finally:
            app.shutdown()


if __name__ == "__main__":
    unittest.main()
