"""Read `general.architecture` from the start of a GGUF file.

Only the header is read. The reader needs the first few hundred kilobytes of the
file at most, so a range request is enough.
"""

from __future__ import annotations

import struct

MAGIC = b"GGUF"

_SCALARS = {
    0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2), 4: ("<I", 4), 5: ("<i", 4),
    6: ("<f", 4), 7: ("<?", 1), 10: ("<Q", 8), 11: ("<q", 8), 12: ("<d", 8),
}
_STRING = 8
_ARRAY = 9


class NeedMoreBytes(Exception):
    """The bytes given end before the architecture key was found."""


class NotGguf(ValueError):
    """The bytes do not start with a GGUF header this reader understands."""


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, count: int) -> bytes:
        if self.pos + count > len(self.data):
            raise NeedMoreBytes
        chunk = self.data[self.pos : self.pos + count]
        self.pos += count
        return chunk

    def unpack(self, fmt: str, size: int):
        return struct.unpack(fmt, self.take(size))[0]

    def string(self) -> str:
        length = self.unpack("<Q", 8)
        return self.take(length).decode("utf-8", errors="replace")

    def skip_value(self, kind: int) -> None:
        if kind in _SCALARS:
            self.take(_SCALARS[kind][1])
        elif kind == _STRING:
            self.string()
        elif kind == _ARRAY:
            inner = self.unpack("<I", 4)
            count = self.unpack("<Q", 8)
            for _ in range(count):
                self.skip_value(inner)
        else:
            raise NotGguf(f"unknown value type {kind}")


def read_architecture(data: bytes) -> str:
    """Return `general.architecture`, or raise NeedMoreBytes or NotGguf."""
    reader = _Reader(data)
    if reader.take(4) != MAGIC:
        raise NotGguf("the file does not start with GGUF")
    version = reader.unpack("<I", 4)
    if version < 2:
        raise NotGguf(f"GGUF version {version} is not supported")
    reader.unpack("<Q", 8)  # tensor count
    pairs = reader.unpack("<Q", 8)
    for _ in range(pairs):
        key = reader.string()
        kind = reader.unpack("<I", 4)
        if key == "general.architecture":
            if kind != _STRING:
                raise NotGguf("general.architecture is not a string")
            return reader.string()
        reader.skip_value(kind)
    raise NotGguf("general.architecture is missing")
