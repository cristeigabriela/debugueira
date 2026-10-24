"""Telling a cut-off minidump from a whole one, before anyone trusts what cdb reads from it.

cdb opens a truncated user dump without a word: memory past the cut reads as `??`, and stacks end early as
if that were the whole story. The dump's own header says how far its streams reach, so a short file can be
named when it opens. Kernel dumps have another layout and aren't checked.
"""

import os
import struct
from dataclasses import dataclass

MEMORY_LIST = 5  # MINIDUMP_MEMORY_DESCRIPTORs, each with its own location
MEMORY64_LIST = 9  # a /ma dump's memory: one base offset, ranges stored back to back after it


@dataclass
class Shortfall:
    size: int  # bytes on disk
    needed: int  # bytes the header's streams reach; a lower bound when the cut falls inside the directory


def shortfall(path: str) -> Shortfall | None:
    """How far a user-mode minidump falls short of what its header describes, or None if it's whole."""
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size

        def read(offset: int, n: int) -> bytes | None:
            if offset + n > size:
                return None
            f.seek(offset)
            return f.read(n)

        header = read(0, 32)
        if header is None or header[:4] != b"MDMP":
            return None
        count, directory = struct.unpack_from("<II", header, 8)
        needed = directory + count * 12
        for i in range(count):
            entry = read(directory + i * 12, 12)
            if entry is None:
                break  # the directory itself is cut, and `needed` already reaches past the end
            kind, data_size, rva = struct.unpack("<III", entry)
            needed = max(needed, rva + data_size)
            if kind == MEMORY64_LIST and (head := read(rva, 16)):
                ranges, base = struct.unpack("<QQ", head)
                if table := read(rva + 16, ranges * 16):
                    needed = max(needed, base + sum(s for _, s in struct.iter_unpack("<QQ", table)))
            elif kind == MEMORY_LIST and (head := read(rva, 4)):
                (ranges,) = struct.unpack("<I", head)
                if table := read(rva + 4, ranges * 16):
                    needed = max(needed, *(at + n for _, n, at in struct.iter_unpack("<QII", table)))
    return Shortfall(size, needed) if needed > size else None
