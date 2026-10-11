import shutil

import pytest
from conftest import make_audio

from audio_organizer import changes as ch
from audio_organizer import neighbours as nb
from audio_organizer import tags
from audio_organizer import writing as wr
from audio_organizer.models import Library
from audio_organizer.scanner import scan

ALBUM = {"artist": "Klaus Doldinger", "album_artist": "Klaus Doldinger", "album": "Das Boot",
         "date": "1981", "genre": "Soundtrack"}  # fmt: skip


@pytest.fixture
def library(Session, tmp_path):
    root = tmp_path / "music"
    album, loose = root / "Klaus Doldinger/Das Boot", root / "Doldinger, Klaus"
    make_audio(album / "01 Titel.mp3", freq=300, title="Titel", track="1/4", **ALBUM)
    make_audio(album / "02 Abschied.mp3", freq=310, title="Abschied", track="2/4", **ALBUM)
    # Fits: the album tag names the folder (in other letter case), number 3 is free
    make_audio(loose / "Klaus Doldinger_Appell.mp3", freq=320, artist="Klaus Doldinger",
               album="das boot", title="Appell", track="3", genre="Jazz")  # fmt: skip
    # Number taken
    make_audio(loose / "Klaus Doldinger_Heimkehr.mp3", freq=330, artist="Klaus Doldinger",
               album="Das Boot", title="Heimkehr", track="2")  # fmt: skip
    # Two loose tracks claiming number 4
    for freq, title in ((340, "U 96"), (350, "U-96")):
        make_audio(loose / f"Klaus Doldinger_{title}.mp3", freq=freq, artist="Klaus Doldinger",
                   album="Das Boot", title=title, track="4")  # fmt: skip
    # No album tag; an album of which there is no folder; identical to an album track
    make_audio(loose / "Klaus Doldinger_Tuuli.mp3", freq=360, artist="Klaus Doldinger",
               title="Tuuli", track="5")  # fmt: skip
    make_audio(loose / "Klaus Doldinger_Blues.mp3", freq=370, artist="Klaus Doldinger",
               album="Passport", title="Blues", track="1")  # fmt: skip
    shutil.copyfile(album / "01 Titel.mp3", loose / "Klaus Doldinger_Titel.mp3")
    session = Session()
    lib = Library(name="music", root=str(root))
    session.add(lib)
    session.commit()
    scan(session, lib)
    yield session, lib, root
    session.close()


def test_survey(library):
    session, lib, root = library
    found = nb.survey(session, lib)
    assert (found.loose, found.duplicates, found.no_album_tag, found.no_folder) == (7, 1, 1, 1)
    assert (found.taken, found.conflicts) == (1, 2)
    [album] = found.albums
    assert (album.folder, album.tracks) == ("Klaus Doldinger/Das Boot", 2)
    [move] = album.moves
    assert move.copy.file.path == "Doldinger, Klaus/Klaus Doldinger_Appell.mp3"
    assert move.to == "Klaus Doldinger/Das Boot/03 Appell.mp3"
    assert move.fill == {
        "album": "Das Boot", "albumartist": "Klaus Doldinger", "tracktotal": "4", "date": "1981",
    }  # fmt: skip
    assert nb.survey(session, lib, path_prefix="Klaus Doldinger/").loose == 0


def test_decide_write_and_undo(library):
    session, lib, root = library
    found = nb.survey(session, lib)
    operation = nb.decide(session, lib, nb.batch(found.albums, 10), "Put loose tracks in albums")
    assert operation.kind == "neighbours"
    assert nb.decide(session, lib, nb.batch(found.albums, 10), "Again") is None
    assert nb.survey(session, lib).pending == 1
    assert (root / "Doldinger, Klaus/Klaus Doldinger_Appell.mp3").exists()  # untouched

    changeset, outcomes = wr.write(session, wr.survey(session, lib, wr.Selection()).files, "Write")
    assert [c.kind for c in changeset.changes] == ["retag", "move"]
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    moved = root / "Klaus Doldinger/Das Boot/03 Appell.mp3"
    fields = tags.read(moved).fields
    assert (fields["album"], fields["tracktotal"], fields["genre"]) == ("Das Boot", "4", "Jazz")
    assert scan(session, lib).new == 0
    assert nb.survey(session, lib).albums == []

    outcomes = ch.undo(session, changeset)
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    assert not moved.exists()
    assert wr.survey(session, lib, wr.Selection(only=["path"])).total == 1  # pending again
    assert tags.read(root / "Doldinger, Klaus/Klaus Doldinger_Appell.mp3").fields["album"] == (
        "das boot"
    )


def test_name_follows_the_neighbours():
    copy = type("C", (), {})
    track = type("T", (), {"title": "Appell: Teil 1", "tracknumber": 3, "discnumber": 2})

    def named(*names):
        members = []
        for name in names:
            c = copy()
            c.file = type("F", (), {"path": f"A/B/{name}"})
            members.append(c)
        return nb._name(members, track)

    assert named("1-01 Titel.mp3", "1-02 Abschied.mp3") == "2-03 Appell_ Teil 1"
    assert named("01 Titel.mp3", "02 - Abschied.mp3") == "03 Appell_ Teil 1"
    assert named("Titel.mp3", "Abschied.mp3") == "Appell_ Teil 1"
