"""Modbus TCP and Modbus RTU clients using only the Python standard library.

RTU needs the optional `pyserial` package for a real serial port; the framing
and CRC logic is pure Python and can be exercised with any transport object
that implements write(), read(n), reset_input_buffer() and close().
"""
import socket
import struct
import threading
import time

EXCEPTION_TEXT = {
    1: "Illegal function",
    2: "Illegal data address",
    3: "Illegal data value",
    4: "Slave device failure",
    5: "Acknowledge",
    6: "Slave device busy",
    8: "Memory parity error",
    10: "Gateway path unavailable",
    11: "Gateway target device failed to respond",
}

FC_READ_COILS = 1
FC_READ_DISCRETE = 2
FC_READ_HOLDING = 3
FC_READ_INPUT = 4
FC_WRITE_COIL = 5
FC_WRITE_REGISTER = 6
FC_WRITE_REGISTERS = 16


class ModbusError(Exception):
    """kind: connection | timeout | protocol | exception"""

    def __init__(self, message, code=None, kind="protocol"):
        super().__init__(message)
        self.code = code
        self.kind = kind


def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


class _ModbusBase:
    unit = 1

    def _transact(self, pdu):  # pragma: no cover - abstract
        raise NotImplementedError

    def close(self):  # pragma: no cover - abstract
        pass

    @staticmethod
    def _check(resp, fc):
        if not resp:
            raise ModbusError("empty response")
        if resp[0] == (fc | 0x80):
            code = resp[1] if len(resp) > 1 else None
            raise ModbusError("Modbus exception %s: %s" % (code, EXCEPTION_TEXT.get(code, "unknown")),
                              code=code, kind="exception")
        if resp[0] != fc:
            raise ModbusError("unexpected function code %d in response" % resp[0])

    def read_bits(self, fc, address, count):
        out = []
        while count > 0:
            n = min(count, 2000)
            resp = self._transact(struct.pack(">BHH", fc, address, n))
            self._check(resp, fc)
            nbytes = resp[1]
            if len(resp) < 2 + nbytes or nbytes < (n + 7) // 8:
                raise ModbusError("short bit response")
            data = resp[2:2 + nbytes]
            out.extend(bool(data[i // 8] >> (i % 8) & 1) for i in range(n))
            address += n
            count -= n
        return out

    def read_registers(self, fc, address, count):
        out = []
        while count > 0:
            n = min(count, 125)
            resp = self._transact(struct.pack(">BHH", fc, address, n))
            self._check(resp, fc)
            if resp[1] != 2 * n or len(resp) < 2 + 2 * n:
                raise ModbusError("short register response")
            out.extend(struct.unpack(">%dH" % n, resp[2:2 + 2 * n]))
            address += n
            count -= n
        return out

    def read_coils(self, address, count):
        return self.read_bits(FC_READ_COILS, address, count)

    def read_discrete_inputs(self, address, count):
        return self.read_bits(FC_READ_DISCRETE, address, count)

    def read_holding_registers(self, address, count):
        return self.read_registers(FC_READ_HOLDING, address, count)

    def read_input_registers(self, address, count):
        return self.read_registers(FC_READ_INPUT, address, count)

    # --- write helpers: part of the library, NOT used by TBox in the read-only phase ---
    def write_single_coil(self, address, value):
        pdu = struct.pack(">BHH", FC_WRITE_COIL, address, 0xFF00 if value else 0)
        self._check(self._transact(pdu), FC_WRITE_COIL)

    def write_single_register(self, address, value):
        pdu = struct.pack(">BHH", FC_WRITE_REGISTER, address, value & 0xFFFF)
        self._check(self._transact(pdu), FC_WRITE_REGISTER)

    def write_multiple_registers(self, address, values):
        pdu = struct.pack(">BHHB", FC_WRITE_REGISTERS, address, len(values), 2 * len(values))
        pdu += struct.pack(">%dH" % len(values), *[v & 0xFFFF for v in values])
        self._check(self._transact(pdu), FC_WRITE_REGISTERS)


class ModbusTcpClient(_ModbusBase):
    def __init__(self, host, port=502, unit=1, timeout=3.0):
        self.host, self.port, self.unit, self.timeout = host, int(port), int(unit), float(timeout)
        self._sock = None
        self._tid = 0
        self._lock = threading.Lock()

    @property
    def is_open(self):
        return self._sock is not None

    def connect(self):
        self.close()
        try:
            s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except socket.timeout:
            raise ModbusError("connection to %s:%s timed out" % (self.host, self.port), kind="timeout")
        except OSError as e:
            raise ModbusError("cannot connect to %s:%s (%s)" % (self.host, self.port, e), kind="connection")
        s.settimeout(self.timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = s

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("connection closed by peer")
            buf += chunk
        return buf

    def _transact(self, pdu):
        with self._lock:
            if self._sock is None:
                self.connect()
            self._tid = (self._tid + 1) & 0xFFFF
            frame = struct.pack(">HHHB", self._tid, 0, len(pdu) + 1, self.unit) + pdu
            try:
                self._sock.sendall(frame)
                hdr = self._recv_exact(7)
                tid, pid, length, _unit = struct.unpack(">HHHB", hdr)
                if pid != 0 or length < 2 or length > 254:
                    raise ModbusError("invalid MBAP header")
                body = self._recv_exact(length - 1)
            except socket.timeout:
                self.close()
                raise ModbusError("response timed out", kind="timeout")
            except (OSError, ConnectionError) as e:
                self.close()
                raise ModbusError("connection lost (%s)" % e, kind="connection")
            except ModbusError:
                self.close()
                raise
            if tid != self._tid:
                self.close()
                raise ModbusError("transaction id mismatch")
            return body


class ModbusRtuClient(_ModbusBase):
    def __init__(self, port=None, baudrate=9600, parity="N", stopbits=1, bytesize=8,
                 unit=1, timeout=1.0, transport=None):
        self.port, self.baudrate, self.parity = port, int(baudrate), parity
        self.stopbits, self.bytesize = stopbits, int(bytesize)
        self.unit, self.timeout = int(unit), float(timeout)
        self._transport = transport
        self._lock = threading.Lock()

    @property
    def is_open(self):
        return self._transport is not None

    def connect(self):
        if self._transport is not None:
            return
        try:
            import serial  # pyserial
        except ImportError:
            raise ModbusError("pyserial is not installed on this gateway (pip install pyserial)",
                              kind="connection")
        try:
            self._transport = serial.Serial(self.port, self.baudrate, bytesize=self.bytesize,
                                            parity=self.parity, stopbits=self.stopbits,
                                            timeout=self.timeout)
        except Exception as e:  # serial.SerialException and OS errors
            raise ModbusError("cannot open serial port %s (%s)" % (self.port, e), kind="connection")

    def close(self):
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
            self._transport = None

    def _read_exact(self, n):
        buf = b""
        deadline = time.monotonic() + self.timeout
        while len(buf) < n and time.monotonic() < deadline:
            chunk = self._transport.read(n - len(buf))
            if chunk:
                buf += chunk
        if len(buf) < n:
            raise ModbusError("response timed out", kind="timeout")
        return buf

    def _frame_gap(self):
        bits = 1 + self.bytesize + (0 if self.parity == "N" else 1) + (2 if self.stopbits == 2 else 1)
        return 0.00175 if self.baudrate > 19200 else 3.5 * bits / max(self.baudrate, 1)

    def _transact(self, pdu):
        frame = bytes([self.unit]) + pdu
        frame += struct.pack("<H", crc16(frame))
        with self._lock:
            self.connect()
            try:
                try:
                    self._transport.reset_input_buffer()
                except AttributeError:
                    pass
                time.sleep(self._frame_gap())
                self._transport.write(frame)
                head = self._read_exact(2)
                fc = head[1]
                if fc & 0x80:
                    rest = self._read_exact(3)
                elif fc in (1, 2, 3, 4):
                    cnt = self._read_exact(1)
                    rest = cnt + self._read_exact(cnt[0] + 2)
                else:
                    rest = self._read_exact(6)
            except ModbusError as e:
                if e.kind == "timeout":
                    raise
                self.close()
                raise
            except Exception as e:
                self.close()
                raise ModbusError("serial I/O error (%s)" % e, kind="connection")
            resp = head + rest
            if crc16(resp[:-2]) != struct.unpack("<H", resp[-2:])[0]:
                raise ModbusError("CRC error")
            if resp[0] != self.unit:
                raise ModbusError("response from unexpected slave id %d" % resp[0])
            return resp[1:-2]
