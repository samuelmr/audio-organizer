"""Cover images embedded in audio files (ID3 APIC frames, MP4 covr atoms, FLAC
pictures): reading them, and embedding an album's cover into files without one, or
whose cover is a thumbnail. The cover comes from the album's folder, or when the folder
has no usable image, from an online lookup (see coverart.py).
"""

import hashlib
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from mutagen import MutagenError
from mutagen.aac import AAC
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3
from mutagen.mp4 import MP4, MP4Cover
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import images, tags
from .images import IMAGE_TYPES
from .models import AudioFile, CoverImage, CoverLookup, EffectiveMetadata, FieldValue, Library
from .pathparse import normalize_name
from .tracks import present

# Images smaller than this on a side are thumbnails (such as Windows Media Player's
# AlbumArtSmall.jpg); covers that small are not embedded, and embedded ones are replaced.
MIN_PIXELS = 200
UNCHECKED = "1"  # the embedded_cover value of catalogs made before sizes were read


def embedded(path: str | Path) -> tuple[str, bytes] | None:
    """(MIME type, data) of the file's front cover (or first picture), or None."""
    return tags.front_picture(*tags.load_tags(path))


@dataclass
class Embedded:
    """A picture in a file, with what is needed to put it back."""

    mime: str
    data: bytes
    type: int = 3  # front cover
    desc: str = ""


def embed(path: str | Path, image: str | Path) -> None:
    """Embed the image file as the file's front cover."""
    image = Path(image)
    mime = IMAGE_TYPES.get(image.suffix.lower())
    if mime is None:
        raise ValueError(f"Not a JPEG or PNG image: {image.name}")
    embed_data(path, mime, image.read_bytes())


def embed_data(
    path: str | Path, mime: str, data: bytes, *, replace: bool = False
) -> list[Embedded]:
    """Embed an image as the file's front cover. With replace, the file's front covers
    (or all its pictures, when none is marked as the front cover) make way for it; they
    are returned, so that undo can put them back."""
    pictures = _pictures(path)
    removed = []
    if replace:
        fronts = [p for p in pictures if p.type == 3] or pictures
        removed = [p for p in pictures if p in fronts]
        pictures = [p for p in pictures if p not in fronts]
    taken = {p.desc for p in pictures}
    desc = "" if "" not in taken else "Cover"  # ID3 tells pictures apart by description
    _set_pictures(path, [Embedded(mime, data, 3, desc), *pictures])
    return removed


def unembed(path: str | Path, sha1: str, restore: list[Embedded]) -> None:
    """Undo embed_data: remove the picture with this data and put back those it replaced."""
    kept = [p for p in _pictures(path) if digest(p.data) != sha1]
    _set_pictures(path, [*restore, *kept])


def remove(path: str | Path) -> None:
    """Remove the file's embedded pictures (to undo embedding one, in changes applied
    before embedding recorded what it replaced)."""
    _set_pictures(path, [])


