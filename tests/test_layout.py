import shutil
from pathlib import Path

import pytest
from conftest import make_audio
from sqlalchemy import select

from audio_organizer import changes as ch
from audio_organizer import layout as lo
from audio_organizer import operations as ops
from audio_organizer import writing as wr
from audio_organizer.models import AudioFile, EffectiveMetadata, Library
from audio_organizer.scanner import scan


def meta(**fields) -> EffectiveMetadata:
    values = {name: None for name in ("artist", "albumartist", "album", "title", "genre",
              "compilation", "tracknumber", "discnumber", "disctotal", "year")}  # fmt: skip
    values.update(fields)
    return EffectiveMetadata(**values)


def render(template, several_discs=False, path="x/y.MP3", **fields):
    m = meta(**fields)
    album_artist = lo._album_artists([(type("F", (), {"id": 1, "path": path})(), m)])[1]
    return lo.render(template, lo.validate(template), m, path, album_artist,
                     several_discs=several_discs)  # fmt: skip


def test_render():
    t = lo.DEFAULT_LAYOUT
    assert render(t, artist="AC/DC", album="Back in Black", title="Hells Bells", tracknumber=1) == (
        "AC_DC/Back in Black/01 Hells Bells.mp3"
    )
    assert render(t, several_discs=True, artist="Texas", album="Hits", title="Halo",
                  tracknumber=11, discnumber=2) == "Texas/Hits/2-11 Halo.mp3"  # fmt: skip
    assert render(t, artist="Garbage", compilation="1", album="Grammy 1997", title="Stupid Girl",
                  tracknumber=6) == "Various Artists/Grammy 1997/06 Stupid Girl.mp3"  # fmt: skip
    assert render(t, artist="Texas", album="Hits", title="Halo") is None  # no track number
    assert render("{artist}/{album}/{title}", artist=" Kwan ", album="...", title="Rain") is None
    rendered = render("{artist}/{year} - {album}/{title}", artist="Air", album="Moon Safari",
                      title="Sexy Boy ", year=1998)  # fmt: skip
    assert rendered == "Air/1998 - Moon Safari/Sexy Boy.mp3"


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("{artist}/{colour}/{title}", "Unknown fields"),
        ("{artist} - {album}", "needs {title}"),
        ("{title}", "at least one folder"),
        ("/{artist}/{title}", "at least one folder"),
        ("{artist}/{title", "Cannot read"),
    ],
)
def test_validate(template, message):
    with pytest.raises(lo.LayoutError, match=message):
        lo.validate(template)


