"""Cover images embedded in audio files (ID3 APIC frames, MP4 covr atoms, FLAC
pictures): reading them, and embedding an album's cover into files without one, or
whose cover is a thumbnail. The cover comes from the album's folder, or when the folder
has no usable image, from an online lookup (see coverart.py).
"""

import hashlib
import os
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
from . import operations as ops
from .images import IMAGE_TYPES
from .models import (
    AudioFile,
    CoverImage,
    CoverLookup,
    EffectiveMetadata,
    FieldValue,
    FolderImages,
    Library,
    Operation,
)
from .pathparse import normalize_name
from .tracks import present

# Images smaller than this on a side are thumbnails (such as Windows Media Player's
# AlbumArtSmall.jpg); covers that small are not embedded, and embedded ones are replaced.
MIN_PIXELS = 200
# Formats whose covers can be written (see _set_pictures): MP4, FLAC and the ID3 ones. Others,
# such as Ogg Vorbis and WMA, are left out of proposals instead of failing every time.
EMBEDDABLE = {"mp3", "aac", "mp4", "flac", "aiff", "wave"}
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
    """The tracks of one album in one folder, and those of them that need a cover."""

    folder: str
    artist: str | None
    album: str | None
    files: list[int]  # ids of the files without a cover, or with a thumbnail
    replace: set[int]  # ids of files whose embedded cover is a thumbnail
    first: int  # id of the album's first track in the folder

    @property
    def key(self) -> str | None:
        return album_key(self.artist, self.album) if self.artist and self.album else None


@dataclass
class FolderCovers:
    """A cover for the tracks of a folder: the folder's image, or one found online, which
    can also be saved as the folder's image (save_as)."""

    folder: str
    files: list[AudioFile]  # to embed the cover in
    replace: set[int] = field(default_factory=set)  # ids of files whose thumbnail goes
    image: Path | None = None  # the folder's cover image
    dimensions: tuple[int, int] | None = None  # pixels, when the image header says
    fetched: CoverImage | None = None  # found online, when the folder has no usable image
    album: str | None = None  # "Artist – Album", for a cover found online
    save_as: str | None = None  # library path to save the cover found online as
    overwrite: bool = False  # save_as is a thumbnail, to be replaced
    first: AudioFile | None = None  # the track that a save_as change belongs to
    # Only folders to be proposed load their files; those listed as too small do not

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
    save_images: bool = True,
    cached: bool = False,
) -> Survey:
    """Tracks without an embedded cover, or with one smaller than min_pixels on a side,
    and the cover each could get: the folder's image, else one found online. Folder
    images smaller than min_pixels are listed apart, not proposed.

    With save_images, a cover found online is also to be saved as the folder's
    cover.jpg (or .png), for players that read folder images and for sync, when the
    folder holds just that album and has no usable image; also when its tracks already
    have the cover embedded.

    Listing a folder is slow on a network share, so what was found is kept (FolderImages).
    With cached, folders already listed are not listed again unless they are about to be
    shown, and then only when their modification time has changed: the page can then be
    built from the catalog alone. Without it, every folder is listed afresh, as when
    proposing changes to files. With enough, the survey stops once that many folders are
    found."""
    by_folder = _albums(session, library, min_pixels, path_prefix)
    found_online = _found_online(session, min_pixels)
    listings = _Listings(session, library, cached)

    def load(ids: list[int]) -> list[AudioFile]:
        found = {f.id: f for f in session.scalars(select(AudioFile).where(AudioFile.id.in_(ids)))}
        return [found[i] for i in ids]

    result = Survey()
    root = Path(library.root)
    for folder, albums in by_folder.items():
        needy = [a for a in albums if a.files]
        only = albums[0] if len(albums) == 1 else None
        to_save = save_images and only is not None and only.key in found_online
        if not needy and not to_save:
            continue
        if enough is not None and len(result.folders) >= enough:
            result.checked_all = False
            break
        part = _survey_folder(
            folder, albums, listings.get(folder), found_online, to_save, min_pixels, root, load
        )
        if part.folders and not listings.fresh(folder):
            # What the catalog remembered decides what is shown; the disk has the last word
            part = _survey_folder(
                folder, albums, listings.get(folder, check=True), found_online, to_save,
                min_pixels, root, load,
            )  # fmt: skip
        result.folders += part.folders
        result.missing += part.missing
        result.too_small += part.too_small
        result.without_image += part.without_image
    listings.save()
    return result


