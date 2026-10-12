import shutil
from pathlib import Path

import pytest
from conftest import make_audio
from sqlalchemy import select

from audio_organizer import album_merge as am
from audio_organizer import changes as ch
from audio_organizer import operations as ops
from audio_organizer import writing as wr
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
    operation = am.decide(session, lib, [merge], "Merge")
    assert operation.kind == "merge" and len(operation.values) == 2
    assert am.decide(session, lib, [merge], "Merge") is None  # not decided twice
    assert (root / "Aguilera, Christina/Stripped/01 Stripped Intro.mp3").exists()  # untouched
    assert am.survey(session, lib, path_prefix="Christina").pending >= 1  # not proposed again

    found = wr.survey(session, lib, wr.Selection(only=["trash", "path"]))
    changeset, outcomes = wr.write(session, found.files, "Write")
    assert [c.kind for c in changeset.changes] == ["trash", "move"]
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    assert not (root / "Aguilera, Christina").exists()  # emptied and removed
    assert (root / "Christina Aguilera/Stripped/02 Can't Hold Us Down.mp3").exists()
    assert am.survey(session, lib, path_prefix="Christina").merges == []

    outcomes = ch.undo(session, changeset)
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    assert (root / "Aguilera, Christina/Stripped/01 Stripped Intro.mp3").exists()
    assert (root / "Aguilera, Christina/Stripped/02 Can't Hold Us Down.mp3").exists()
    assert wr.survey(session, lib, wr.Selection(only=["trash", "path"])).total == 2


def test_merge_decision_can_be_undone(library):
    session, lib, root = library
    merge = am.plan_folders(
        session, lib, "Aguilera, Christina/Stripped", "Christina Aguilera/Stripped"
    )
    operation = am.decide(session, lib, [merge], "Merge")
    assert ops.undo(session, operation).reverted == 2
    session.commit()
    assert wr.survey(session, lib, wr.Selection()).total == 0
    assert len(am.survey(session, lib, path_prefix="Christina").merges) == 1


def png(width: int, height: int) -> bytes:
    """Enough of a PNG for its size to be read."""
    size = width.to_bytes(4, "big") + height.to_bytes(4, "big")
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + size + bytes(5)


def test_merge_takes_the_other_files_along(library):
    """The source's other files move into the target; of same-named files, identical
    copies and system files are trashed, the bigger image takes the name and the other
    files get the folder's name added. Undo restores both folders."""
    session, lib, root = library
    source, target = root / "Aguilera, Christina/Stripped", root / "Christina Aguilera/Stripped"
    files = {
        source: {"booklet.pdf": b"booklet", "Scans/back.jpg": b"back", "folder.jpg": b"same",
                 "cover.png": png(500, 500), "front.png": png(100, 100), "album.cue": b"a",
                 ".DS_Store": b"source"},
        target: {"folder.jpg": b"same", "cover.png": png(250, 250), "front.png": png(300, 300),
                 "album.cue": b"b", ".DS_Store": b"target"},
    }  # fmt: skip
    for folder, contents in files.items():
        for name, data in contents.items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(data)
    merge = am.plan_folders(
        session, lib, "Aguilera, Christina/Stripped", "Christina Aguilera/Stripped"
    )
    am.decide(session, lib, [merge], "Merge")
    found = wr.survey(session, lib, wr.Selection(only=["trash", "path"]))
    assert all("loose file" in p.fields.get("trash", p.fields.get("path")) for p in found.files)
    changeset, outcomes = wr.write(session, found.files, "Write")
    s, t = "Aguilera, Christina/Stripped", "Christina Aguilera/Stripped"
    assert [ch.describe(c) for c in changeset.changes[2:]] == [
        f"trash {s}/.DS_Store (not music)",
        f"move {s}/Scans/back.jpg -> {t}/Scans/back.jpg",
        f"move {s}/album.cue -> {t}/album (Aguilera, Christina - Stripped).cue",
        f"move {s}/booklet.pdf -> {t}/booklet.pdf",
        f"rename {t}/cover.png -> {t}/cover (Christina Aguilera - Stripped).png",
        f"move {s}/cover.png -> {t}/cover.png",
        f"trash {s}/folder.jpg (same as {t}/folder.jpg)",
        f"move {s}/front.png -> {t}/front (Aguilera, Christina - Stripped).png",
    ]
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    assert not (root / "Aguilera, Christina").exists()
    assert (target / "cover.png").read_bytes() == png(500, 500)
    assert (target / "Scans/back.jpg").read_bytes() == b"back"

    outcomes = ch.undo(session, changeset)
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    for folder, contents in files.items():
        for name, data in contents.items():
            assert (folder / name).read_bytes() == data, folder / name
    assert sorted(p.name for p in target.iterdir()) == [
        ".DS_Store", "01 Stripped Intro.mp3", "03 Walk Away.mp3", "album.cue", "cover.png",
        "folder.jpg", "front.png",
    ]  # fmt: skip


def test_two_merges_into_one_folder_share_its_names(library):
    """Merges decided one after the other into the same folder: the first one's files are
    not on the disk yet when the second is planned, but their names are taken all the same."""
    session, lib, root = library
    target = "Christina Aguilera/Stripped"
    other = "C Aguilera/Stripped"
    (root / other).mkdir(parents=True)
    shutil.copyfile(root / target / "01 Stripped Intro.mp3", root / other / "01 Stripped Intro.mp3")
    first, second = "Aguilera, Christina/Stripped", other
    for folder, notes in ((first, b"one"), (second, b"two")):
        (root / folder).mkdir(parents=True, exist_ok=True)
        (root / folder / "desktop.ini").write_bytes(b"[.ShellClassInfo]")
        (root / folder / "notes.txt").write_bytes(notes)
    scan(session, lib)
    for source in (first, second):
        am.decide(session, lib, [am.plan_folders(session, lib, source, target)], "Merge")

    found = wr.survey(session, lib, wr.Selection(only=["trash", "path"]))
    changeset, outcomes = wr.write(session, found.files, "Write")
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    described = [ch.describe(c) for c in changeset.changes]
    assert f"move {first}/desktop.ini -> {target}/desktop.ini" in described
    assert f"trash {second}/desktop.ini (same as {target}/desktop.ini)" in described
    assert f"move {second}/notes.txt -> {target}/notes (C Aguilera - Stripped).txt" in described
    assert not (root / "C Aguilera").exists() and not (root / "Aguilera, Christina").exists()

    outcomes = ch.undo(session, changeset)
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    for folder, notes in ((first, b"one"), (second, b"two")):
        assert (root / folder / "notes.txt").read_bytes() == notes
        assert (root / folder / "desktop.ini").exists()
    assert not (root / target / "desktop.ini").exists()


def test_no_leftovers_move_while_tracks_stay(library):
    session, lib, root = library
    (root / "Texas/Greatest Hits/booklet.pdf").write_bytes(b"booklet")
    merge = am.plan_folders(session, lib, "Texas/Greatest Hits", "Texas/The Greatest Hits")
    assert merge.clashes and am.leftovers(merge, root) == []


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
