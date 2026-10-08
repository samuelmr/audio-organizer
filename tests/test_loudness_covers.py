import re

import pytest

from audio_organizer import covers, loudness, tags


def test_measure(copy_audio, tmp_path):
    values = loudness.measure(copy_audio("mp3", tmp_path / "x.mp3"))
    assert re.fullmatch(r"[+-]\d+\.\d\d dB", values["replaygain_track_gain"])
    assert 0 < float(values["replaygain_track_peak"]) <= 1.5


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
    assert tags.read(path).fields == before  # other tags untouched
    covers.remove(path)
    assert covers.embedded(path) is None
