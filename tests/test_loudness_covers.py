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


@pytest.mark.parametrize("name", ["mp3", "m4a", "aac"])
def test_embed_and_remove_cover(copy_audio, tmp_path, name):
    path = copy_audio(name, tmp_path / f"x.{name}")
    image = tmp_path / "cover.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0 a tiny jpeg")
    before = tags.read(path).fields
    assert covers.embedded(path) is None
    covers.embed(path, image)
    assert covers.embedded(path) == ("image/jpeg", image.read_bytes())
    after = tags.read(path).fields
    assert after.pop(tags.EMBEDDED_COVER) == "1"
    assert after == before  # other tags untouched
    covers.remove(path)
    assert covers.embedded(path) is None
