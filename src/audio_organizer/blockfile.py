"""A read-only file object that reads the underlying file in large, cached blocks.

Reading a file's tags and hashing its audio both jump around the file: the tag area at
the start, the end for ID3v1 and APE tags, a few audio samples. On a network share every
read is a round trip, so it pays to read in large blocks, each at most once, and to
share them between the tag reader and the hasher.
"""

import io
import os
from pathlib import Path

# Measured on a NAS (tags + sampled hash per file): 256 KB blocks read ~940 KB per file
# in ~4 reads, 64 KB ~470 KB in ~7, 32 KB ~370 KB in ~12. With parallel reads hiding
# part of the latency, bytes matter more than round trips.
BLOCK_SIZE = 64 * 1024


class BlockFile(io.RawIOBase):
    def __init__(self, path: str | Path, block_size: int = BLOCK_SIZE) -> None:
        self.name = os.fspath(path)  # mutagen uses the name to guess the format
        self._file = open(path, "rb", buffering=0)
        self._size = os.fstat(self._file.fileno()).st_size
        self._block_size = block_size
        self._blocks: dict[int, bytes] = {}
        self._pos = 0
        self.reads = 0  # round trips to the underlying file, for measurements

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            pos = offset
        elif whence == os.SEEK_CUR:
            pos = self._pos + offset
        elif whence == os.SEEK_END:
            pos = self._size + offset
        else:
            raise ValueError(f"invalid whence {whence}")
        if pos < 0:
            raise OSError("negative seek position")
        self._pos = pos
        return pos

    def readinto(self, buffer) -> int:
        view = memoryview(buffer).cast("B")
        end = min(self._pos + len(view), self._size)
        written = 0
        while self._pos < end:
            index, offset = divmod(self._pos, self._block_size)
            block = self._block(index)
            chunk = block[offset : offset + end - self._pos]
            if not chunk:
                break
            view[written : written + len(chunk)] = chunk
            written += len(chunk)
            self._pos += len(chunk)
        return written

    def _block(self, index: int) -> bytes:
        block = self._blocks.get(index)
        if block is None:
            self._file.seek(index * self._block_size)
            block = self._file.read(self._block_size)
            self._blocks[index] = block
            self.reads += 1
        return block

    def close(self) -> None:
        if not self.closed:
            self._file.close()
            self._blocks.clear()
        super().close()