@pytest.fixture
def library(Session, tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    root = tmp_path / "music"
    tones = iter(range(300, 2000, 70))

    def track(path: str, **fields: str) -> Path:
        return make_audio(root / path, freq=next(tones), seconds=0.3, **fields)

    album = dict(artist="Rebecca Pan", album="In the Mood for Love")
    track("Pan, Rebecca/Rebecca Pan_Bengawan Solo.mp3", title="Bengawan Solo", track="10", **album)
    track("Pan, Rebecca/Rebecca Pan_Quizas.mp3", title="Quizas", track="11", **album)
    (root / "Pan, Rebecca/Folder.jpg").write_bytes(b"\xff\xd8 cover")
    track("Rebecca Pan/In the Mood for Love/01 Yumeji's Theme.mp3", title="Yumeji's Theme",
          track="1", **album)  # fmt: skip
    # Two copies wanting the same path
    track("Air/Air_Sexy Boy.mp3", artist="Air", album="Moon Safari", title="Sexy Boy", track="2")
    track("Air/Moon Safari/02 Sexy Boy.mp3", artist="Air", album="Moon Safari",
          title="Sexy Boy", track="2")  # fmt: skip
    track("Air/Kelly Watch the Stars.mp3", artist="Air", title="Kelly Watch the Stars")  # no album
    session = Session()
    lib = Library(name="music", root=str(root))
    session.add(lib)
    session.commit()
    scan(session, lib)
    yield session, lib, root
    session.close()


def test_plan(library):
    session, lib, root = library
    plan = lo.plan(session, lib, lo.DEFAULT_LAYOUT)
    assert [a.folder for a in plan.albums] == ["Rebecca Pan/In the Mood for Love"]
    [album] = plan.albums
    assert sorted(m.target for m in album.moves) == [
        "Rebecca Pan/In the Mood for Love/10 Bengawan Solo.mp3",
        "Rebecca Pan/In the Mood for Love/11 Quizas.mp3",
    ]
    assert album.sources == ["Pan, Rebecca"]
    assert (plan.in_place, plan.incomplete) == (2, 1)  # Yumeji's Theme, Air's Sexy Boy
    assert [(f.path, why) for f, _, why in plan.conflicts] == [
        ("Air/Air_Sexy Boy.mp3", "taken by Air/Moon Safari/02 Sexy Boy.mp3"),
    ]


def test_propose_apply_and_undo_with_cover(library):
    session, lib, root = library
    plan = lo.plan(session, lib, lo.DEFAULT_LAYOUT)
    changeset = lo.propose(session, lib, plan.albums, "Move")
    assert [c.kind for c in changeset.changes] == ["move", "move"]
    assert changeset.changes[-1].payload["extras"] == [
        ["Pan, Rebecca/Folder.jpg", "Rebecca Pan/In the Mood for Love/Folder.jpg"]
    ]
    ch.set_status(changeset.changes, "approved")
    outcomes = ch.apply(session, changeset)
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    assert (root / "Rebecca Pan/In the Mood for Love/Folder.jpg").exists()
    assert not (root / "Pan, Rebecca").exists()  # emptied and removed
    assert lo.plan(session, lib, lo.DEFAULT_LAYOUT).albums == []

    outcomes = ch.undo(session, changeset)
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    assert (root / "Pan, Rebecca/Folder.jpg").exists()
    assert (root / "Pan, Rebecca/Rebecca Pan_Quizas.mp3").exists()
    assert not (root / "Rebecca Pan/In the Mood for Love/Folder.jpg").exists()
    assert scan(session, lib).unchanged == 6


def test_decide_and_write_with_pending_tags(library):
    """A move is a decision like any other: it waits on the Pending page, is written once
    with the file's tag edits, and undoing the write leaves it pending again."""
    session, lib, root = library
    plan = lo.plan(session, lib, lo.DEFAULT_LAYOUT)
    operation = lo.decide(session, lib, plan.albums, "Move")
    assert operation.kind == "layout" and len(operation.values) == 2
    assert lo.decide(session, lib, plan.albums, "Move") is None  # not decided twice
    assert (root / "Pan, Rebecca/Rebecca Pan_Quizas.mp3").exists()  # the files are untouched
    again = lo.plan(session, lib, lo.DEFAULT_LAYOUT)
    assert again.albums == [] and again.pending == 2

    quizas = session.scalar(select(AudioFile).where(AudioFile.path.like("%Quizas.mp3")))
    wr.store_edit(session, [quizas], {"genre": "Latin"}, "Genre")
    found = wr.survey(session, lib, wr.Selection())
    assert found.by_field["path"] == 2 and found.by_field["genre"] == 1
    [pending] = [p for p in found.files if p.file.id == quizas.id]
    assert pending.fields["path"].startswith("Rebecca Pan/In the Mood for Love/11 Quizas.mp3")

    changeset, outcomes = wr.write(session, found.files, "Write")
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    assert sorted(c.kind for c in changeset.changes) == ["move", "move", "retag"]
    moved = root / "Rebecca Pan/In the Mood for Love"
    assert (moved / "11 Quizas.mp3").exists() and (moved / "Folder.jpg").exists()
    assert not (root / "Pan, Rebecca").exists()
    assert wr.survey(session, lib, wr.Selection()).total == 0

    outcomes = ch.undo(session, changeset)
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    assert (root / "Pan, Rebecca/Folder.jpg").exists()
    assert wr.survey(session, lib, wr.Selection(only=["path"])).total == 2


def test_undoing_the_decision_drops_the_move(library):
    session, lib, root = library
    plan = lo.plan(session, lib, lo.DEFAULT_LAYOUT)
    operation = lo.decide(session, lib, plan.albums, "Move")
    undone = ops.undo(session, operation)
    session.commit()
    assert undone.reverted == 2
    assert wr.survey(session, lib, wr.Selection()).total == 0
    assert len(lo.plan(session, lib, lo.DEFAULT_LAYOUT).albums) == 1  # to decide again


def test_other_files_go_with_the_last_track(library):
    """Written in parts, the folder's cover goes only when no track stays in the folder."""
    session, lib, root = library
    lo.decide(session, lib, lo.plan(session, lib, lo.DEFAULT_LAYOUT).albums, "Move")
    first, last = wr.survey(session, lib, wr.Selection()).files
    _, outcomes = wr.write(session, [last], "Write")  # the other track stays
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    assert (root / "Pan, Rebecca/Folder.jpg").exists()
    _, outcomes = wr.write(session, [first], "Write")
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    assert (root / "Rebecca Pan/In the Mood for Love/Folder.jpg").exists()
    assert not (root / "Pan, Rebecca").exists()


def test_decided_target_is_taken(library):
    """Another file does not get a path that a decided move is about to take."""
    session, lib, root = library
    plan = lo.plan(session, lib, lo.DEFAULT_LAYOUT)
    lo.decide(session, lib, plan.albums, "Move")
    taken = "Rebecca Pan/In the Mood for Love/11 Quizas.mp3"
    twin = make_audio(root / "Rebecca Pan/Quizas.mp3", freq=1900, seconds=0.3,
                      artist="Rebecca Pan", album="In the Mood for Love", title="Quizas",
                      track="11")  # fmt: skip
    scan(session, lib)
    again = lo.plan(session, lib, lo.DEFAULT_LAYOUT)
    assert [(f.path, why) for f, target, why in again.conflicts if target == taken] == [
        ("Rebecca Pan/Quizas.mp3", "taken by Pan, Rebecca/Rebecca Pan_Quizas.mp3")
    ]
    assert twin.exists()


def test_extras_only_from_folders_the_album_leaves(tmp_path):
    """Other files stay where tracks stay, in the library's top folder, and in subfolders
    holding audio, hidden or system ones, or the album's new folder."""
    root = tmp_path / "music"
    for name in (
        "Loose/a.mp3",
        "Loose/b.mp3",
        "Loose/x.cue",
        "c.mp3",
        "notes.txt",
        "Artist/d.mp3",
        "Artist/artist.jpg",
        "Artist/.DS_Store",
        "Artist/CD2/e.mp3",
        "Artist/@eaDir/thumb.jpg",
        "Artist/Album/cover.jpg",
        "Artist/Photos/1.jpg",
    ):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(b"x")  # fmt: skip
    library = Library(name="music", root=str(root))

    def extras(*paths: str, folder: str = "Artist/Album") -> list[tuple[str, str]]:
        moves = [lo.Move(AudioFile(path=p), f"{folder}/{p.rsplit('/', 1)[-1]}") for p in paths]
        found = lo.with_extras(lo.AlbumMove(folder, moves), library).extras
        return [pair for pairs in found.values() for pair in pairs]

    assert extras("Loose/a.mp3") == []  # b.mp3 stays
    assert extras("Loose/a.mp3", "Loose/b.mp3") == [("Loose/x.cue", "Artist/Album/x.cue")]
    assert extras("c.mp3") == []
    assert extras("Artist/d.mp3") == [
        ("Artist/.DS_Store", "Artist/Album/.DS_Store"),
        ("Artist/Photos/1.jpg", "Artist/Album/Photos/1.jpg"),
        ("Artist/artist.jpg", "Artist/Album/artist.jpg"),
    ]


def test_album_without_album_artist_stays_together(Session, tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    root = tmp_path / "music"
    album = dict(album="The Nightmare Before Christmas")
    for i, artist in enumerate(["Danny Elfman", "Danny Elfman", "Catherine O'Hara"], start=1):
        make_audio(root / f"Elfman, Danny/{artist}_Song {i}.mp3", freq=400 + i * 50,
                   seconds=0.3, artist=artist, title=f"Song {i}", track=str(i),
                   **album)  # fmt: skip
    for i, artist in enumerate(["A", "B", "C"], start=1):
        make_audio(root / f"Mix/{artist}_Song {i}.mp3", freq=900 + i * 50, seconds=0.3,
                   artist=artist, title=f"Song {i}", track=str(i), album="Mixtape")  # fmt: skip
    with Session() as session:
        lib = Library(name="music", root=str(root))
        session.add(lib)
        session.commit()
        scan(session, lib)
        plan = lo.plan(session, lib, lo.DEFAULT_LAYOUT)
        assert [(a.folder, len(a.moves)) for a in plan.albums] == [
            ("Danny Elfman/The Nightmare Before Christmas", 3),
            ("Various Artists/Mixtape", 3),
        ]


def test_layout_setting(library):
    session, lib, root = library
    assert lo.get_layout(session, lib) == lo.DEFAULT_LAYOUT
    lo.set_layout(session, lib, "{albumartist}/{year} {album}/{track} {title}")
    session.commit()
    assert lo.get_layout(session, lib) == "{albumartist}/{year} {album}/{track} {title}"
    with pytest.raises(lo.LayoutError):
        lo.set_layout(session, lib, "{nonsense}/{title}")
    assert session.scalar(select(EffectiveMetadata).limit(1)) is not None
