"""Unit tests for register decoding (word order, dtypes, scaling)."""
import math
import struct

from aqms_logger.decode import decode_value


def _regs(value: float, fmt: str = ">f", order: str = "ABCD"):
    """Pack a value big-endian, then arrange the two 16-bit words per `order`."""
    b = struct.pack(fmt, value)
    a, bb, c, d = b[0], b[1], b[2], b[3]
    arranged = {
        "ABCD": (a, bb, c, d),
        "CDAB": (c, d, a, bb),
        "BADC": (bb, a, d, c),
        "DCBA": (d, c, bb, a),
    }[order]
    return [(arranged[0] << 8) | arranged[1], (arranged[2] << 8) | arranged[3]]


def test_float32_all_word_orders():
    for order in ("ABCD", "CDAB", "BADC", "DCBA"):
        regs = _regs(12.34, ">f", order)
        assert math.isclose(decode_value(regs, "float32", order), 12.34, rel_tol=1e-5)


def test_float32_wrong_word_order_is_wrong():
    # Decoding ABCD wire data as CDAB should NOT match — proves ordering matters.
    regs = _regs(12.34, ">f", "ABCD")
    assert not math.isclose(decode_value(regs, "float32", "CDAB"), 12.34, rel_tol=1e-3)


def test_pm_mg_to_ug_scale():
    # Fidas reports PM in mg/m3; ×1000 → µg/m3.
    regs = _regs(0.01234, ">f", "ABCD")
    assert math.isclose(decode_value(regs, "float32", "ABCD", scale=1000.0), 12.34, rel_tol=1e-4)


def test_uint16_and_int16():
    assert decode_value([1234], "uint16") == 1234
    assert decode_value([0xFFFF], "int16") == -1
    assert math.isclose(decode_value([100], "int16", scale=0.1), 10.0)


def test_uint32():
    regs = _regs(70000, ">I", "ABCD")
    assert decode_value(regs, "uint32", "ABCD") == 70000


def test_offset():
    regs = _regs(5.0, ">f", "ABCD")
    assert math.isclose(decode_value(regs, "float32", "ABCD", scale=1.0, offset=-2.0), 3.0)
