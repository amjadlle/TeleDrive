"""Streaming CRC32C (Castagnoli) for S3 upload integrity checks.

Portable pure-Python implementation: reflected polynomial 0x82F63B78.
This checksum detects accidental corruption; it does not authenticate data.
"""
from __future__ import annotations


def _table() -> tuple[int, ...]:
    values = []
    for index in range(256):
        word = index
        for _ in range(8):
            word = (word >> 1) ^ (0x82F63B78 if word & 1 else 0)
        values.append(word & 0xFFFFFFFF)
    return tuple(values)


_TABLE = _table()


def crc32c(data: bytes, prior: int = 0) -> int:
    """Return the finalized CRC32C, allowing incremental block updates."""
    if not 0 <= prior <= 0xFFFFFFFF:
        raise ValueError("Invalid CRC32C accumulator")
    state = prior ^ 0xFFFFFFFF
    for byte in data:
        state = _TABLE[(state ^ byte) & 255] ^ (state >> 8)
    return state ^ 0xFFFFFFFF


# CRC-64/NVME (reflected polynomial 0x9A6C9329AC4BC9B5).
# Initial value and final XOR are both all ones (CRC-64/NVME check:
# b"123456789" -> 0xAE8B14860A799888).
def _crc64nvme_table() -> tuple[int, ...]:
    entries = []
    for index in range(256):
        word = index
        for _ in range(8):
            word = (word >> 1) ^ (0x9A6C9329AC4BC9B5 if word & 1 else 0)
        entries.append(word & 0xFFFFFFFFFFFFFFFF)
    return tuple(entries)


_CRC64NVME_TABLE = _crc64nvme_table()


def crc64nvme(data: bytes, prior: int = 0) -> int:
    """Return finalized CRC-64/NVME; pass the previous result for streaming."""
    if not 0 <= prior <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("Invalid CRC64NVME accumulator")
    state = prior ^ 0xFFFFFFFFFFFFFFFF
    for byte in data:
        state = _CRC64NVME_TABLE[(state ^ byte) & 255] ^ (state >> 8)
    return state ^ 0xFFFFFFFFFFFFFFFF
