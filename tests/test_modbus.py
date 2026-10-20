import unittest
import helpers  # noqa: F401  (sets sys.path)
from tbox_modbus import BYTE_ORDERS, ModbusError, ModbusRtuClient, ModbusTcpClient, crc16, decode_value, encode_value
from tbox_modbus.sim import DataStore, LoopbackRtuTransport, ModbusTcpSimServer, populate_meter


class CodecTests(unittest.TestCase):
    def test_roundtrip_all_orders(self):
        cases = [("float32", 123.456), ("uint32", 4_000_000_000), ("int32", -70000), ("int16", -5), ("uint16", 65535),
                 ("float64", -1.5e10), ("int64", -123456789012), ("uint64", 2**63 + 5)]
        for order in BYTE_ORDERS:
            for dt, v in cases:
                got = decode_value(encode_value(v, dt, order), dt, order)
                self.assertAlmostEqual(got, v, delta=abs(v) * 1e-6 + 1e-6, msg=(order, dt))

    def test_known_vectors(self):
        self.assertAlmostEqual(decode_value([0x42F6, 0xE979], "float32", "ABCD"), 123.456, places=3)
        self.assertAlmostEqual(decode_value([0xE979, 0x42F6], "float32", "CDAB"), 123.456, places=3)
        self.assertEqual(decode_value([0x0001, 0x0000], "uint32", "CDAB"), 1)
        self.assertEqual(decode_value([0x0100], "uint16", "BADC"), 1)

    def test_bit_and_bool(self):
        self.assertTrue(decode_value([0b100], "bool", bit=2))
        self.assertFalse(decode_value([0b100], "bool", bit=1))

    def test_nan_rejected(self):
        with self.assertRaises(ValueError):
            decode_value([0x7FC0, 0x0000], "float32")

    def test_crc(self):
        self.assertEqual(crc16(bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x0A])), 0xCDC5)


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = DataStore(1000)
        populate_meter(cls.ds, 1000.0)
        cls.srv = ModbusTcpSimServer("127.0.0.1", 0, cls.ds).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_tcp_reads(self):
        c = ModbusTcpClient("127.0.0.1", self.srv.port, 1, 2)
        self.assertAlmostEqual(decode_value(c.read_holding_registers(8, 2), "float32"), 50.0, delta=0.1)
        self.assertEqual(c.read_coils(0, 2), [True, False])
        self.assertEqual(len(c.read_holding_registers(0, 200)), 200)  # chunked
        c.close()

    def test_tcp_exception(self):
        c = ModbusTcpClient("127.0.0.1", self.srv.port, 1, 2)
        with self.assertRaises(ModbusError) as cm:
            c.read_holding_registers(5000, 2)
        self.assertEqual((cm.exception.kind, cm.exception.code), ("exception", 2))

    def test_tcp_connection_refused(self):
        c = ModbusTcpClient("127.0.0.1", 1, 1, 0.5)
        with self.assertRaises(ModbusError) as cm:
            c.read_holding_registers(0, 1)
        self.assertEqual(cm.exception.kind, "connection")

    def test_tcp_timeout_wrong_unit(self):
        c = ModbusTcpClient("127.0.0.1", self.srv.port, 9, 0.4)
        with self.assertRaises(ModbusError) as cm:
            c.read_holding_registers(0, 1)
        self.assertEqual(cm.exception.kind, "timeout")

    def test_rtu_loopback(self):
        c = ModbusRtuClient(transport=LoopbackRtuTransport(self.ds, 1), unit=1, timeout=0.5, baudrate=115200)
        self.assertAlmostEqual(decode_value(c.read_holding_registers(8, 2), "float32"), 50.0, delta=0.1)
        self.assertEqual(c.read_coils(0, 2), [True, False])
        with self.assertRaises(ModbusError) as cm:
            c.read_holding_registers(5000, 1)
        self.assertEqual(cm.exception.code, 2)
        bad = ModbusRtuClient(transport=LoopbackRtuTransport(self.ds, 1), unit=7, timeout=0.2, baudrate=115200)
        with self.assertRaises(ModbusError) as cm:
            bad.read_holding_registers(0, 1)
        self.assertEqual(cm.exception.kind, "timeout")

    def test_write_roundtrip(self):
        c = ModbusTcpClient("127.0.0.1", self.srv.port, 1, 2)
        c.write_multiple_registers(100, [1, 2, 3])
        self.assertEqual(c.read_holding_registers(100, 3), [1, 2, 3])
        c.write_single_coil(50, True)
        self.assertEqual(c.read_coils(50, 1), [True])


if __name__ == "__main__":
    unittest.main()
