"""Register encoding/decoding for Modbus data types and byte/word orders.

Byte-order names follow the usual convention for a 32-bit value whose
big-endian bytes are A B C D (A = most significant):

    ABCD  big-endian                      (regs: AB CD)
    CDAB  big-endian bytes, swapped words (regs: CD AB)
    BADC  swapped bytes inside each word  (regs: BA DC)
    DCBA  fully little-endian             (regs: DC BA)

The same rule is applied to 64-bit values (word list reversed / bytes swapped).
"""
import math
import struct

DATA_TYPES = {
    "bool": (1, None),
    "int16": (1, ">h"),
    "uint16": (1, ">H"),
    "int32": (2, ">i"),
    "uint32": (2, ">I"),
    "float32": (2, ">f"),
    "int64": (4, ">q"),
    "uint64": (4, ">Q"),
    "float64": (4, ">d"),
}
BYTE_ORDERS = ("ABCD", "CDAB", "BADC", "DCBA")


def word_count(data_type):
    try:
        return DATA_TYPES[data_type][0]
    except KeyError:
        raise ValueError("unsupported data type: %r" % (data_type,))


def _check_order(order):
    if order not in BYTE_ORDERS:
        raise ValueError("unsupported byte order: %r" % (order,))


def _canonical_bytes(words, order):
    """Registers as read from the wire -> big-endian bytes of the value."""
    _check_order(order)
    ws = list(words)
    if order in ("CDAB", "DCBA"):
        ws.reverse()
    out = bytearray()
    for w in ws:
        hi, lo = (w >> 8) & 0xFF, w & 0xFF
        out += bytes((lo, hi)) if order in ("BADC", "DCBA") else bytes((hi, lo))
    return bytes(out)


def _words_from_canonical(raw, order):
    _check_order(order)
    ws = []
    for i in range(0, len(raw), 2):
        hi, lo = raw[i], raw[i + 1]
        ws.append((lo << 8) | hi if order in ("BADC", "DCBA") else (hi << 8) | lo)
    if order in ("CDAB", "DCBA"):
        ws.reverse()
    return ws


def decode_value(words, data_type, order="ABCD", bit=None):
    """Decode registers to a Python value. Raises ValueError on bad data."""
    n = word_count(data_type)
    if len(words) < n:
        raise ValueError("need %d registers for %s, got %d" % (n, data_type, len(words)))
    words = list(words[:n])
    if data_type == "bool":
        if bit is not None:
            return bool((words[0] >> int(bit)) & 1)
        return words[0] != 0
    raw = _canonical_bytes(words, order)
    value = struct.unpack(DATA_TYPES[data_type][1], raw)[0]
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise ValueError("non-finite floating point value")
    return value


def encode_value(value, data_type, order="ABCD"):
    """Encode a Python value to a list of 16-bit registers."""
    if data_type == "bool":
        return [1 if value else 0]
    fmt = DATA_TYPES[data_type][1]
    if fmt[-1] in "fd":
        raw = struct.pack(fmt, float(value))
    else:
        raw = struct.pack(fmt, int(value))
    return _words_from_canonical(raw, order)
