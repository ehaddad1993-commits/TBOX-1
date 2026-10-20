"""TBox Edge Gateway agent."""
import json
import logging
import os
import platform
import queue
import threading
import time

from tbox_modbus import ModbusError

from . import __version__
from .buffer import Buffer
from .cloud import CloudClient, CloudError
from .drivers import available_protocols, get_driver

log = logging.getLogger("tbox.gateway")


def now_ms():
    return int(time.time() * 1000)


class DeviceWorker(threading.Thread):
    """Polls one device. Reports Good/Bad samples and connection status; never writes to the device."""

    def __init__(self, gw, dev):
        super().__init__(daemon=True, name="dev-%s" % dev["id"])
        self.gw, self.dev = gw, dev
        self.stop_evt = threading.Event()
        self.wake = threading.Event()
        self.cmds = queue.Queue()
        self.session = None
        self.failures = 0
        self.status = "unknown"
        self.last_error = ""
        self.latency_ms = None
        self.stats = {"polls": 0, "failures": 0, "last_ok": None}
        self.since = now_ms()
        c = dev["connection"]
        self.threshold = int(c.get("retries", 2)) + 1
        self.base_poll = max(0.1, c.get("poll_interval_ms", 1000) / 1000.0)
        self.next_due = {}

    def stop(self):
        self.stop_evt.set()
        self.wake.set()

    def set_status(self, status, err=""):
        if status != self.status:
            self.since = now_ms()
            log.info("device '%s' status %s -> %s %s", self.dev["name"], self.status, status, err)
        self.status, self.last_error = status, err
        self.gw.statuses[self.dev["id"]] = {
            "device_id": self.dev["id"], "status": status, "last_error": err, "latency_ms": self.latency_ms,
            "since": self.since, "stats": dict(self.stats), "driver": self.dev["protocol"]}

    def _close(self):
        if self.session:
            self.session.close()
            self.session = None

    def run(self):
        driver = get_driver(self.dev["protocol"])
        if not self.dev.get("enabled", True):
            self.set_status("disabled")
        elif driver is None or self.dev["protocol"] not in available_protocols():
            self.set_status("error", "Driver for '%s' is not available on this gateway" % self.dev["protocol"])
        while not self.stop_evt.is_set():
            self._handle_commands(driver)
            if self.status in ("disabled", "error") and (not self.dev.get("enabled", True) or driver is None
                                                         or self.dev["protocol"] not in available_protocols()):
                self.wake.wait(2.0)
                self.wake.clear()
                continue
            try:
                delay = self._cycle(driver)
            except Exception:  # never let a worker die
                log.exception("unexpected error polling %s", self.dev["name"])
                self._close()
                delay = 5.0
            self.wake.wait(delay)
            self.wake.clear()
        self._close()

    def _handle_commands(self, driver):
        while True:
            try:
                cid, cmd, payload = self.cmds.get_nowait()
            except queue.Empty:
                return
            if cmd == "reconnect":
                self._close()
                self.failures = 0
                self.next_due.clear()
                self.gw.report_result(cid, True, "Reconnecting now", {})
                self.wake.set()
            elif cmd == "write":
                self._do_write(cid, payload or {}, driver)
            elif cmd == "test":
                if driver is None or self.dev["protocol"] not in available_protocols():
                    self.gw.report_result(cid, False, "Driver for '%s' is not available on this gateway" % self.dev["protocol"], {})
                    continue
                self._close()  # free serial port / TCP slot for the test
                ok, msg, detail = driver(self.dev).test(self.dev["tags"])
                self.gw.report_result(cid, ok, msg, detail)
                self.next_due.clear()

    def _do_write(self, cid, p, driver):
        """Defence in depth: the gateway re-validates every write locally before touching the device."""
        if not self.gw.cfg.allow_writes:
            return self.gw.report_result(cid, False, "Writes are disabled on this gateway (allow_writes is false in its local configuration).", {})
        tag = next((t for t in self.dev["tags"] if t["id"] == p.get("tag_id")), None)
        if tag is None or driver is None or self.dev["protocol"] not in available_protocols():
            return self.gw.report_result(cid, False, "Tag or driver not available on this gateway", {})
        try:
            value = float(p["value"])
            if self.session is None:
                self.session = driver(self.dev)
                self.session.open()
            back = self.session.write(tag, value)
        except (ValueError, KeyError, TypeError) as e:
            return self.gw.report_result(cid, False, "Write refused: %s" % e, {})
        except ModbusError as e:
            self._close()
            return self.gw.report_result(cid, False, "Write failed: %s" % e, {"kind": e.kind})
        tol = abs(tag.get("scale") or 1.0) * 0.51 + 1e-6
        ok = back is not None and abs(back - value) <= tol
        msg = ("Wrote %s to %s; device reads back %s (verified)" % (value, tag["name"], back)) if ok else \
              ("Wrote %s to %s but the device reads back %s: value was NOT accepted as sent" % (value, tag["name"], back))
        log.warning("WRITE %s = %s -> readback %s (%s)", tag["name"], value, back, "ok" if ok else "MISMATCH")
        self.next_due.clear()
        self.gw.report_result(cid, ok, msg, {"written": value, "readback": back})

    def _cycle(self, driver):
        now = time.monotonic()
        due = [t for t in self.dev["tags"] if self.next_due.get(t["id"], 0) <= now]
        if not due:
            return max(0.05, min(self.next_due.values()) - now)
        t0 = time.monotonic()
        try:
            if self.session is None:
                self.session = driver(self.dev)
                self.session.open()
            results = self.session.read(due)
        except ModbusError as e:
            self._close()
            self.failures += 1
            self.stats["failures"] += 1
            self.stats["polls"] += 1
            self.latency_ms = None
            if self.failures >= self.threshold:
                self.set_status("comm_lost", str(e))
                ts = now_ms()
                for t in due:
                    self.gw.emit(t, ts, None, "bad")
            for t in due:
                self.next_due[t["id"]] = now + self._interval(t)
            return min(30.0, max(self.base_poll, 2.0 ** min(self.failures, 5) * 0.5))
        self.failures = 0
        self.stats["polls"] += 1
        self.stats["last_ok"] = now_ms()
        self.latency_ms = int((time.monotonic() - t0) * 1000)
        ts = now_ms()
        bad = [r[2] for r in results.values() if r[1] != "good"]
        for t in due:
            v, q, err = results[t["id"]]
            self.gw.emit(t, ts, v, q)
            self.next_due[t["id"]] = now + self._interval(t)
        self.set_status("online", ("%d tag(s) unreadable: %s" % (len(bad), bad[0])) if bad else "")
        nxt = min(self.next_due.values()) - time.monotonic()
        return max(0.05, nxt)

    def _interval(self, t):
        return (t.get("poll_interval_ms") or 0) / 1000.0 or self.base_poll