class _Listings:
    """The image files of folders: from the catalog when cached, else from the disk."""

    def __init__(self, session: Session, library: Library, cached: bool):
        self.session, self.library, self.cached = session, library, cached
        self.known = {
            f.folder: f
            for f in session.scalars(
                select(FolderImages).where(FolderImages.library_id == library.id)
            )
        }
        self.checked: set[str] = set()  # listed from the disk during this survey

    def fresh(self, folder: str) -> bool:
        return folder in self.checked

    def get(self, folder: str, check: bool = False) -> FolderImages | None:
        """The folder's listing: the remembered one unless check (or not cached, or never
        listed), when it is checked against the disk: listed again if the modification time
        has changed, or always when not cached. None when the folder cannot be read."""
        known = self.known.get(folder)
        if known is not None and self.cached and not check:
            return known
        directory = Path(self.library.root) / folder
        try:
            mtime = directory.stat().st_mtime_ns
        except OSError:
            return None
        self.checked.add(folder)
        if known is not None and self.cached and known.dir_mtime_ns == mtime:
            return known
        try:
            names = os.listdir(directory)
        except OSError:
            return None
        known = _remember(self.session, self.library, folder, mtime, names, known)
        self.known[folder] = known
        return known

    def save(self) -> None:
        if self.checked:
            self.session.commit()


def _remember(
    session: Session,
    library: Library,
    folder: str,
    mtime: int,
    names: list[str],
    known: FolderImages | None,
) -> FolderImages:
    """Keep what a folder holds: the image files among its entries, and the dimensions of
    the one that serves as its cover."""
    from .sync import image_names, pick_cover

    cover = pick_cover(names)
    size = dimensions(Path(library.root) / folder / cover) if cover else None
    if known is None:
        known = FolderImages(library_id=library.id, folder=folder)
        session.add(known)
    known.dir_mtime_ns, known.images, known.cover = mtime, sorted(image_names(names)), cover
    known.cover_width, known.cover_height = size if size else (None, None)
    return known


def remember_folders(
    session: Session, library: Library, folders: dict[str, tuple[int, list[str]]]
) -> int:
    """Bring the remembered folders up to date with what a scan saw: {folder: (modification
    time, entry names)} of every folder with audio files. Folders that have not changed
    cost nothing; those that are gone are forgotten. Returns how many were read anew."""
    known = {
        f.folder: f
        for f in session.scalars(select(FolderImages).where(FolderImages.library_id == library.id))
    }
    for folder in known.keys() - folders.keys():
        session.delete(known[folder])
    changed = 0
    for folder, (mtime, names) in folders.items():
        if folder not in known or known[folder].dir_mtime_ns != mtime:
            _remember(session, library, folder, mtime, names, known.get(folder))
            changed += 1
    return changed


def _survey_folder(
    folder: str,
    albums: list[Album],
    listing: FolderImages | None,
    found_online: dict[str, CoverImage],
    to_save: bool,
    min_pixels: int,
    root: Path,
    load: Callable[[list[int]], list[AudioFile]],
) -> Survey:
    """What one folder gives to the survey, judging by its listing (None: unreadable)."""
    result = Survey()
    needy = [a for a in albums if a.files]
    directory = root / folder
    image = directory / listing.cover if listing and listing.cover else None
    size = (listing.cover_width, listing.cover_height) if image and listing.cover_width else None
    names = listing.images if listing else []
    to_save = to_save and listing is not None
    if image is not None and not small(size, min_pixels):
        if needy:
            ids = [i for a in needy for i in a.files]
            replace = set().union(*(a.replace for a in needy))
            result.folders.append(FolderCovers(folder, load(ids), replace, image, size))
        return result
    left = []
    for album in albums:
        fetched = found_online.get(album.key) if album.key else None
        target = _save_target(names, fetched.mime) if fetched and to_save else None
        if fetched is None or not (album.files or target):
            if album.files:
                left.append(album)
            continue
        result.folders.append(
            FolderCovers(
                folder,
                load(album.files),
                album.replace,
                fetched=fetched,
                album=f"{album.artist} – {album.album}",
                save_as=f"{folder}/{target[0]}" if target else None,
                overwrite=bool(target and target[1]),
                first=load([album.first])[0],
            )  # fmt: skip
        )
    result.missing.extend(left)
    if left and image is not None:
        result.too_small.append(FolderCovers(folder, [], image=image, dimensions=size))
    elif left:
        result.without_image += 1
    return result


