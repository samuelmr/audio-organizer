"""A hash of the audio data alone, so that retagging a file does not change its identity.

The audio data ("payload") is found by skipping tags. MP3 (and other ID3-tagged
streams): the ID3v2 header, trailing ID3v1 and APEv2 tags. MP4/M4A: everything but the
contents of the 'mdat' atoms. Other formats: nothing is skipped for now.

To keep scans of large libraries on network shares fast, the hash covers the payload's
length and three samples of it (start, middle, end) rather than all of it: about 200 KB
per track instead of several MB. Two files get the same hash only if their audio has the
same length and the same bytes in all three samples, which in practice means the same
audio; a copy damaged only between the samples would go unnoticed.
"""

import hashlib
import os
import struct
from contextlib import nullcontext
from pathlib import Path
from typing import BinaryIO

SAMPLE = 64 * 1024


def audio_hash(source: str | Path | BinaryIO) -> str:
    """Hash of a file given by path, or of an open, seekable binary file object."""
    context = open(source, "rb") if isinstance(source, str | Path) else nullcontext(source)
    with context as f:
        f.seek(0)
        head = f.read(12)
        if len(head) >= 8 and head[4:8] == b"ftyp":
            ranges = _mp4_ranges(f)
        else:
            ranges = [_id3_stream_range(f)]
        total = sum(end - start for start, end in ranges)
        digest = hashlib.blake2b(total.to_bytes(8, "big"), digest_size=16)
        for offset, length in _samples(total):
            digest.update(_read_payload(f, ranges, offset, length))
    return digest.hexdigest()


def _samples(total: int) -> list[tuple[int, int]]:
    """(offset, length) of the payload samples to hash: all of a short payload."""
    if total <= 3 * SAMPLE:
        return [(0, total)]
    return [(0, SAMPLE), (total // 2 - SAMPLE // 2, SAMPLE), (total - SAMPLE, SAMPLE)]


def _read_payload(f: BinaryIO, ranges: list[tuple[int, int]], offset: int, length: int) -> bytes:
    """Read from the payload, which may consist of several ranges of the file."""
    parts = []
    for start, end in ranges:
        if length <= 0:
            break
        if offset >= end - start:
            offset -= end - start
            continue
        f.seek(start + offset)
        part = f.read(min(length, end - start - offset))
        parts.append(part)
        length -= len(part)
        offset = 0
    return b"".join(parts)


def _size(f: BinaryIO) -> int:
    size = f.seek(0, os.SEEK_END)
    f.seek(0)
    return size


def _id3_stream_range(f) -> tuple[int, int]:
    size = _size(f)
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
    size = _size(f)
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