class Gateway:
    def __init__(self, cfg, cloud=None, buffer=None):
        self.cfg = cfg
        self.cloud = cloud or CloudClient(cfg)
        self.buffer = buffer or Buffer(cfg.state_dir / "buffer.db", cfg.max_buffer_rows)
        self.workers = {}
        self.statuses = {}
        self.config_version = -1
        self.stop_evt = threading.Event()
        self.uplink_ok = True
        self.last_enqueued = {}
        self.pending_results = []
        self._plock = threading.Lock()
        self.started = time.time()
        self.clock_skew_ms = None
        self._threads = []
        self._cache = cfg.state_dir / "config_cache.json"
        self.log_interval = {}

    # ------------------------------------------------------------ data path
    def emit(self, tag, ts, value, quality):
        tid = tag["id"]
        if not self.uplink_ok:
            # Offline: keep history at the tag's logging resolution so the buffer stays small
            last = self.last_enqueued.get(tid)
            interval = (tag.get("log_interval_s") or 60) * 1000
            if last and ts - last[0] < interval and last[1] == quality:
                return
        self.last_enqueued[tid] = (ts, quality)
        self.buffer.put("sample", {"tag_id": tid, "ts": ts, "value": value, "quality": quality})

    def report_result(self, cmd_id, ok, message, detail):
        with self._plock:
            self.pending_results.append({"id": cmd_id, "ok": bool(ok), "message": message, "detail": detail})

    # ------------------------------------------------------------ configuration
    def apply_config(self, payload):
        wanted = {d["id"]: d for d in payload.get("devices", [])}
        for did in list(self.workers):
            w = self.workers[did]
            if did not in wanted or json.dumps(wanted[did], sort_keys=True) != json.dumps(w.dev, sort_keys=True):
                w.stop()
                w.join(timeout=5)
                del self.workers[did]
                self.statuses.pop(did, None)
        for did, dev in wanted.items():
            if did not in self.workers:
                w = DeviceWorker(self, dev)
                self.workers[did] = w
                w.start()
        self.config_version = payload["version"]
        log.info("configuration v%s applied: %d device(s)", payload["version"], len(wanted))

    def refresh_config(self):
        try:
            resp = self.cloud.get_config(self.config_version)
        except CloudError as e:
            log.warning("config refresh failed: %s", e)
            return False
        if resp.get("changed"):
            self.apply_config(resp)
            try:
                tmp = self._cache.with_suffix(".tmp")
                tmp.write_text(json.dumps(resp))
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
                tmp.replace(self._cache)
            except OSError as e:
                log.warning("cannot cache configuration: %s", e)
        return True

    def load_cached_config(self):
        try:
            payload = json.loads(self._cache.read_text())
            self.apply_config(payload)
            self.config_version = -1  # still fetch the latest from the cloud as soon as possible
            log.info("started from cached configuration (offline start)")
        except (OSError, ValueError, KeyError):
            pass

    # ------------------------------------------------------------ uplink
    def _build_payload(self, items):
        with self._plock:
            results, self.pending_results = self.pending_results, []
        return {"sent_at": now_ms(), "config_version": self.config_version,
                "gateway": {"version": __version__, "host": platform.node(), "protocols": available_protocols(),
                            "buffer_depth": self.buffer.depth(), "uptime_s": int(time.time() - self.started)},
                "device_status": list(self.statuses.values()),
                "samples": [p for _, k, p in items if k == "sample"],
                "command_results": results}, results

    def uplink_once(self):
        """Send one batch (or heartbeat). Returns number of sample rows sent, or -1 on failure."""
        items = self.buffer.take(self.cfg.max_batch)
        payload, results = self._build_payload(items)
        try:
            resp = self.cloud.ingest(payload)
        except CloudError as e:
            with self._plock:
                self.pending_results = results + self.pending_results
            if self.uplink_ok:
                log.warning("uplink down (%s). Buffering locally.", e)
            self.uplink_ok = False
            if e.kind == "auth":
                log.error("The server rejected this gateway's token.")
            return -1
        if not self.uplink_ok:
            log.info("uplink restored. Buffered records: %d", self.buffer.depth())
        self.uplink_ok = True
        if items:
            self.buffer.delete_upto(items[-1][0], items[0][0])
        if resp.get("rejected"):
            log.warning("server rejected %s sample(s) (unknown tag or device not assigned)", resp["rejected"])
        if "server_time" in resp:
            self.clock_skew_ms = now_ms() - resp["server_time"]
            if abs(self.clock_skew_ms) > 5000:
                log.warning("clock differs from server by %d ms. Fix NTP on this gateway.", self.clock_skew_ms)
        for c in resp.get("commands", []):
            w = self.workers.get(c["device_id"])
            if w:
                w.cmds.put((c["id"], c["command"], c.get("payload")))
                w.wake.set()
            else:
                self.report_result(c["id"], False, "Device is not configured on this gateway", {})
        if resp.get("config_version") not in (None, self.config_version):
            self.refresh_config()
        return len(items)

    def _uplink_loop(self):
        last = 0.0
        backoff = 1.0
        while not self.stop_evt.is_set():
            has_data = self.buffer.depth() > 0 or self.pending_results
            if not has_data and time.monotonic() - last < self.cfg.heartbeat_s:
                self.stop_evt.wait(0.5)
                continue
            n = self.uplink_once()
            last = time.monotonic()
            if n < 0:
                self.stop_evt.wait(backoff)
                backoff = min(backoff * 2, 30.0)
            else:
                backoff = 1.0
                if n >= self.cfg.max_batch:
                    continue  # drain quickly after an outage
                self.stop_evt.wait(0.5)

    def _config_loop(self):
        while not self.stop_evt.wait(self.cfg.config_refresh_s):
            self.refresh_config()

    # ------------------------------------------------------------ lifecycle
    def start(self):
        log.info("TBox Edge Gateway %s starting. Protocols: %s", __version__, ", ".join(available_protocols()))
        self.load_cached_config()
        self.refresh_config()
        for fn, name in ((self._uplink_loop, "uplink"), (self._config_loop, "config")):
            t = threading.Thread(target=fn, daemon=True, name=name)
            t.start()
            self._threads.append(t)

    def stop(self):
        self.stop_evt.set()
        for w in list(self.workers.values()):
            w.stop()
        for w in list(self.workers.values()):
            w.join(timeout=5)
        self.buffer.close()
