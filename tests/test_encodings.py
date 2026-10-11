import pytest
from conftest import make_audio
from sqlalchemy import select

from audio_organizer import changes as ch
from audio_organizer import encodings as rec
from audio_organizer import tags
from audio_organizer import writing as wr
from audio_organizer.models import AudioFile, Library
from audio_organizer.scanner import scan


@pytest.fixture
def library(Session, tmp_path):
    """Cases from a real library: one recording in several encodings, and look-alikes."""
    root = tmp_path / "music"
    # An album ripped twice: MP3 and AAC
    make_audio(root / "Yö/Live/01 Intro.mp3", freq=300, title="Intro", track="1")
    make_audio(root / "Yö/Live/01 Intro.m4a", freq=301, bitrate="128k", title="Intro", track="1")
    # An iTunes copy at a higher bitrate than the original
    make_audio(root / "Yello/Excess/02 Lost Again.mp3", freq=400, title="Lost Again", track="2")
    make_audio(root / "Yello/Excess/02 Lost Again 1.mp3", freq=401, bitrate="128k",
               title="Lost Again", track="2")  # fmt: skip
    # A loose copy, better than the album's
    make_audio(root / "Linkin Park/Meteora/13 Numb.mp3", freq=500, artist="Linkin Park",
               album="Meteora", title="Numb", track="13/13", date="2003")  # fmt: skip
    make_audio(root / "Linkin Park/Linkin Park_Numb.mp3", freq=501, bitrate="128k",
               artist="Linkin Park", title="Numb")  # fmt: skip
    # A loose copy, worse than the album's
    make_audio(root / "Texas/Greatest Hits/11 Halo.m4a", freq=600, bitrate="128k",
               artist="Texas", album="Greatest Hits", title="Halo", track="11")  # fmt: skip
    make_audio(root / "Texas/Texas_Halo.mp3", freq=601, artist="Texas", title="Halo",
               genre="Pop")  # fmt: skip
    # Another version: same title and number, durations differ
    make_audio(root / "Rammstein/Mutter/11 Nebel.mp3", freq=700, title="Nebel", track="11")
    make_audio(root / "Rammstein/Mutter/11 Nebel 1.mp3", freq=701, seconds=4, title="Nebel",
               track="11")  # fmt: skip
    # Two tracks of an album with the same title
    make_audio(root / "Preisner/Bleu/13 Funeral Music.mp3", freq=800, title="Funeral Music",
               track="13")  # fmt: skip
    make_audio(root / "Preisner/Bleu/14 Funeral Music.mp3", freq=801, title="Funeral Music",
               track="14")  # fmt: skip
    session = Session()
    lib = Library(name="music", root=str(root))
    session.add(lib)
    session.commit()
    scan(session, lib)
    yield session, lib, root
    session.close()


def _by_keep(found: rec.Survey) -> dict[str, rec.Proposal]:
    return {p.keep.file.path: p for p in found.proposals}


