"""Driver registry. A protocol is only advertised if its driver can actually run on this gateway."""
from .modbus import ModbusSession


def available_protocols():
    protos = ["modbus_tcp"]
    try:
        import serial  # noqa: F401
        protos.append("modbus_rtu")
    except ImportError:
        pass
    return protos


def get_driver(protocol):
    if protocol in ("modbus_tcp", "modbus_rtu"):
        return ModbusSession
    return None  # mqtt / opcua: drivers not implemented in this release
