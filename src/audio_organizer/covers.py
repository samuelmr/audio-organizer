"""Cover images embedded in audio files (ID3 APIC frames, MP4 covr atoms, FLAC
pictures): reading them, and embedding an album's cover image into files without one.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from mutagen.aac import AAC
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3
from mutagen.mp4 import MP4, MP4Cover
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import tags
from .models import AudioFile, FieldValue, Library
from .tracks import present

IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def embedded(path: str | Path) -> tuple[str, bytes] | None:
    """(MIME type, data) of the file's front cover (or first picture), or None."""
    audio, file_tags = tags.load_tags(path)
    if isinstance(file_tags, ID3):
        pictures = file_tags.getall("APIC")
        pictures.sort(key=lambda p: p.type != 3)  # the front cover first
        if pictures:
            return pictures[0].mime or "image/jpeg", pictures[0].data
    elif isinstance(audio, MP4) and audio.tags and audio.tags.get("covr"):
        cover = audio.tags["covr"][0]
        mime = "image/png" if cover.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"
        return mime, bytes(cover)
    elif isinstance(audio, FLAC) and audio.pictures:
        picture = sorted(audio.pictures, key=lambda p: p.type != 3)[0]
        return picture.mime, picture.data
    return None


def embed(path: str | Path, image: str | Path) -> None:
    """Embed the image as the file's front cover."""
    image = Path(image)
    mime = IMAGE_TYPES.get(image.suffix.lower())
    if mime is None:
        raise ValueError(f"Not a JPEG or PNG image: {image.name}")
    data = image.read_bytes()
    audio, file_tags = tags.load_tags(path)
    if audio is None:
        raise tags.UnsupportedFormat(f"Not a recognized audio file: {path}")
    if isinstance(audio, MP4):
        if audio.tags is None:
            audio.add_tags()
        fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        audio.tags["covr"] = [MP4Cover(data, imageformat=fmt)]
        audio.save()
    elif isinstance(audio, FLAC):
        picture = Picture()
        picture.type, picture.mime, picture.data = 3, mime, data
        audio.add_picture(picture)
        audio.save()
    else:
        _save_id3(audio, file_tags, path, lambda t: t.add(
            APIC(encoding=3, mime=mime, type=3, desc="", data=data)
        ))  # fmt: skip


def remove(path: str | Path) -> None:
    """Remove the file's embedded pictures (to undo embedding one)."""
    audio, file_tags = tags.load_tags(path)
    if isinstance(audio, MP4):
        if audio.tags is not None:
            audio.tags.pop("covr", None)
            audio.save()
    elif isinstance(audio, FLAC):
        audio.clear_pictures()
        audio.save()
    elif isinstance(file_tags, ID3):
        _save_id3(audio, file_tags, path, lambda t: t.delall("APIC"))


def _save_id3(audio, file_tags, path, change) -> None:
    had_tag = file_tags is not None
    if isinstance(audio, AAC):
        file_tags = file_tags if had_tag else ID3()
    elif not had_tag:
        audio.add_tags()
        file_tags = audio.tags
    version = tags.id3_tag_version(file_tags) if had_tag else 3
    change(file_tags)
    tags.prepare_id3(file_tags, version)
    if isinstance(audio, AAC):
        file_tags.save(path, v2_version=version)
    else:
        audio.save(v2_version=version)


# ---- embedding in batches ------------------------------------------------------------


@dataclass
class FolderCovers:
    folder: str
    image: Path  # the folder's cover image
    files: list[AudioFile]  # without an embedded cover
    dimensions: tuple[int, int] | None = None  # pixels, when the image header says

    @property
    def image_size(self) -> int:
        return self.image.stat().st_size


@dataclass
class Survey:
    folders: list[FolderCovers] = field(default_factory=list)
    without_image: int = 0  # folders whose tracks lack a cover, but with no image
    too_small: list[FolderCovers] = field(default_factory=list)  # images under min_pixels
    checked_all: bool = True  # False when the survey stopped after enough folders


def survey(
    session: Session,
    library: Library,
    *,
    path_prefix: str | None = None,
    enough: int | None = None,
    min_pixels: int = 200,
) -> Survey:
    """Folders with a cover image and tracks without an embedded cover. Images smaller
    than min_pixels on a side (thumbnails) are listed apart, not proposed. Looking for
    the images lists each folder, slow on a network share, so with enough the survey
    stops once that many folders are found."""
    from .sync import find_cover

    has_cover = select(FieldValue.file_id).where(
        FieldValue.source == "tag", FieldValue.field == tags.EMBEDDED_COVER
    )
    query = (
        select(AudioFile)
        .where(present(library.id), AudioFile.id.not_in(has_cover))
        .order_by(AudioFile.path)
    )
    if path_prefix:
        query = query.where(AudioFile.path.startswith(path_prefix, autoescape=True))
    by_folder: dict[str, list[AudioFile]] = defaultdict(list)
    for file in session.scalars(query):
        by_folder[str(PurePosixPath(file.path).parent)].append(file)
    result = Survey()
    for folder, files in by_folder.items():
        if enough is not None and len(result.folders) >= enough:
            result.checked_all = False
            break
        image = find_cover(Path(library.root) / folder)
        if image is None:
            result.without_image += 1
            continue
        found = FolderCovers(folder, image, files, dimensions(image))
        if found.dimensions and min(found.dimensions) < min_pixels:
            result.too_small.append(found)  # e.g. Windows Media Player's AlbumArtSmall.jpg
        else:
            result.folders.append(found)
    return result


def dimensions(image: Path) -> tuple[int, int] | None:
    """(width, height) of a JPEG or PNG image from its header, or None."""
    try:
        with open(image, "rb") as f:
            head = f.read(64 * 1024)
    except OSError:
        return None
    if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    if head[:2] != b"\xff\xd8":
        return None
    pos = 2
    while pos + 9 < len(head):
        if head[pos] != 0xFF:
            return None
        marker, length = head[pos + 1], int.from_bytes(head[pos + 2 : pos + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # start of frame
            height = int.from_bytes(head[pos + 5 : pos + 7], "big")
            width = int.from_bytes(head[pos + 7 : pos + 9], "big")
            return width, height
        pos += 2 + length
    return None


def propose(session: Session, library: Library, folders: list[FolderCovers], description: str):
    from . import changes as ch

    changeset = ch.new_changeset(session, description)
    root = Path(library.root)
    for folder in folders:
        image = folder.image.relative_to(root).as_posix()
        for file in folder.files:
            ch.propose_embed_cover(changeset, file, image)
    session.commit()
    return changeset