def digest(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _pictures(path: str | Path) -> list[Embedded]:
    audio, file_tags = _load(path)
    if isinstance(audio, MP4):
        return [
            Embedded(
                "image/png" if c.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg", bytes(c)
            )
            for c in (audio.tags or {}).get("covr", [])
        ]
    if isinstance(audio, FLAC):
        return [Embedded(p.mime, p.data, p.type, p.desc) for p in audio.pictures]
    if isinstance(file_tags, ID3) or (audio is not None and _id3_format(audio)):
        frames = file_tags.getall("APIC") if file_tags is not None else []
        return [Embedded(f.mime or "image/jpeg", f.data, f.type, f.desc) for f in frames]
    raise tags.UnsupportedFormat(f"Covers can only be embedded in MP3, AAC, MP4 and FLAC: {path}")


def _load(path: str | Path):
    try:
        return tags.load_tags(path)
    except MutagenError as e:
        raise tags.UnsupportedFormat(f"Not a readable audio file: {path} ({e})") from None


def _id3_format(audio) -> bool:
    """A format whose tags are ID3 (MP3, AAC with an ID3 tag, AIFF, WAV)."""
    return isinstance(audio, AAC) or audio.tags is None or isinstance(audio.tags, ID3)


def _set_pictures(path: str | Path, pictures: list[Embedded]) -> None:
    audio, file_tags = _load(path)
    if audio is None:
        raise tags.UnsupportedFormat(f"Not a recognized audio file: {path}")
    if isinstance(audio, MP4):
        if audio.tags is None:
            if not pictures:
                return
            audio.add_tags()
        if pictures:
            audio.tags["covr"] = [
                MP4Cover(
                    p.data, MP4Cover.FORMAT_PNG if p.mime == "image/png" else MP4Cover.FORMAT_JPEG
                )
                for p in pictures
            ]
        else:
            audio.tags.pop("covr", None)
        audio.save()
    elif isinstance(audio, FLAC):
        audio.clear_pictures()
        for p in pictures:
            picture = Picture()
            picture.type, picture.mime, picture.desc, picture.data = p.type, p.mime, p.desc, p.data
            size = images.dimensions(p.data[:65536])
            if size:
                picture.width, picture.height = size
            audio.add_picture(picture)
        audio.save()
    elif _id3_format(audio):
        if file_tags is None and not pictures:
            return

        def change(t: ID3) -> None:
            t.delall("APIC")
            for p in pictures:
                t.add(APIC(encoding=3, mime=p.mime, type=p.type, desc=p.desc, data=p.data))

        _save_id3(audio, file_tags, path, change)
    else:
        raise tags.UnsupportedFormat(
            f"Covers can only be embedded in MP3, AAC, MP4 and FLAC: {path}"
        )


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


def store_image(
    session: Session, data: bytes, source: str, url: str | None = None, mime: str | None = None
) -> CoverImage:
    """Keep an image in the catalog (once per content)."""
    sha1 = digest(data)
    stored = session.get(CoverImage, sha1)
    if stored is None:
        size = images.dimensions(data[:65536])
        stored = CoverImage(
            sha1=sha1, mime=mime or images.mime_type(data) or "image/jpeg", data=data,
            width=size[0] if size else None, height=size[1] if size else None,
            source=source, url=url,
        )  # fmt: skip
        session.add(stored)
    return stored


def dimensions(image: Path) -> tuple[int, int] | None:
    """(width, height) of a JPEG or PNG image file from its header, or None."""
    try:
        with open(image, "rb") as f:
            return images.dimensions(f.read(64 * 1024))
    except OSError:
        return None


def small(size: tuple[int, int] | None, min_pixels: int) -> bool:
    """Whether an image is a thumbnail; one of unknown size is given the benefit of doubt."""
    return size is not None and min(size) < min_pixels


def refresh_sizes(
    session: Session, library: Library, progress: Callable[[int, int], None] | None = None
) -> int:
    """Read the size of the embedded covers that the catalog only knows exist (catalogs
    made before sizes were read). Reads only the files' tags, which is much less than
    `aorg scan --reread` reads."""
    rows = session.execute(
        select(AudioFile, FieldValue)
        .join(FieldValue, FieldValue.file_id == AudioFile.id)
        .where(
            present(library.id),
            FieldValue.source == "tag",
            FieldValue.field == tags.EMBEDDED_COVER,
            FieldValue.value == UNCHECKED,
        )
    ).all()
    for count, (file, value) in enumerate(rows, start=1):
        try:
            found = embedded(Path(library.root) / file.path)
        except (OSError, tags.UnsupportedFormat, ValueError):
            found = None
        size = images.dimensions(found[1][:65536]) if found else None
        value.value = images.format_size(size)
        if count % 200 == 0:
            session.commit()
        if progress:
            progress(count, len(rows))
    session.commit()
    return len(rows)


# ---- finding covers for albums -------------------------------------------------------


def album_key(artist: str, album: str) -> str:
    return f"{normalize_name(artist)}\x1f{normalize_name(album)}"


@dataclass
class Album:
    """The tracks of one album in one folder that need a cover."""

    folder: str
    artist: str | None
    album: str | None
    files: list[AudioFile]
    replace: set[int]  # ids of files whose embedded cover is a thumbnail

    @property
    def key(self) -> str | None:
        return album_key(self.artist, self.album) if self.artist and self.album else None


@dataclass
class FolderCovers:
    """A cover to embed into tracks of a folder: the folder's image, or one found online."""

    folder: str
    files: list[AudioFile]
    replace: set[int] = field(default_factory=set)  # ids of files whose thumbnail goes
    image: Path | None = None  # the folder's cover image
    dimensions: tuple[int, int] | None = None  # pixels, when the image header says
    fetched: CoverImage | None = None  # found online, when the folder has no usable image
    album: str | None = None  # "Artist – Album", for a cover found online

    @property
    def image_size(self) -> int:
        if self.fetched is not None:
            return len(self.fetched.data)
        return self.image.stat().st_size if self.image else 0

    @property
    def image_name(self) -> str:
        return self.image.name if self.image else "found online"


@dataclass
class Survey:
    folders: list[FolderCovers] = field(default_factory=list)
    without_image: int = 0  # folders whose tracks lack a cover, with no image at all
    too_small: list[FolderCovers] = field(default_factory=list)  # images under min_pixels
    missing: list[Album] = field(default_factory=list)  # albums without a usable cover
    checked_all: bool = True  # False when the survey stopped after enough folders


def survey(
    session: Session,
    library: Library,
    *,
    path_prefix: str | None = None,
    enough: int | None = None,
    min_pixels: int = MIN_PIXELS,
) -> Survey:
    """Tracks without an embedded cover, or with one smaller than min_pixels on a side,
    and the cover each could get: the folder's image, else one found online. Folder
    images smaller than min_pixels are listed apart, not proposed. Looking for the
    images lists each folder, slow on a network share, so with enough the survey stops
    once that many folders are found."""
    from .sync import find_cover

    by_folder = _needing(session, library, min_pixels, path_prefix)
    result = Survey()
    for folder, albums in by_folder.items():
        if enough is not None and len(result.folders) >= enough:
            result.checked_all = False
            break
        image = find_cover(Path(library.root) / folder)
        size = dimensions(image) if image else None
        if image is not None and not small(size, min_pixels):
            files = [f for a in albums for f in a.files]
            replace = set().union(*(a.replace for a in albums))
            result.folders.append(FolderCovers(folder, files, replace, image, size))
            continue
        fetched = _fetched(session, [a.key for a in albums if a.key], min_pixels)
        left = []
        for album in albums:
            found = fetched.get(album.key) if album.key else None
            if found is None:
                left.append(album)
                continue
            label = f"{album.artist} – {album.album}"
            result.folders.append(
                FolderCovers(folder, album.files, album.replace, fetched=found, album=label)
            )
        result.missing.extend(left)
        if left and image is not None:
            files = [f for a in left for f in a.files]
            result.too_small.append(FolderCovers(folder, files, image=image, dimensions=size))
        elif left:
            result.without_image += 1
    return result


def _needing(
    session: Session, library: Library, min_pixels: int, path_prefix: str | None
) -> dict[str, list[Album]]:
    """The tracks that need a cover, by folder and album."""
    cover = (
        select(FieldValue.file_id, FieldValue.value)
        .where(FieldValue.source == "tag", FieldValue.field == tags.EMBEDDED_COVER)
        .subquery()
    )
    query = (
        select(AudioFile, cover.c.value, EffectiveMetadata)
        .outerjoin(cover, cover.c.file_id == AudioFile.id)
        .outerjoin(EffectiveMetadata, EffectiveMetadata.file_id == AudioFile.id)
        .where(present(library.id))
        .order_by(AudioFile.path)
    )
    if path_prefix:
        query = query.where(AudioFile.path.startswith(path_prefix, autoescape=True))
    groups: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for file, value, meta in session.execute(query):
        size = images.parse_size(value)
        if value is not None and not small(size, min_pixels):
            continue  # has a cover that is big enough, or of unknown size
        album = meta.album if meta and meta.album else None
        folder = str(PurePosixPath(file.path).parent)
        groups[folder][normalize_name(album) if album else ""].append((file, value, meta))
    return {
        folder: [_album(folder, members) for members in albums.values()]
        for folder, albums in groups.items()
    }


def _album(folder: str, members: list) -> Album:
    """The album's artist: the album artist, else the tracks' common artist, else
    "Various Artists" (as MusicBrainz credits compilations)."""
    metas = [meta for _, _, meta in members if meta is not None]
    album = Counter(m.album for m in metas if m.album).most_common(1)
    albumartist = Counter(m.albumartist for m in metas if m.albumartist).most_common(1)
    artists = {m.artist for m in metas if m.artist}
    if albumartist:
        artist = albumartist[0][0]
    elif len(artists) == 1:
        artist = artists.pop()
    else:
        artist = "Various Artists" if artists else None
    return Album(
        folder=folder,
        artist=artist,
        album=album[0][0] if album else None,
        files=[file for file, _, _ in members],
        replace={file.id for file, value, _ in members if value is not None},
    )


def _fetched(session: Session, keys: list[str], min_pixels: int) -> dict[str, CoverImage]:
    """Covers found online for these albums, at least min_pixels on a side."""
    if not keys:
        return {}
    rows = session.execute(
        select(CoverLookup.key, CoverImage)
        .join(CoverImage, CoverImage.sha1 == CoverLookup.image_sha1)
        .where(CoverLookup.key.in_(keys), CoverLookup.status == "found")
    ).all()
    return {key: image for key, image in rows if not small(image.size, min_pixels)}


def propose(session: Session, library: Library, folders: list[FolderCovers], description: str):
    from . import changes as ch

    changeset = ch.new_changeset(session, description)
    root = Path(library.root)
    for folder in folders:
        for file in folder.files:
            replace = file.id in folder.replace
            if folder.fetched is not None:
                size = images.format_size(folder.fetched.size)
                label = f"the Cover Art Archive's image of {folder.album} ({size})"
                ch.propose_embed_cover(
                    changeset, file, cover_image=folder.fetched.sha1, label=label,
                    replace=replace,
                )  # fmt: skip
            else:
                image = folder.image.relative_to(root).as_posix()
                ch.propose_embed_cover(changeset, file, image, replace=replace)
    session.commit()
    return changeset