def _save_target(names: list[str], mime: str) -> tuple[str, bool] | None:
    """The name to save a cover as in a folder with these image files, and whether a file
    of that name is there to overwrite: cover.jpg (cover.png for a PNG), which the folder's
    other images do not outrank (see sync.pick_cover). None when an existing cover.jpg
    would."""
    from .sync import COVER_NAMES, IMAGE_EXTENSIONS

    extension = ".png" if mime == "image/png" else ".jpg"
    by_name = {n.casefold(): n for n in names}
    stem = COVER_NAMES[0]
    earlier = IMAGE_EXTENSIONS[: IMAGE_EXTENSIONS.index(extension)]
    if any(stem + e in by_name for e in earlier):
        return None
    existing = by_name.get(stem + extension)
    return (existing, True) if existing else (stem + extension, False)


def _albums(
    session: Session, library: Library, min_pixels: int, path_prefix: str | None
) -> dict[str, list[Album]]:
    """The tracks by folder and album, with those that need a cover. Reads columns, not
    whole files, as a library has hundreds of thousands of tracks and the page shows few."""
    cover = (
        select(FieldValue.file_id, FieldValue.value)
        .where(FieldValue.source == "tag", FieldValue.field == tags.EMBEDDED_COVER)
        .subquery()
    )
    query = (
        select(
            AudioFile.id, AudioFile.path, AudioFile.format, cover.c.value,
            EffectiveMetadata.album, EffectiveMetadata.albumartist, EffectiveMetadata.artist,
        )
        .outerjoin(cover, cover.c.file_id == AudioFile.id)
        .outerjoin(EffectiveMetadata, EffectiveMetadata.file_id == AudioFile.id)
        .where(present(library.id))
        .order_by(AudioFile.path)
    )  # fmt: skip
    if path_prefix:
        query = query.where(AudioFile.path.startswith(path_prefix, autoescape=True))
    groups: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for row in session.execute(query):
        folder = str(PurePosixPath(row.path).parent)
        groups[folder][normalize_name(row.album) if row.album else ""].append(row)
    return {
        folder: [_album(folder, members, min_pixels) for members in albums.values()]
        for folder, albums in groups.items()
    }


def _album(folder: str, members: list, min_pixels: int) -> Album:
    """The album's artist: the album artist, else the tracks' common artist, else
    "Various Artists" (as MusicBrainz credits compilations)."""
    album = Counter(m.album for m in members if m.album).most_common(1)
    albumartist = Counter(m.albumartist for m in members if m.albumartist).most_common(1)
    artists = {m.artist for m in members if m.artist}
    if albumartist:
        artist = albumartist[0][0]
    elif len(artists) == 1:
        artist = artists.pop()
    else:
        artist = "Various Artists" if artists else None
    # A cover of unknown size (or one of an older catalog) is given the benefit of doubt
    needing = [
        m
        for m in members
        if m.format in EMBEDDABLE
        and (m.value is None or small(images.parse_size(m.value), min_pixels))
    ]
    return Album(
        folder=folder,
        artist=artist,
        album=album[0][0] if album else None,
        files=[m.id for m in needing],
        replace={m.id for m in needing if m.value is not None},
        first=members[0].id,
    )


def _found_online(session: Session, min_pixels: int) -> dict[str, CoverImage]:
    """Covers found online, by album, at least min_pixels on a side (without their data,
    which is loaded when used)."""
    rows = session.execute(
        select(CoverLookup.key, CoverImage)
        .join(CoverImage, CoverImage.sha1 == CoverLookup.image_sha1)
        .where(CoverLookup.status == "found")
    ).all()
    return {key: image for key, image in rows if not small(image.size, min_pixels)}


def decide(
    session: Session, library: Library, folders: list[FolderCovers], description: str
) -> Operation | None:
    """Decide on these folders' covers: the folder's image or the one found online is to be
    in each track that needs it, and a cover found online also saved as the folder's image.
    Only the catalog changes (as an operation that can be undone); the files are written
    with the other pending changes (writing.py). None when it was all decided already."""
    desires = []
    root = Path(library.root)
    for folder in folders:
        label = None
        if folder.fetched is not None:
            size = images.format_size(folder.fetched.size)
            label = f"the Cover Art Archive's image of {folder.album} ({size})"
        for file in folder.files:
            if folder.fetched is not None:
                spec: dict = {"cover_image": folder.fetched.sha1, "label": label}
            else:
                spec = {"image": folder.image.relative_to(root).as_posix()}
            if file.id in folder.replace:
                spec["replace"] = True
            desires.append(ops.Desire(file.id, "embed", spec))
        if folder.save_as and folder.fetched is not None and folder.first is not None:
            spec = {"cover_image": folder.fetched.sha1, "to": folder.save_as, "label": label}
            if folder.overwrite:
                spec["replace"] = True
            desires.append(ops.Desire(folder.first.id, "image", spec))
    operation = ops.record(session, library.id, "covers", description, [], desires=desires)
    session.commit()
    return operation
