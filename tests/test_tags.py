from mutagen.id3 import COMM, ID3

from audio_organizer import tags
from audio_organizer.audiohash import audio_hash


def test_read_mp3(copy_audio, tmp_path):
    info = tags.read(copy_audio("mp3", tmp_path / "x.mp3"))
    assert info.format == "mp3"
    assert 0.9 < info.duration < 1.2
    assert info.fields == {
        "artist": "Texas", "album": "Greatest Hits", "title": "Halo", "tracknumber": "11",
        "tracktotal": "16", "date": "2000", "genre": "Pop",
    }  # fmt: skip


def test_read_m4a(copy_audio, tmp_path):
    info = tags.read(copy_audio("m4a", tmp_path / "x.m4a"))
    assert info.format == "mp4"
    assert info.fields["artist"] == "P!nk"
    assert info.fields["tracknumber"] == "1"


def test_musicmatch_comments(copy_audio, tmp_path):
    path = copy_audio("mp3", tmp_path / "x.mp3")
    id3 = ID3(path)
    id3.add(COMM(encoding=1, lang="eng", desc="MusicMatch_Mood", text=["Upbeat"]))
    id3.save(v2_version=3)
    assert tags.read(path).fields["mood"] == "Upbeat"
    tags.write(path, {"mood": "Calm"})
    assert tags.read(path).fields["mood"] == "Calm"
    assert len(ID3(path).getall("COMM")) == 1


def test_write_mp3_keeps_version_and_other_fields(copy_audio, tmp_path):
    path = copy_audio("mp3", tmp_path / "x.mp3")
    tags.write(path, {"album": "The Greatest Hits", "tracknumber": "2", "genre": None,
                      "albumartist": "Texas"})  # fmt: skip
    fields = tags.read(path).fields
    assert fields["album"] == "The Greatest Hits"
    assert fields["tracknumber"] == "2"
    assert fields["tracktotal"] == "16"
    assert fields["albumartist"] == "Texas"
    assert "genre" not in fields
    assert fields["title"] == "Halo"
    assert ID3(path).version == (2, 3, 0)


def test_write_m4a(copy_audio, tmp_path):
    path = copy_audio("m4a", tmp_path / "x.m4a")
    tags.write(path, {"title": "So What (Live)", "discnumber": "1", "disctotal": "2",
                      "compilation": "1"})  # fmt: skip
    fields = tags.read(path).fields
    assert fields["title"] == "So What (Live)"
    assert (fields["discnumber"], fields["disctotal"]) == ("1", "2")
    assert fields["compilation"] == "1"


def test_unknown_field_rejected(copy_audio, tmp_path):
    path = copy_audio("mp3", tmp_path / "x.mp3")
    try:
        tags.write(path, {"copy_suffix": "1"})
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_audio_hash_ignores_tags(copy_audio, tmp_path):
    for name in ("mp3", "m4a"):
        path = copy_audio(name, tmp_path / f"x.{name}")
        before = audio_hash(path)
        tags.write(path, {"title": "A much longer title " * 20, "album": "Something else"})
        assert audio_hash(path) == before, name


def test_audio_hash_ignores_id3v1(copy_audio, tmp_path):
    path = copy_audio("mp3", tmp_path / "x.mp3")
    before = audio_hash(path)
    with open(path, "ab") as f:
        f.write(b"TAG" + b"\0" * 125)
    assert audio_hash(path) == before


def test_audio_hash_differs_for_different_audio(copy_audio, tmp_path):
    a = copy_audio("mp3", tmp_path / "a.mp3")
    b = copy_audio("mp3b", tmp_path / "b.mp3")
    assert audio_hash(a) != audio_hash(b)
