"""TBox Modbus library (TCP + RTU clients, codec, simulator). Standard library only."""
from .codec import DATA_TYPES, BYTE_ORDERS, decode_value, encode_value, word_count  # noqa: F401
from .client import ModbusTcpClient, ModbusRtuClient, ModbusError, crc16  # noqa: F401

__version__ = "0.1.0"
