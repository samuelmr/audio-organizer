"""A hash of the audio data alone, so that retagging a file does not change its identity.

MP3 (and other ID3-tagged streams): the ID3v2 header, trailing ID3v1 and APEv2 tags
are skipped. MP4/M4A: only the contents of the 'mdat' atoms are hashed. Other formats
are hashed whole for now.
"""

import hashlib
import os
import struct
from pathlib import Path

CHUNK = 1 << 20


def audio_hash(path: str | Path) -> str:
    with open(path, "rb") as f:
        head = f.read(12)
        f.seek(0)
        if len(head) >= 8 and head[4:8] == b"ftyp":
            ranges = _mp4_ranges(f)
        else:
            ranges = [_id3_stream_range(f)]
        digest = hashlib.blake2b(digest_size=16)
        for start, end in ranges:
            f.seek(start)
            remaining = end - start
            while remaining > 0:
                chunk = f.read(min(CHUNK, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
    return digest.hexdigest()


def _id3_stream_range(f) -> tuple[int, int]:
    size = os.fstat(f.fileno()).st_size
    start = 0
    # ID3v2 tags at the start; some files carry several
    while True:
        f.seek(start)
        header = f.read(10)
        if len(header) < 10 or header[:3] != b"ID3":
            break
        tag_size = _syncsafe(header[6:10])
        footer = 10 if header[5] & 0x10 else 0
        start += 10 + tag_size + footer
    end = size
    # ID3v1 at the very end
    if end - start >= 128:
        f.seek(end - 128)
        if f.read(3) == b"TAG":
            end -= 128
    # APEv2 footer before that (Lyrics3 is ignored)
    if end - start >= 32:
        f.seek(end - 32)
        footer = f.read(32)
        if footer[:8] == b"APETAGEX":
            tag_size, flags = struct.unpack("<II", footer[12:16] + footer[20:24])
            has_header = bool(flags & 0x80000000)
            end -= tag_size + (32 if has_header else 0)
    return start, max(start, end)


def _mp4_ranges(f) -> list[tuple[int, int]]:
    size = os.fstat(f.fileno()).st_size
    ranges = []
    pos = 0
    while pos + 8 <= size:
        f.seek(pos)
        atom_size, atom_type = struct.unpack(">I4s", f.read(8))
        header = 8
        if atom_size == 1:
            atom_size = struct.unpack(">Q", f.read(8))[0]
            header = 16
        elif atom_size == 0:
            atom_size = size - pos
        if atom_size < header:
            break  # corrupt; hash what we have
        if atom_type == b"mdat":
            ranges.append((pos + header, min(pos + atom_size, size)))
        pos += atom_size
    return ranges or [(0, size)]


def _syncsafe(data: bytes) -> int:
    n = 0
    for byte in data:
        n = (n << 7) | (byte & 0x7F)
    return n
