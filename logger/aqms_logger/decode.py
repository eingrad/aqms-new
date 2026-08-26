"""Decode Modbus register words into Python numbers.

Modbus transmits each 16-bit register big-endian. A 32-bit value spans two
registers whose *word order* varies by device, so it is configurable:

    ABCD  big-endian, high word first      (Modbus default)
    CDAB  word-swapped                      (very common on field devices)
    BADC  byte-swapped within each word
    DCBA  fully reversed

The Fidas 200 delivers IEEE-754 float32 values; word order is verified once
against the instrument display (default ABCD, try CDAB if values look wrong).
"""
from __future__ import annotations

import struct

WORD_ORDERS = {"ABCD", "CDAB", "BADC", "DCBA"}


def _registers_to_bytes(registers: list[int], word_order: str) -> bytes:
    """Turn 16-bit registers into an ordered byte string for struct.unpack('>...')."""
    if word_order not in WORD_ORDERS:
        raise ValueError(f"unknown word_order {word_order!r}; expected one of {sorted(WORD_ORDERS)}")
    # Baseline ABCD: registers in the order read, each packed big-endian.
    raw = b"".join(struct.pack(">H", r & 0xFFFF) for r in registers)
    if len(registers) != 2:
        return raw  # single-register types don't need reordering
    a, b, c, d = raw[0], raw[1], raw[2], raw[3]
    return {
        "ABCD": bytes((a, b, c, d)),
        "CDAB": bytes((c, d, a, b)),
        "BADC": bytes((b, a, d, c)),
        "DCBA": bytes((d, c, b, a)),
    }[word_order]


def decode_value(
    registers: list[int],
    dtype: str,
    word_order: str = "ABCD",
    scale: float = 1.0,
    offset: float = 0.0,
) -> float:
    """Decode registers → engineering value = raw * scale + offset."""
    dtype = dtype.lower()

    if dtype in ("uint16", "int16"):
        raw = registers[0] & 0xFFFF
        if dtype == "int16" and raw >= 0x8000:
            raw -= 0x10000
        num: float = raw
    elif dtype in ("float32", "uint32", "int32"):
        if len(registers) < 2:
            raise ValueError(f"{dtype} needs 2 registers, got {len(registers)}")
        packed = _registers_to_bytes(registers[:2], word_order)
        fmt = {"float32": ">f", "uint32": ">I", "int32": ">i"}[dtype]
        num = struct.unpack(fmt, packed)[0]
    else:
        raise ValueError(f"unsupported dtype {dtype!r}")

    return num * scale + offset
