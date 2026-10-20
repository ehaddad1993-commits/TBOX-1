"""Modbus TCP / RTU driver: block-optimised reads, decoding, scaling, isolation of bad addresses."""
import time
from collections import defaultdict

from tbox_modbus import ModbusError, ModbusRtuClient, ModbusTcpClient, decode_value
from tbox_modbus.codec import DATA_TYPES, encode_value, word_count

TABLE_FN = {"coil": "read_coils", "discrete": "read_discrete_inputs",
            "holding": "read_holding_registers", "input": "read_input_registers"}
MAX_REGS, MAX_BITS, MAX_GAP = 100, 1000, 4


class Block:
    __slots__ = ("table", "start", "end", "items")

    def __init__(self, table, start, end, item):
        self.table, self.start, self.end, self.items = table, start, end, [item]


def tag_span(t):
    if t["table_type"] in ("coil", "discrete"):
        return 1
    return word_count(t["data_type"])


def plan_blocks(tags):
    by_table = defaultdict(list)
    for t in tags:
        if t["table_type"] in TABLE_FN:
            by_table[t["table_type"]].append((t["address"], t["address"] + tag_span(t), t))
    blocks = []
    for table, items in by_table.items():
        limit = MAX_BITS if table in ("coil", "discrete") else MAX_REGS
        cur = None
        for s, e, t in sorted(items, key=lambda x: x[0]):
            if cur and s - cur.end <= MAX_GAP and max(e, cur.end) - cur.start <= limit:
                cur.end = max(cur.end, e)
                cur.items.append((s, e, t))
            else:
                cur = Block(table, s, e, (s, e, t))
                blocks.append(cur)
    return blocks


def decode_tag(t, words):
    """words: list of bool (coil/discrete) or int registers. Returns float."""
    if t["table_type"] in ("coil", "discrete"):
        return 1.0 if words[0] else 0.0
    v = decode_value(words, t["data_type"], t.get("byte_order") or "ABCD", t.get("bit"))
    if t["data_type"] == "bool":
        return 1.0 if v else 0.0
    return float(v) * (t.get("scale") or 1.0) + (t.get("offset") or 0.0)


class ModbusSession:
    def __init__(self, device):
        c = device["connection"]
        self.device = device
        self.timeout = c.get("timeout_ms", 3000) / 1000.0
        if device["protocol"] == "modbus_tcp":
            self.client = ModbusTcpClient(c["host"], c.get("port", 502), c.get("slave_id", 1), self.timeout)
        else:
            self.client = ModbusRtuClient(c["serial_port"], c.get("baud_rate", 9600), c.get("parity", "N"),
                                          c.get("stop_bits", 1), c.get("data_bits", 8), c.get("slave_id", 1), self.timeout)

    def open(self):
        self.client.connect()

    def close(self):
        self.client.close()

    def read(self, tags):
        """Returns {tag_id: (value|None, quality, error|None)}. Raises ModbusError on link failure."""
        out = {}
        for blk in plan_blocks(tags):
            fn = getattr(self.client, TABLE_FN[blk.table])
            try:
                data = fn(blk.start, blk.end - blk.start)
            except ModbusError as e:
                if e.kind != "exception":
                    raise  # link problem: let the worker handle reconnect / comm-lost
                self._read_individually(blk, out)
                continue
            for s, e, t in blk.items:
                self._decode_into(out, t, data[s - blk.start:e - blk.start])
        for t in tags:
            out.setdefault(t["id"], (None, "bad", "unsupported table type %s" % t["table_type"]))
        return out

    def _read_individually(self, blk, out):
        fn = getattr(self.client, TABLE_FN[blk.table])
        for s, e, t in blk.items:
            try:
                self._decode_into(out, t, fn(s, e - s))
            except ModbusError as ex:
                if ex.kind != "exception":
                    raise
                out[t["id"]] = (None, "bad", str(ex))

    @staticmethod
    def _decode_into(out, t, words):
        try:
            out[t["id"]] = (decode_tag(t, words), "good", None)
        except (ValueError, IndexError) as ex:
            out[t["id"]] = (None, "bad", "decode error: %s" % ex)

    def write(self, t, value):
        """Write one tag and read it back. Raises ValueError for anything unsafe or unsupported."""
        if t.get("access") != "RW":
            raise ValueError("tag is not marked read/write")
        if t.get("write_min") is not None and value < t["write_min"] or t.get("write_max") is not None and value > t["write_max"]:
            raise ValueError("value outside the allowed write range")
        if t["table_type"] == "coil":
            self.client.write_single_coil(t["address"], bool(value))
        elif t["table_type"] == "holding":
            if t.get("bit") is not None:
                raise ValueError("bit writes are not supported")
            if t["data_type"] == "bool":
                words = [1 if value else 0]
            else:
                raw = (float(value) - (t.get("offset") or 0.0)) / (t.get("scale") or 1.0)
                if DATA_TYPES[t["data_type"]][1][-1] not in "fd":
                    raw = int(round(raw))
                try:
                    words = encode_value(raw, t["data_type"], t.get("byte_order") or "ABCD")
                except Exception:
                    raise ValueError("value does not fit the %s data type" % t["data_type"])
            if len(words) == 1:
                self.client.write_single_register(t["address"], words[0])
            else:
                self.client.write_multiple_registers(t["address"], words)
        else:
            raise ValueError("only coils and holding registers can be written")
        back = self.read([t])[t["id"]]
        return back[0]

    def test(self, tags):
        t0 = time.monotonic()
        probe = [t for t in tags if t["table_type"] in TABLE_FN][:1]
        try:
            self.open()
            if probe:
                res = self.read(probe)
                val, q, err = res[probe[0]["id"]]
                ms = int((time.monotonic() - t0) * 1000)
                if q == "good":
                    return True, "Connected. Read '%s' = %s in %d ms" % (probe[0]["name"], val, ms), {"latency_ms": ms, "value": val}
                return False, "Device answered but the tag could not be read: %s" % err, {"latency_ms": ms}
            try:
                self.client.read_holding_registers(0, 1)
            except ModbusError as e:
                if e.kind != "exception":
                    raise
            ms = int((time.monotonic() - t0) * 1000)
            return True, "Device answered in %d ms (no tags configured to read yet)" % ms, {"latency_ms": ms}
        except ModbusError as e:
            return False, "Connection test failed: %s" % e, {"kind": e.kind}
        finally:
            self.close()
