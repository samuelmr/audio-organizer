import shutil
from pathlib import Path

import pytest
from conftest import make_audio
from sqlalchemy import select

from audio_organizer import album_merge as am
from audio_organizer import changes as ch
from audio_organizer.models import AudioFile, Library
from audio_organizer.scanner import scan


@pytest.fixture
def library(Session, tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    root = tmp_path / "music"
    tones = iter(range(300, 2000, 70))  # a different recording for each new track

    def track(path: str, same_as: Path | None = None, **fields: str) -> Path:
        target = root / path
        if same_as:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(same_as, target)
        else:
            make_audio(target, freq=next(tones), seconds=0.3, **fields)
        return target

    # A fragment of an album in a second folder: one shared track, one to move
    intro = track("Christina Aguilera/Stripped/01 Stripped Intro.mp3")
    track("Christina Aguilera/Stripped/03 Walk Away.mp3")
    track("Aguilera, Christina/Stripped/01 Stripped Intro.mp3", same_as=intro)
    track("Aguilera, Christina/Stripped/02 Can't Hold Us Down.mp3", track="2")
    # Same name, but a different recording with a number the target already uses
    halo = track("Texas/The Greatest Hits/11 Halo.mp3")
    track("Texas/The Greatest Hits/03 Say What You Want.mp3")
    track("Texas/Greatest Hits/11 Halo.mp3", same_as=halo)
    track("Texas/Greatest Hits/03 Say What You Want.mp3", track="3")
    session = Session()
    lib = Library(name="music", root=str(root))
    session.add(lib)
    session.commit()
    scan(session, lib)
    yield session, lib, root
    session.close()


def test_survey(library):
    session, lib, root = library
    found = am.survey(session, lib)
    merges = {m.source.path: m for m in found.merges}
    stripped = merges["Aguilera, Christina/Stripped"]
    assert stripped.target.path == "Christina Aguilera/Stripped"  # more tracks
    assert stripped.batchable
    assert [t.name for t, _ in stripped.trash] == ["01 Stripped Intro.mp3"]
    assert stripped.move[0][1] == "Christina Aguilera/Stripped/02 Can't Hold Us Down.mp3"

    texas = next(m for m in found.merges if m.source.path.startswith("Texas"))
    assert [t.name for t in texas.clashes] == ["03 Say What You Want.mp3"]
    assert not texas.batchable
    assert [m.source.path for m in am.batch(found.merges, 10)] == ["Aguilera, Christina/Stripped"]


def test_merge_apply_and_undo(library):
    session, lib, root = library
    merge = am.plan_folders(
        session, lib, "Aguilera, Christina/Stripped", "Christina Aguilera/Stripped"
    )
    changeset = am.propose(session, [merge], "Merge")
    assert [c.kind for c in changeset.changes] == ["trash", "move"]
    ch.set_status(changeset.changes, "approved")
    outcomes = ch.apply(session, changeset)
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    assert not (root / "Aguilera, Christina").exists()  # emptied and removed
    assert (root / "Christina Aguilera/Stripped/02 Can't Hold Us Down.mp3").exists()
    assert am.survey(session, lib, path_prefix="Christina").merges == []

    outcomes = ch.undo(session, changeset)
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    assert (root / "Aguilera, Christina/Stripped/01 Stripped Intro.mp3").exists()
    assert (root / "Aguilera, Christina/Stripped/02 Can't Hold Us Down.mp3").exists()


def test_plan_folders_rejects_unknown_folders(library):
    session, lib, root = library
    with pytest.raises(ValueError, match="not an album folder"):
        am.plan_folders(session, lib, "Nobody/Nothing", "Christina Aguilera/Stripped")


def test_compilation_and_album_are_not_merged(Session, copy_audio, tmp_path):
    root = tmp_path / "music"
    copy_audio("mp3", root / "Various Artists/Hits 2000/04 Halo.mp3")
    copy_audio("mp3", root / "Texas/Hits 2000/11 Halo.mp3")
    with Session() as session:
        lib = Library(name="music", root=str(root))
        session.add(lib)
        session.commit()
        scan(session, lib)
        assert am.survey(session, lib).merges == []
        assert session.scalar(select(AudioFile).limit(1)) is not None
