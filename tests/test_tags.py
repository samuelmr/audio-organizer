import mutagen
import pytest
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


def test_adts_aac_with_id3_tag(copy_audio, tmp_path):
    path = copy_audio("aac", tmp_path / "x.AAC")
    with pytest.raises(mutagen.MutagenError):
        mutagen.File(path)  # the problem: detected as MP3, which fails
    info = tags.read(path)
    assert info.format == "aac"
    assert info.duration
    assert info.fields == {"artist": "Underworld", "album": "Trainspotting", "title": "Born Slippy"}
    before = audio_hash(path)
    tags.write(path, {"title": "Born Slippy .NUXX", "tracknumber": "8"})
    assert tags.read(path).fields["title"] == "Born Slippy .NUXX"
    assert tags.read(path).fields["tracknumber"] == "8"
    assert audio_hash(path) == before


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


@pytest.mark.parametrize(
    ("value", "number"),
    [("2", 2), ("02", 2), ("02.", 2), ("13-", 13), (" 7 ", 7), ("Satakuntalainen Osakunta", None),
     ("A1", None), ("", None), (None, None)],
)  # fmt: skip
def test_parse_number(value, number):
    assert tags.parse_number(value) == number


@pytest.mark.parametrize(
    ("value", "year"),
    [("1996", 1996), ("1996-05-01", 1996), ("2017-03-03T08:00:00Z", 2017), ("96", None),
     ("unknown", None), (None, None)],
)  # fmt: skip
def test_parse_year(value, year):
    assert tags.parse_year(value) == year


def test_odd_track_numbers_are_normalized(copy_audio, tmp_path):
    path = copy_audio("mp3", tmp_path / "x.mp3")
    tags.write(path, {"tracknumber": "02.", "tracktotal": None})
    assert tags.read(path).fields["tracknumber"] == "2"
    tags.write(path, {"tracknumber": "Satakuntalainen Osakunta"})
    assert tags.read(path).fields["tracknumber"] == "Satakuntalainen Osakunta"  # kept as it is


NEW_FIELDS = {
    "originaldate": "1994",
    "albumartistsort": "Morissette, Alanis",
    "artistsort": "Morissette, Alanis",
    "composer": "Alanis Morissette",
    "conductor": "Leif Segerstam",
    "grouping": "Rock'n'roll",
    "subtitle": "Live",
    "language": "fin",
    "mood": "Upbeat",
    "rating": "4",
    "discid": "870a280b",
    "replaygain_track_gain": "-6.48 dB",
    "replaygain_track_peak": "0.988525",
    "replaygain_album_gain": "-7.10 dB",
    "replaygain_album_peak": "1.000000",
}


@pytest.mark.parametrize("version", [3, 4])
def test_new_fields_mp3(copy_audio, tmp_path, version):
    path = copy_audio("mp3", tmp_path / "x.mp3")
    tags.write(path, NEW_FIELDS, id3_version=version)
    fields = tags.read(path).fields
    assert {k: fields.get(k) for k in NEW_FIELDS} == NEW_FIELDS
    id3 = ID3(path)
    assert id3.version[:2] == (2, version)
    assert id3.getall("POPM")[0].rating == 196
    assert id3.getall("TSOP")  # kept in ID3v2.3 too, as iTunes does


def test_id3v23_stays_v23_on_disk(copy_audio, tmp_path):
    """Writing an ID3v2.3 file must use v2.3 frames: the year in TYER, not TDRC."""
    path = copy_audio("mp3", tmp_path / "x.mp3")
    tags.write(path, {"title": "Halo (Live)"})
    raw = path.read_bytes()[:4096]
    assert raw[3] == 3  # ID3v2.3 header
    assert b"TYER" in raw and b"TDRC" not in raw
    assert tags.read(path).fields["date"] == "2000"


def test_upgrade_to_id3v24(copy_audio, tmp_path):
    path = copy_audio("mp3", tmp_path / "x.mp3")
    assert tags.id3_version(path) == 3
    tags.write(path, {}, id3_version=4)
    assert tags.id3_version(path) == 4
    assert b"TDRC" in path.read_bytes()[:4096]
    assert tags.read(path).fields["date"] == "2000"


def test_new_fields_m4a(copy_audio, tmp_path):
    path = copy_audio("m4a", tmp_path / "x.m4a")
    tags.write(path, NEW_FIELDS)
    fields = tags.read(path).fields
    assert {k: fields.get(k) for k in NEW_FIELDS} == NEW_FIELDS


def test_mood_goes_to_the_standard_tag(copy_audio, tmp_path):
    path = copy_audio("mp3", tmp_path / "x.mp3")
    tags.write(path, {"mood": "Mellow"})
    id3 = ID3(path)
    assert [f.text for f in id3.getall("TXXX") if f.desc == "MOOD"] == [["Mellow"]]
    assert not id3.getall("COMM")  # no MusicMatch comment where there was none

    id3.add(COMM(encoding=1, lang="eng", desc="MusicMatch_Mood", text=["Upbeat"]))
    id3.save(v2_version=3)
    assert tags.read(path).fields["mood"] == "Mellow"  # the standard tag wins
    tags.write(path, {"mood": "Calm"})
    id3 = ID3(path)
    assert [f.text for f in id3.getall("COMM") if f.desc == "MusicMatch_Mood"] == [["Calm"]]


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
