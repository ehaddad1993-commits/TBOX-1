"""End-to-end: Modbus TCP simulator -> Edge Gateway -> TBox server -> API. No mocks of the protocol path."""
import time
import unittest
from pathlib import Path
import tempfile

import helpers
from helpers import Api, login, start_app
from tbox_gateway.agent import Gateway
from tbox_gateway.cloud import CloudClient, CloudError
from tbox_gateway.config import GatewayConfig
from tbox_modbus.sim import DataStore, ModbusTcpSimServer, populate_meter


def wait(fn, timeout=15, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


class FlakyCloud(CloudClient):
    """Real HTTP client that can be switched off to simulate an internet outage."""
    down = False

    def _call(self, *a, **k):
        if self.down:
            raise CloudError("simulated internet outage", "network")
        return super()._call(*a, **k)


class GatewayE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = DataStore(1000)
        populate_meter(cls.ds)
        cls.mb = ModbusTcpSimServer("127.0.0.1", 0, cls.ds).start()
        cls.app, cls.base = start_app()
        cls.admin = login(cls.base)
        cid = cls.admin.post("/api/companies", {"name": "E2E"}, expect=200)[1]["id"]
        cls.site = cls.admin.post("/api/sites", {"company_id": cid, "name": "S"}, expect=200)[1]["id"]
        g = cls.admin.post("/api/gateways", {"site_id": cls.site, "name": "GW1"}, expect=200)[1]
        cls.gid, token = g["id"], g["token"]
        cls.dev = cls.admin.post("/api/devices", {
            "site_id": cls.site, "name": "SimMeter", "source": "gateway", "gateway_id": cls.gid, "protocol": "modbus_tcp",
            "profile_id": "tbox-modbus-simulator", "category": "energy_meter",
            "connection": {"host": "127.0.0.1", "port": cls.mb.port, "slave_id": 1, "poll_interval_ms": 200, "timeout_ms": 500, "retries": 1}},
            expect=200)[1]["id"]
        d = Path(tempfile.mkdtemp())
        cfg = GatewayConfig(server_url=cls.base, token=token, state_dir=d, heartbeat_s=0.5, config_refresh_s=1.0, allow_writes=True)
        cls.cloud = FlakyCloud(cfg)
        cls.gw = Gateway(cfg, cloud=cls.cloud)
        cls.stop_feed = False
        import threading
        def feed():
            while not cls.stop_feed:
                populate_meter(cls.ds)
                time.sleep(0.2)
        threading.Thread(target=feed, daemon=True).start()
        cls.gw.start()

    @classmethod
    def tearDownClass(cls):
        cls.stop_feed = True
        cls.gw.stop()
        cls.mb.stop()
        cls.app.shutdown()

    def tags(self):
        return {t["name"]: t for t in self.admin.get("/api/live?device_id=%d" % self.dev)[1]["tags"]}

    def test_1_real_data_flows_end_to_end(self):
        self.assertTrue(wait(lambda: self.tags().get("Voltage L1-N", {}).get("quality") == "good"))
        t = self.tags()
        self.assertFalse(t["Voltage L1-N"]["simulated"])
        self.assertTrue(220 < t["Voltage L1-N"]["value"] < 240)
        self.assertAlmostEqual(t["Frequency"]["value"], 50, delta=0.2)
        self.assertTrue(30 < t["Temperature"]["value"] < 60)     # int16 scaled by 0.1
        self.assertEqual(t["Running"]["value"], 1.0)             # coil
        self.assertEqual(t["Fault"]["value"], 0.0)
        self.assertEqual(t["Status Bit 0"]["value"], 1.0)        # bit of a holding register
        self.assertGreater(t["Energy Import"]["value"], 100)     # uint32 * 0.1
        st = self.admin.get("/api/devices/%d" % self.dev)[1]
        self.assertTrue(wait(lambda: self.admin.get("/api/devices/%d" % self.dev)[1]["status"] == "online"))
        self.assertIsNotNone(st["last_comm_ts"])
        gws = self.admin.get("/api/gateways")[1]
        self.assertTrue(gws[0]["online"])
        self.assertIn("modbus_tcp", gws[0]["protocols"])

    def test_2_connection_test_command(self):
        _, r = self.admin.post("/api/devices/%d/test" % self.dev, expect=200)
        res = wait(lambda: (lambda c: c if c["done"] else None)(self.admin.get("/api/commands/%d" % r["command_id"])[1]))
        self.assertTrue(res["result"]["ok"], res)
        self.assertIn("Connected", res["result"]["message"])

    def test_3_offline_buffering_and_resync(self):
        self.app.db.execute("UPDATE tags SET log_interval_s=1 WHERE device_id=?", (self.dev,))  # 1-second logging
        self.app.engine.bump_gateway_config(self.gid)
        self.app.engine.invalidate()
        time.sleep(2.5)  # let the gateway pick up the new configuration
        self.cloud.down = True
        time.sleep(4.0)
        self.assertGreater(self.gw.buffer.depth(), 0)
        self.assertFalse(self.gw.uplink_ok)
        outage_start = int(time.time() * 1000) - 3800
        self.cloud.down = False
        self.assertTrue(wait(lambda: self.gw.buffer.depth() == 0 and self.gw.uplink_ok, 20))
        n = self.app.db.scalar("SELECT COUNT(*) FROM samples s JOIN tags t ON t.id=s.tag_id WHERE t.device_id=? AND s.ts>=? AND s.ts<?",
                               (self.dev, outage_start, outage_start + 3000), 0)
        self.assertGreater(n, 10, "samples recorded during the outage must be back-filled with their original timestamps")

    def test_4_bad_register_is_isolated(self):
        _, r = self.admin.post("/api/tags", {"device_id": self.dev, "name": "Missing", "table_type": "holding", "address": 5000,
                                              "data_type": "uint16"}, expect=200)
        t = wait(lambda: (lambda x: x if x and x["quality"] == "bad" and not x["no_data"] else None)(self.tags().get("Missing")), 20)
        self.assertIsNotNone(t)
        self.assertEqual(self.tags()["Voltage L1-N"]["quality"], "good")  # neighbours unaffected

    def test_5_comm_loss_raises_alarm_and_recovers(self):
        self.mb.fail_next = 10_000  # device drops every connection
        self.assertTrue(wait(lambda: self.admin.get("/api/devices/%d" % self.dev)[1]["status"] == "comm_lost", 25))
        al = [a for a in self.admin.get("/api/alarms")[1] if a["kind"] == "comm_lost" and a["device_id"] == self.dev]
        self.assertEqual(len(al), 1)
        self.assertEqual(al[0]["priority"], "high")
        self.assertTrue(wait(lambda: self.tags()["Voltage L1-N"]["quality"] in ("bad", "stale"), 10))
        self.mb.fail_next = 0
        self.assertTrue(wait(lambda: self.admin.get("/api/devices/%d" % self.dev)[1]["status"] in ("online", "alarm"), 30))
        self.assertFalse([a for a in self.admin.get("/api/alarms?state=history")[1] if a["kind"] == "comm_lost" and a["active"]])

    def test_6_disconnect_stops_polling(self):
        self.admin.post("/api/devices/%d/disconnect" % self.dev, expect=200)
        self.assertTrue(wait(lambda: self.gw.workers[self.dev].status == "disabled", 10))
        self.assertEqual(self.admin.get("/api/devices/%d" % self.dev)[1]["status"], "offline")
        self.admin.post("/api/devices/%d/connect" % self.dev, expect=200)
        self.assertTrue(wait(lambda: self.admin.get("/api/devices/%d" % self.dev)[1]["status"] in ("online", "alarm"), 15))

    def test_7_gateway_security(self):
        bad = Api(self.base, "tbx_wrong")
        bad.get("/api/gateway/config", expect=401)
        bad.post("/api/gateway/ingest", {"samples": []}, expect=401)
        self.admin.get("/api/gateway/config", expect=401)  # a user token is not a gateway token
        # a gateway cannot inject data for devices it does not own
        g2 = self.admin.post("/api/gateways", {"site_id": self.site, "name": "GW-other"}, expect=200)[1]
        some_tag = list(self.tags().values())[0]["id"]
        _, r = Api(self.base, g2["token"]).post("/api/gateway/ingest", {"samples": [{"tag_id": some_tag, "ts": int(time.time() * 1000), "value": 999999, "quality": "good"}]}, expect=200)
        self.assertEqual((r["accepted"], r["rejected"]), (0, 1))
        # token rotation invalidates the old token
        old = g2["token"]
        new = self.admin.post("/api/gateways/%d/rotate-token" % g2["id"], expect=200)[1]["token"]
        Api(self.base, old).get("/api/gateway/config", expect=401)
        Api(self.base, new).get("/api/gateway/config", expect=200)

    def test_8_unsupported_protocol_reported_not_faked(self):
        self.admin.post("/api/devices", {"site_id": self.site, "name": "OPC1", "source": "gateway", "gateway_id": self.gid,
                                         "protocol": "opcua", "connection": {"endpoint": "opc.tcp://10.9.9.9:4840"}}, expect=200)
        did = [d for d in self.admin.get("/api/devices")[1] if d["name"] == "OPC1"][0]["id"]
        def st():
            return self.admin.get("/api/devices/%d/diagnostics" % did)[1]
        self.assertTrue(wait(lambda: "not available" in st()["last_error"], 20), st())
        self.assertNotIn(st()["status"], ("online", "alarm"))

    # ---- controlled writes -------------------------------------------------
    def _write(self, tag_id, value, confirm=True, expect=200):
        body = {"value": value}
        if confirm:
            body["confirm"] = True
        return self.admin.post("/api/tags/%d/write" % tag_id, body, expect=expect)

    def _wait_cmd(self, cid):
        return wait(lambda: (lambda c: c if c["done"] else None)(self.admin.get("/api/commands/%d" % cid)[1]), 15)

    def test_9_controlled_write(self):
        mk = lambda **kw: self.admin.post("/api/tags", dict({"device_id": self.dev, "table_type": "holding", "data_type": "uint16", "access": "RW"}, **kw), expect=200)[1]["id"]
        sp = mk(name="Setpoint", address=200, write_min=0, write_max=100)
        spf = mk(name="SetpointScaled", address=202, scale=0.1, unit="bar")
        coil = mk(name="StartCmd", table_type="coil", address=10, data_type="bool")
        ro = mk(name="ReadOnlyReg", address=204, access="R")
        time.sleep(3.5)  # gateway picks up the new configuration
        # 1. master switch is off by default
        st, r = self._write(sp, 42, expect=403)
        self.assertEqual(r["code"], "control_disabled")
        self.admin.put("/api/settings", {"control_enabled": True}, expect=200)
        # 2. read-only tag, no confirmation, out of range, wrong role
        self.assertEqual(self._write(ro, 1, expect=403)[1]["code"], "tag_read_only")
        self.assertEqual(self._write(sp, 42, confirm=False, expect=400)[1]["code"], "confirm_required")
        self.assertEqual(self._write(sp, 500, expect=400)[1]["code"], "out_of_range")
        self.assertEqual(self._write(sp, -1, expect=400)[1]["code"], "out_of_range")
        self.admin.post("/api/tags/%d/write" % sp, {"value": "abc", "confirm": True}, expect=400)
        pw = "Operat0rW1te!!"
        self.admin.post("/api/users", {"username": "opw", "password": pw, "role": "operator", "site_ids": [self.site]}, expect=200)
        _, t = login(self.base, "opw", pw).post("/api/auth/change-password", {"current_password": pw, "new_password": "Operat0rW1te!!2"})
        Api(self.base, t["token"]).post("/api/tags/%d/write" % sp, {"value": 1, "confirm": True}, expect=403)
        self.assertNotEqual(self.ds.holding[200], 42)
        # 3. valid writes reach the device and are verified by read-back
        r = self._wait_cmd(self._write(sp, 42)[1]["command_id"])
        self.assertTrue(r["result"]["ok"], r)
        self.assertEqual(self.ds.holding[200], 42)
        self.assertIn("verified", r["result"]["message"])
        r = self._wait_cmd(self._write(spf, 12.3)[1]["command_id"])
        self.assertTrue(r["result"]["ok"], r)
        self.assertEqual(self.ds.holding[202], 123)  # engineering value / scale
        r = self._wait_cmd(self._write(coil, True)[1]["command_id"])
        self.assertTrue(r["result"]["ok"], r)
        self.assertTrue(self.ds.coils[10])
        # 4. gateway-side local switch overrides the cloud
        self.gw.cfg.allow_writes = False
        r = self._wait_cmd(self._write(sp, 7)[1]["command_id"])
        self.assertFalse(r["result"]["ok"])
        self.assertIn("disabled on this gateway", r["result"]["message"])
        self.assertEqual(self.ds.holding[200], 42)
        self.gw.cfg.allow_writes = True
        # 5. everything is audited
        _, ev = self.admin.get("/api/events?category=audit&limit=100")
        msgs = " | ".join(e["message"] for e in ev)
        for needle in ("WRITE requested", "WRITE CONFIRMED", "WRITE FAILED", "BLOCKED write", "REMOTE CONTROL ENABLED"):
            self.assertIn(needle, msgs)
        # 6. switching control off blocks again
        self.admin.put("/api/settings", {"control_enabled": False}, expect=200)
        self._write(sp, 5, expect=403)

    def test_9b_write_refused_when_gateway_offline(self):
        self.admin.put("/api/settings", {"control_enabled": True}, expect=200)
        tid = [t for t in self.tags().values() if t["name"] == "Setpoint"][0]["id"]
        self.app.db.execute("UPDATE gateways SET last_seen=? WHERE id=?", (int(time.time() * 1000) - 600_000, self.gid))
        st, r = self._write(tid, 5, expect=409)
        self.assertIn("offline", r["error"])
        self.admin.put("/api/settings", {"control_enabled": False}, expect=200)


if __name__ == "__main__":
    unittest.main()
