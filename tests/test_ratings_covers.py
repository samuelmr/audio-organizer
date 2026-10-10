import subprocess

import pytest
from sqlalchemy import delete, select

from audio_organizer import changes as ch
from audio_organizer import covers, ratings, tags, writing
from audio_organizer.models import AudioFile, FieldValue, Library
from audio_organizer.provenance import resolve
from audio_organizer.scanner import scan


@pytest.fixture
def library(Session, copy_audio, tmp_path):
    root = tmp_path / "music"
    excellent = copy_audio("mp3", root / "Air/Moon Safari/01 La Femme d'Argent.mp3")
    tags.write(excellent, {"preference": "Excellent"})
    good_rated = copy_audio("mp3b", root / "Air/Moon Safari/02 Sexy Boy.mp3")
    tags.write(good_rated, {"preference": "Good", "rating": "2"})  # a rating in the file
    good = copy_audio("m4a", root / "Air/Moon Safari/03 All I Need.m4a")
    tags.write(good, {"preference": "Good"})
    (root / "Air/Moon Safari/Folder.jpg").write_bytes(b"\xff\xd8\xff\xe0 cover")
    covers.embed(good, root / "Air/Moon Safari/Folder.jpg")  # already has a cover
    session = Session()
    lib = Library(name="music", root=str(root))
    session.add(lib)
    session.commit()
    scan(session, lib)
    yield session, lib, root
    session.close()


def _rating(session, path):
    file = session.scalar(select(AudioFile).where(AudioFile.path == path))
    session.refresh(file)
    return resolve(file.values).get("rating")


def test_ratings_from_preferences(library):
    session, lib, root = library
    assert ratings.preferences(session, lib) == {"Good": 2, "Excellent": 1}
    assert ratings.apply(session, lib, ratings.get_map(session, lib)) == 3
    assert _rating(session, "Air/Moon Safari/01 La Femme d'Argent.mp3").value == "5"
    kept = _rating(session, "Air/Moon Safari/02 Sexy Boy.mp3")
    assert (kept.value, kept.source) == ("2", "tag")  # the file's own rating wins

    found = writing.survey(session, lib, writing.Selection(only=["rating"]))
    assert sorted(p.file.path for p in found.files) == [
        "Air/Moon Safari/01 La Femme d'Argent.mp3",
        "Air/Moon Safari/03 All I Need.m4a",
    ]
    _, outcomes = writing.write(session, found.files, "Write ratings")
    assert all(o.ok for o in outcomes)
    assert tags.read(root / "Air/Moon Safari/01 La Femme d'Argent.mp3").fields["rating"] == "5"
    assert tags.read(root / "Air/Moon Safari/03 All I Need.m4a").fields["rating"] == "3"

    mapping = {**ratings.get_map(session, lib), "Excellent": None}
    ratings.set_map(session, lib, mapping)
    session.commit()
    assert ratings.get_map(session, lib)["Excellent"] is None
    with pytest.raises(ValueError):
        ratings.set_map(session, lib, {"Good": 7})


def test_covers_in_batches(library):
    session, lib, root = library
    found = covers.survey(session, lib)
    [folder] = found.folders
    assert folder.image.name == "Folder.jpg"
    assert sorted(f.path for f in folder.files) == [
        "Air/Moon Safari/01 La Femme d'Argent.mp3",
        "Air/Moon Safari/02 Sexy Boy.mp3",
    ]  # the m4a already has one
    changeset = covers.propose(session, lib, [folder], "Embed")
    ch.set_status(changeset.changes, "approved")
    assert all(o.ok for o in ch.apply(session, changeset))
    for name in ("01 La Femme d'Argent.mp3", "02 Sexy Boy.mp3"):
        assert covers.embedded(root / "Air/Moon Safari" / name) is not None
    assert covers.survey(session, lib).folders == []


def _image(path, size):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=red:s={size}",
                    "-frames:v", "1", str(path)], check=True)  # fmt: skip
    return path


def test_image_dimensions_and_thumbnails(library, tmp_path):
    session, lib, root = library
    assert covers.dimensions(_image(tmp_path / "a.jpg", "320x240")) == (320, 240)
    assert covers.dimensions(_image(tmp_path / "a.png", "64x48")) == (64, 48)
    assert covers.dimensions(tmp_path / "missing.jpg") is None

    (root / "Air/Moon Safari/Folder.jpg").unlink()
    _image(root / "Air/Moon Safari/AlbumArtSmall.jpg", "75x75")
    found = covers.survey(session, lib)
    assert found.folders == []
    [small] = found.too_small
    assert (small.image.name, small.dimensions) == ("AlbumArtSmall.jpg", (75, 75))


def test_cover_is_not_embedded_twice(library):
    """A catalog made before cover detection does not know about existing covers."""
    session, lib, root = library
    session.execute(delete(FieldValue).where(FieldValue.field == tags.EMBEDDED_COVER))
    session.commit()
    [folder] = covers.survey(session, lib).folders
    assert len(folder.files) == 3
    changeset = covers.propose(session, lib, [folder], "Embed")
    ch.set_status(changeset.changes, "approved")
    outcomes = {o.change.file.path: o for o in ch.apply(session, changeset)}
    refused = outcomes["Air/Moon Safari/03 All I Need.m4a"]
    assert not refused.ok and "already has an embedded cover" in refused.message
