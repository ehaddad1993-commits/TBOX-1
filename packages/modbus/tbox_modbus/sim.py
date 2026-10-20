"""Modbus device simulator used for development and automated tests.

Run standalone:   python -m tbox_modbus.sim --port 1502
It serves the "TBox Modbus Simulator Meter" register map (see profiles/tbox-modbus-simulator.json).
"""
import argparse
import math
import socketserver
import struct
import threading
import time

from .client import crc16
from .codec import encode_value


class DataStore:
    def __init__(self, size=65536):
        self.size = size
        self.lock = threading.Lock()
        self.coils = [False] * size
        self.discrete = [False] * size
        self.holding = [0] * size
        self.inputs = [0] * size

    def set_registers(self, address, words, table="holding"):
        with self.lock:
            tbl = self.holding if table == "holding" else self.inputs
            for i, w in enumerate(words):
                tbl[address + i] = w & 0xFFFF


class _Exc(Exception):
    def __init__(self, code):
        self.code = code


def handle_pdu(ds, pdu):
    """Process one request PDU and return the response PDU."""
    fc = pdu[0]
    try:
        if fc in (1, 2):
            addr, cnt = struct.unpack(">HH", pdu[1:5])
            if not 1 <= cnt <= 2000:
                raise _Exc(3)
            if addr + cnt > ds.size:
                raise _Exc(2)
            tbl = ds.coils if fc == 1 else ds.discrete
            with ds.lock:
                bits = tbl[addr:addr + cnt]
            data = bytearray((cnt + 7) // 8)
            for i, b in enumerate(bits):
                if b:
                    data[i // 8] |= 1 << (i % 8)
            return bytes([fc, len(data)]) + bytes(data)
        if fc in (3, 4):
            addr, cnt = struct.unpack(">HH", pdu[1:5])
            if not 1 <= cnt <= 125:
                raise _Exc(3)
            if addr + cnt > ds.size:
                raise _Exc(2)
            tbl = ds.holding if fc == 3 else ds.inputs
            with ds.lock:
                regs = tbl[addr:addr + cnt]
            return bytes([fc, 2 * cnt]) + struct.pack(">%dH" % cnt, *regs)
        if fc == 5:
            addr, val = struct.unpack(">HH", pdu[1:5])
            if val not in (0, 0xFF00):
                raise _Exc(3)
            if addr >= ds.size:
                raise _Exc(2)
            with ds.lock:
                ds.coils[addr] = val == 0xFF00
            return pdu[:5]
        if fc == 6:
            addr, val = struct.unpack(">HH", pdu[1:5])
            if addr >= ds.size:
                raise _Exc(2)
            with ds.lock:
                ds.holding[addr] = val
            return pdu[:5]
        if fc == 16:
            addr, cnt, bc = struct.unpack(">HHB", pdu[1:6])
            if not 1 <= cnt <= 123 or bc != 2 * cnt:
                raise _Exc(3)
            if addr + cnt > ds.size:
                raise _Exc(2)
            vals = struct.unpack(">%dH" % cnt, pdu[6:6 + bc])
            with ds.lock:
                ds.holding[addr:addr + cnt] = list(vals)
            return pdu[:5]
        raise _Exc(1)
    except _Exc as e:
        return bytes([fc | 0x80, e.code])
    except struct.error:
        return bytes([fc | 0x80, 3])


class _TcpHandler(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        sock.settimeout(60)
        try:
            while True:
                hdr = self._recv(sock, 7)
                if hdr is None:
                    return
                tid, pid, length, unit = struct.unpack(">HHHB", hdr)
                pdu = self._recv(sock, length - 1)
                if pdu is None:
                    return
                if unit not in self.server.units:
                    continue  # silent, like a real slave that is not addressed
                if self.server.fail_next:
                    self.server.fail_next -= 1
                    return  # drop the connection to simulate a failure
                resp = handle_pdu(self.server.datastore, pdu)
                sock.sendall(struct.pack(">HHHB", tid, 0, len(resp) + 1, unit) + resp)
        except (OSError, struct.error):
            return

    @staticmethod
    def _recv(sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf


class ModbusTcpSimServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, host="127.0.0.1", port=1502, datastore=None, units=(1,)):
        super().__init__((host, port), _TcpHandler)
        self.datastore = datastore or DataStore()
        self.units = set(units)
        self.fail_next = 0
        self._thread = None

    @property
    def port(self):
        return self.server_address[1]

    def start(self):
        self._thread = threading.Thread(target=self.serve_forever, daemon=True, name="modbus-sim")
        self._thread.start()
        return self

    def stop(self):
        self.shutdown()
        self.server_close()


def rtu_respond(frame, ds, unit):
    """Return the RTU response frame for a request frame, or None if not addressed / bad CRC."""
    if len(frame) < 4 or frame[0] != unit:
        return None
    if crc16(frame[:-2]) != struct.unpack("<H", frame[-2:])[0]:
        return None
    resp = bytes([unit]) + handle_pdu(ds, frame[1:-2])
    return resp + struct.pack("<H", crc16(resp))


class LoopbackRtuTransport:
    """Fake serial port wired straight to an in-memory RTU slave (for tests)."""

    def __init__(self, ds, unit=1):
        self.ds, self.unit = ds, unit
        self._rx = b""

    def reset_input_buffer(self):
        self._rx = b""

    def write(self, data):
        resp = rtu_respond(bytes(data), self.ds, self.unit)
        if resp:
            self._rx += resp

    def read(self, n):
        chunk, self._rx = self._rx[:n], self._rx[n:]
        if not chunk:
            time.sleep(0.01)
        return chunk

    def close(self):
        pass


# ---- demo meter register map (matches profiles/tbox-modbus-simulator.json) ----
def populate_meter(ds, t=None):
    t = time.time() if t is None else t
    w = 2 * math.pi / 60.0

    def f32(addr, v):
        ds.set_registers(addr, encode_value(v, "float32", "ABCD"))

    f32(0, 230 + 3 * math.sin(w * t))
    f32(2, 231 + 3 * math.sin(w * t + 2.1))
    f32(4, 229 + 3 * math.sin(w * t + 4.2))
    f32(6, 40 + 12 * math.sin(w * t / 2))
    f32(8, 50 + 0.05 * math.sin(w * t * 3))
    f32(10, 25 + 8 * math.sin(w * t / 2))
    kwh_tenths = int(t * 25.0 / 3600 * 10) & 0xFFFFFFFF
    ds.set_registers(12, encode_value(kwh_tenths, "uint32", "ABCD"))
    ds.set_registers(14, encode_value(int((45 + 5 * math.sin(w * t / 4)) * 10), "int16"))
    ds.set_registers(15, [1])
    with ds.lock:
        ds.coils[0] = True
        ds.coils[1] = False
        ds.discrete[0] = int(t) % 20 < 3


def main():
    ap = argparse.ArgumentParser(description="TBox Modbus TCP simulator (demo meter)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1502)
    ap.add_argument("--unit", type=int, default=1)
    a = ap.parse_args()
    ds = DataStore(1000)
    srv = ModbusTcpSimServer(a.host, a.port, ds, units=(a.unit,)).start()
    print("Modbus simulator listening on %s:%d (unit %d). Ctrl+C to stop." % (a.host, srv.port, a.unit))
    try:
        while True:
            populate_meter(ds)
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    main()