def test_survey(library):
    session, lib, root = library
    found = rec.survey(session, lib)
    assert found.groups == 5
    assert [[f.path for f in v] for v in found.versions] == [
        ["Rammstein/Mutter/11 Nebel 1.mp3", "Rammstein/Mutter/11 Nebel.mp3"]
    ]
    proposals = _by_keep(found)
    assert sorted(proposals) == [
        "Linkin Park/Linkin Park_Numb.mp3",
        "Texas/Greatest Hits/11 Halo.m4a",
        "Yello/Excess/02 Lost Again 1.mp3",
        "Yö/Live/01 Intro.m4a",
    ]

    ripped_twice = proposals["Yö/Live/01 Intro.m4a"]
    assert [c.file.path for c in ripped_twice.trash] == ["Yö/Live/01 Intro.mp3"]
    assert (ripped_twice.move_to, ripped_twice.replaces) == (None, False)

    itunes_copy = proposals["Yello/Excess/02 Lost Again 1.mp3"]
    assert [c.file.path for c in itunes_copy.trash] == ["Yello/Excess/02 Lost Again.mp3"]
    assert itunes_copy.move_to == "Yello/Excess/02 Lost Again.mp3"

    better_loose = proposals["Linkin Park/Linkin Park_Numb.mp3"]
    assert [c.file.path for c in better_loose.trash] == ["Linkin Park/Meteora/13 Numb.mp3"]
    assert better_loose.move_to == "Linkin Park/Meteora/13 Numb.mp3"
    assert better_loose.replaces
    assert better_loose.fill == {
        "album": "Meteora", "tracknumber": "13", "tracktotal": "13", "date": "2003",
    }  # fmt: skip

    worse_loose = proposals["Texas/Greatest Hits/11 Halo.m4a"]
    assert [c.file.path for c in worse_loose.trash] == ["Texas/Texas_Halo.mp3"]
    assert worse_loose.fill == {"genre": "Pop"}
    assert worse_loose.move_to is None

    assert [p.keep.file.path for p in found.batchable] == [
        "Texas/Greatest Hits/11 Halo.m4a",
        "Yello/Excess/02 Lost Again 1.mp3",
        "Yö/Live/01 Intro.m4a",
    ]
    assert len(rec.batch(found.proposals, 10, replace=True)) == 4
    assert rec.survey(session, lib, path_prefix="Texas").groups == 1


def test_choose_another_copy(library):
    session, lib, root = library
    found = rec.survey(session, lib)
    loose = _by_keep(found)["Linkin Park/Linkin Park_Numb.mp3"]
    album = loose.trash[0]
    [chosen] = rec.choose([loose], {str(loose.keep.file.id): album.file.id})
    assert chosen.keep is album
    assert [c.file.path for c in chosen.trash] == ["Linkin Park/Linkin Park_Numb.mp3"]
    assert (chosen.move_to, chosen.replaces) == (None, False)


def test_decide_write_and_undo(library):
    session, lib, root = library
    found = rec.survey(session, lib)
    operation = rec.decide(session, lib, found.proposals, "Keep the best encodings")
    assert operation.kind == "encodings"
    assert rec.decide(session, lib, found.proposals, "Again") is None  # not decided twice
    assert rec.survey(session, lib).pending == 4
    assert (root / "Linkin Park/Linkin Park_Numb.mp3").exists()  # the files are untouched

    pending = wr.survey(session, lib, wr.Selection())
    changeset, outcomes = wr.write(session, pending.files, "Write")
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    kinds = [c.kind for c in changeset.changes]
    assert "move" in kinds and kinds.index("move") > max(
        i for i, kind in enumerate(kinds) if kind == "trash"
    )  # trashed before a file takes the place of its copy

    numb = root / "Linkin Park/Meteora/13 Numb.mp3"
    assert not (root / "Linkin Park/Linkin Park_Numb.mp3").exists()
    assert tags.read(numb).bitrate > 100_000  # the loose copy, in the album's place
    assert tags.read(numb).fields["album"] == "Meteora"
    lost_again = root / "Yello/Excess/02 Lost Again.mp3"
    assert tags.read(lost_again).bitrate > 100_000
    assert not (root / "Yello/Excess/02 Lost Again 1.mp3").exists()
    assert not (root / "Yö/Live/01 Intro.mp3").exists()
    stats = scan(session, lib)
    assert stats.new == 0
    assert rec.survey(session, lib).groups == 1  # Nebel's versions

    outcomes = ch.undo(session, changeset)
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    assert (root / "Linkin Park/Linkin Park_Numb.mp3").exists()
    assert tags.read(numb).bitrate < 100_000
    assert tags.read(root / "Yello/Excess/02 Lost Again 1.mp3").bitrate > 100_000
    assert scan(session, lib).unchanged == 12
    session.expire_all()
    paths = set(session.scalars(select(AudioFile.path).where(AudioFile.missing.is_(False))))
    assert "Yello/Excess/02 Lost Again 1.mp3" in paths
