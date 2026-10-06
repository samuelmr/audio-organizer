import os
import random

from audio_organizer import tags
from audio_organizer.audiohash import SAMPLE, audio_hash
from audio_organizer.blockfile import BlockFile


def test_behaves_like_a_file(tmp_path):
    data = random.Random(1).randbytes(10_000)
    path = tmp_path / "data"
    path.write_bytes(data)
    rng = random.Random(2)
    with BlockFile(path, block_size=1024) as f, open(path, "rb") as plain:
        for _ in range(200):
            whence = rng.choice([os.SEEK_SET, os.SEEK_CUR, os.SEEK_END])
            offset = rng.randrange(-500, 500) if whence != os.SEEK_SET else rng.randrange(11_000)
            if plain.seek(0, os.SEEK_CUR) + offset < 0 and whence == os.SEEK_CUR:
                continue
            if whence == os.SEEK_END and offset < -10_000:
                continue
            assert f.seek(offset, whence) == plain.seek(offset, whence)
            n = rng.choice([-1, 0, 1, 7, 1024, 3000])
            assert f.read(n) == plain.read(n)
            assert f.tell() == plain.tell()
        assert f.reads <= 10  # each block read once


def test_reads_each_block_once(tmp_path):
    path = tmp_path / "data"
    path.write_bytes(bytes(8 * 1024))
    with BlockFile(path, block_size=1024) as f:
        f.read(100)
        f.seek(-100, os.SEEK_END)
        f.read()
        f.seek(50)
        f.read(10)
        assert f.reads == 2


def test_tags_and_hash_through_blockfile(copy_audio, tmp_path):
    for name in ("mp3", "m4a"):
        path = copy_audio(name, tmp_path / f"x.{name}")
        with BlockFile(path, block_size=4096) as f:
            assert tags.read(f) == tags.read(path)
            assert audio_hash(f) == audio_hash(path)


def _raw_file(tmp_path, data: bytes):
    path = tmp_path / "raw.mp3"
    path.write_bytes(data)
    return path


def test_sampled_hash_trade_off(tmp_path):
    data = bytearray(random.Random(3).randbytes(1_000_000))
    before = audio_hash(_raw_file(tmp_path, bytes(data)))

    unsampled = bytearray(data)
    unsampled[300_000] ^= 0xFF  # between the start and middle samples
    assert audio_hash(_raw_file(tmp_path, bytes(unsampled))) == before

    for position in (10, len(data) // 2, len(data) - 10):
        changed = bytearray(data)
        changed[position] ^= 0xFF
        assert audio_hash(_raw_file(tmp_path, bytes(changed))) != before, position

    longer = bytes(data) + b"\0"
    assert audio_hash(_raw_file(tmp_path, longer)) != before


def test_short_audio_is_hashed_whole(tmp_path):
    data = bytearray(random.Random(4).randbytes(3 * SAMPLE))
    before = audio_hash(_raw_file(tmp_path, bytes(data)))
    data[SAMPLE + 5] ^= 0xFF
    assert audio_hash(_raw_file(tmp_path, bytes(data))) != before
