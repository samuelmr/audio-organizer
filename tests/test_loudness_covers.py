import re
import shutil
import subprocess

import pytest
from conftest import make_audio
from sqlalchemy import select

from audio_organizer import covers, loudness, tags
from audio_organizer.models import AudioFile, Library
from audio_organizer.scanner import scan


def test_measure(copy_audio, tmp_path):
    values = loudness.measure(copy_audio("mp3", tmp_path / "x.mp3"))
    assert re.fullmatch(r"[+-]\d+\.\d\d dB", values["replaygain_track_gain"])
    assert 0 < float(values["replaygain_track_peak"]) <= 1.5
    gain = float(values["replaygain_track_gain"].split()[0])
    assert gain == pytest.approx(-18 - float(values[loudness.LOUDNESS]), abs=0.01)


def test_deep_scan(Session, tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    root = tmp_path / "music"
    quiet = make_audio(root / "Air/Moon Safari/01 Quiet.mp3", freq=440, seconds=2,
                       album="Moon Safari", title="Quiet", track="1")  # fmt: skip
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2,volume=8", "-c:a", "libmp3lame",
                    "-id3v2_version", "3", "-metadata", "album=Moon Safari",
                    "-metadata", "track=2", str(root / "Air/Moon Safari/02 Loud.mp3")],
                   check=True)  # fmt: skip
    with Session() as session:
        lib = Library(name="music", root=str(root))
        session.add(lib)
        session.commit()
        scan(session, lib)

        stats = loudness.measure_library(session, lib, limit=1)
        assert (stats.measured, stats.albums) == (1, 0)  # the album is not complete yet
        stats = loudness.measure_library(session, lib)
        assert (stats.measured, stats.albums) == (1, 1)  # continues with the other one
        assert loudness.measure_library(session, lib).measured == 0

        def analysis(path):
            file = session.scalar(select(AudioFile).where(AudioFile.path == path))
            return {fv.field: fv.value for fv in file.values if fv.source == "analysis"}

        a, b = analysis("Air/Moon Safari/01 Quiet.mp3"), analysis("Air/Moon Safari/02 Loud.mp3")
        gains = [float(v["replaygain_track_gain"].split()[0]) for v in (a, b)]
        album_gain = float(a["replaygain_album_gain"].split()[0])
        assert min(gains) < album_gain < max(gains)
        assert a["replaygain_album_gain"] == b["replaygain_album_gain"]
        assert a["replaygain_album_peak"] == b["replaygain_album_peak"]

        # A retag keeps the measurement; new audio drops it
        tags.write(quiet, {"title": "Quiet (retagged)"})
        scan(session, lib)
        assert loudness.LOUDNESS in analysis("Air/Moon Safari/01 Quiet.mp3")
        shutil.copyfile(root / "Air/Moon Safari/02 Loud.mp3", quiet)
        scan(session, lib)
        assert analysis("Air/Moon Safari/01 Quiet.mp3") == {}


def test_measure_fails_clearly(tmp_path):
    path = tmp_path / "x.mp3"
    path.write_bytes(b"not audio")
    with pytest.raises(loudness.LoudnessError, match="Could not measure x.mp3"):
        loudness.measure(path)


def test_measure_mp3_in_a_wav_container_behind_an_id3_tag(tmp_path):
    """Old rippers wrote an MP3 stream in a RIFF/WAVE container and put an ID3 tag in front;
    ffmpeg fails on those unless it starts at the container."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    wav = tmp_path / "riff.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=2",
                    "-c:a", "libmp3lame", "-f", "wav", str(wav)], check=True)  # fmt: skip
    size = 2038  # syncsafe: 2038 = 15 * 128 + 118
    header = b"ID3\x03\x00\x00" + bytes([0, 0, 2038 >> 7, 2038 & 0x7F]) + bytes(size)
    path = tmp_path / "x.mp3"
    path.write_bytes(header + wav.read_bytes())
    assert loudness.measure(path)[loudness.LOUDNESS]
    assert loudness._skip_tag(path) == ["-skip_initial_bytes", str(10 + size)]
    assert loudness._skip_tag(wav) == []


def test_unmeasurable_files_are_remembered_until_retried(Session, tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    root = tmp_path / "music"
    make_audio(root / "Air/Moon Safari/01 Quiet.mp3", album="Moon Safari", title="Quiet")
    with Session() as session:
        lib = Library(name="music", root=str(root))
        session.add(lib)
        session.commit()
        scan(session, lib)
        path = root / "Air/Moon Safari/01 Quiet.mp3"
        good = path.read_bytes()
        path.write_bytes(good[:100])  # unreadable now, but the catalog still has the file

        stats = loudness.measure_library(session, lib)
        assert (stats.measured, len(stats.errors)) == (0, 1)
        # Errors name the file by its path in the library
        assert "Air/Moon Safari/01 Quiet.mp3" in stats.errors[0][1]
        assert loudness.unmeasured(session, lib) == []
        assert loudness.measure_library(session, lib).errors == []  # not tried again

        path.write_bytes(good)
        assert len(loudness.unmeasured(session, lib, retry_failed=True)) == 1
        stats = loudness.measure_library(session, lib, retry_failed=True)
        assert (stats.measured, stats.errors) == (1, [])
        file = session.scalar(select(AudioFile))
        assert loudness.FAILED not in {v.field for v in file.values}


@pytest.mark.parametrize("name", ["mp3", "m4a", "aac"])
def test_embed_and_remove_cover(copy_audio, tmp_path, name):
    path = copy_audio(name, tmp_path / f"x.{name}")
    image = tmp_path / "cover.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=320x240",
                    "-frames:v", "1", str(image)], check=True)  # fmt: skip
    before = tags.read(path).fields
    assert covers.embedded(path) is None
    covers.embed(path, image)
    assert covers.embedded(path) == ("image/jpeg", image.read_bytes())
    after = tags.read(path).fields
    assert after.pop(tags.EMBEDDED_COVER) == "320x240"
    assert after == before  # other tags untouched
    covers.remove(path)
    assert covers.embedded(path) is None
