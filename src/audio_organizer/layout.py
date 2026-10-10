"""Moving files into a consistent folder layout, album by album.

A library's layout is a template of its files' paths, filled in from each file's
effective metadata, e.g. the default "{albumartist}/{album}/{disc}{track} {title}" gives
"Texas/The Greatest Hits/11 Halo.mp3". Fields:

    albumartist  album artist; the artist when missing; "Various Artists" on compilations
    albumartistsort, artistsort   the artist's sort name if one is set ("Morissette,
                 Alanis"; see artists.py), otherwise the name
    artist, album, title, genre, year
    track        two digits, "07"
    disc         "2-" on albums with several discs, otherwise empty

Files lacking a field the template needs are left where they are. Moves are proposed
per album (the files that get the same folder), with the album's cover image when the
last of its tracks leaves a folder. Targets that are taken, or that several files would
get, are conflicts and left alone. Paths that differ only in letter case are left alone
too: on case-insensitive file systems (macOS, most NAS shares) they are the same path.
"""

import os
import re
import string
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import artists
from . import changes as ch
from .models import AudioFile, Change, Changeset, EffectiveMetadata, Library, LibrarySetting
from .pathparse import normalize_name
from .tracks import present

DEFAULT_LAYOUT = "{albumartistsort}/{album}/{disc}{track} {title}"
TEMPLATE_FIELDS = (
    "albumartist", "albumartistsort", "artist", "artistsort", "album", "title", "genre", "year",
    "track", "disc",
)  # fmt: skip
OPTIONAL_FIELDS = ("disc",)  # may be empty
VARIOUS_ARTISTS = "Various Artists"
MAX_COMPONENT = 120
UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")


class LayoutError(ValueError):
    pass


def get_layout(session: Session, library: Library) -> str:
    setting = session.get(LibrarySetting, (library.id, "layout"))
    return setting.value if setting else DEFAULT_LAYOUT


def set_layout(session: Session, library: Library, template: str) -> None:
    validate(template)
    session.merge(LibrarySetting(library_id=library.id, key="layout", value=template.strip()))


def validate(template: str) -> list[str]:
    """The template's fields; raises LayoutError if it is not usable."""
    template = template.strip()
    try:
        names = [name for _, name, spec, conv in string.Formatter().parse(template) if name]
    except ValueError as e:
        raise LayoutError(f"Cannot read the layout: {e}") from None
    unknown = set(names) - set(TEMPLATE_FIELDS)
    if unknown:
        raise LayoutError(
            f"Unknown fields: {', '.join(sorted(unknown))}; fields: {', '.join(TEMPLATE_FIELDS)}"
        )
    if "title" not in names and "track" not in names:
        raise LayoutError("The file name needs {title} or {track}")
    if "/" not in template or template.startswith("/") or "//" in template:
        raise LayoutError("The layout needs at least one folder, like {albumartist}/{album}/...")
    return names


@dataclass
class Move:
    file: AudioFile
    target: str


@dataclass
class AlbumMove:
    folder: str  # the target folder
    moves: list[Move] = field(default_factory=list)
    extras: list[tuple[str, str]] = field(default_factory=list)  # cover images

    @property
    def sources(self) -> list[str]:
        return sorted({str(PurePosixPath(m.file.path).parent) for m in self.moves})


@dataclass
class Plan:
    albums: list[AlbumMove] = field(default_factory=list)
    in_place: int = 0  # already where the layout puts them
    case_only: int = 0  # differ only in letter case
    incomplete: int = 0  # lack a field the layout needs
    conflicts: list[tuple[AudioFile, str, str]] = field(default_factory=list)  # file, target, why
    pending: int = 0  # have changes waiting for review

    @property
    def files(self) -> int:
        return sum(len(a.moves) for a in self.albums)


def plan(
    session: Session, library: Library, template: str, *, path_prefix: str | None = None
) -> Plan:
    names = validate(template)
    result = Plan()
    rows = session.execute(
        select(AudioFile, EffectiveMetadata)
        .join(EffectiveMetadata, EffectiveMetadata.file_id == AudioFile.id)
        .where(present(library.id))
        .order_by(AudioFile.path)
    ).all()
    pending = set(
        session.scalars(select(Change.file_id).where(Change.status.in_(("proposed", "approved"))))
    )
    album_artists = _album_artists(rows)
    sorting = artists.sort_names(session, library)
    discs: dict[tuple[str, str], int] = defaultdict(int)  # album -> most discs
    for file, meta in rows:
        key = (normalize_name(album_artists[file.id] or ""), normalize_name(meta.album or ""))
        discs[key] = max(discs[key], meta.disctotal or 0, meta.discnumber or 0)

    taken = {file.path.casefold(): file for file, _ in rows}  # where files are now
    wanted: dict[str, list[Move]] = defaultdict(list)
    for file, meta in rows:
        if path_prefix and not file.path.startswith(path_prefix):
            continue
        if file.id in pending:
            result.pending += 1
            continue
        album_artist = album_artists[file.id]
        key = (normalize_name(album_artist or ""), normalize_name(meta.album or ""))
        target = render(
            template,
            names,
            meta,
            file.path,
            album_artist,
            several_discs=discs[key] > 1,
            sort_names=sorting,
        )
        if target is None:
            result.incomplete += 1
        elif target == file.path:
            result.in_place += 1
        elif target.casefold() == file.path.casefold():
            result.case_only += 1
        else:
            wanted[target.casefold()].append(Move(file, target))
    _group(result, wanted, taken)
    return result


def follow_renames(
    session: Session, library: Library, renames: list[tuple[AudioFile, dict[str, tuple[str, str]]]]
) -> Plan:
    """Moves that keep folders named after an album or artist that has been renamed, for
    files whose fields changed (field -> (old, new)); the files keep their names.

    A folder is renamed where the old album name stands in its name as whole words
    ("1992 Best of Foriegner" -> "1992 Best of Foreigner"), or where its whole name is the
    old artist or album artist (not "Yesterday" for the band Yes). Unlike plan(), this
    does not apply the layout, so it suits libraries that do not follow one."""
    result = Plan()
    wanted: dict[str, list[Move]] = defaultdict(list)
    for file, changed in renames:
        target = _renamed(file.path, changed)
        if target == file.path:
            result.in_place += 1
        elif target.casefold() == file.path.casefold():
            result.case_only += 1
        else:
            wanted[target.casefold()].append(Move(file, target))
    targets = [m.target for moves in wanted.values() for m in moves]
    taken = {}
    for start in range(0, len(targets), 500):
        # Missing files count too: their paths are still in the catalog
        for file in session.scalars(
            select(AudioFile).where(
                AudioFile.library_id == library.id,
                func.lower(AudioFile.path).in_([t.lower() for t in targets[start : start + 500]]),
            )
        ):
            taken[file.path.casefold()] = file
    _group(result, wanted, taken)
    return result


def _renamed(path: str, changed: dict[str, tuple[str, str]]) -> str:
    """The path with its folders renamed after the changed fields (see follow_renames)."""
    parts = PurePosixPath(path).parts
    folders = [unicodedata.normalize("NFC", part) for part in parts[:-1]]
    for name, (old, new) in changed.items():
        old, new = file_name(old), file_name(new)
        if not old or not new:
            continue
        if name == "album":
            words = re.compile(r"(?<!\w)" + re.escape(old) + r"(?!\w)", re.IGNORECASE)
            # A function, so that backslashes in the new name are not read as escapes
            folders = [_tidy(words.sub(lambda _, new=new: new, f)) for f in folders]
        elif name in ("albumartist", "artist"):
            folders = [new if folder.casefold() == old.casefold() else folder for folder in folders]
    return "/".join([*folders, parts[-1]])


def _group(result: Plan, wanted: dict[str, list[Move]], taken: dict[str, AudioFile]) -> None:
    """Group the wanted moves by target folder, leaving out (as conflicts) those whose
    target several files want or another file has."""
    albums: dict[str, AlbumMove] = {}
    for moves in wanted.values():
        move = moves[0]
        if len(moves) > 1:
            for m in moves:
                result.conflicts.append((m.file, m.target, "several files would get this path"))
            continue
        occupant = taken.get(move.target.casefold())
        if occupant is not None and occupant.id != move.file.id:
            result.conflicts.append((move.file, move.target, f"taken by {occupant.path}"))
            continue
        folder = str(PurePosixPath(move.target).parent)
        albums.setdefault(folder, AlbumMove(folder)).moves.append(move)
    result.albums = sorted(albums.values(), key=lambda a: a.folder.casefold())


def render(
    template: str,
    names: list[str],
    meta: EffectiveMetadata,
    path: str,
    album_artist: str | None,
    *,
    several_discs: bool,
    sort_names: dict[str, str] | None = None,
) -> str | None:
    """The path the template gives a file, or None when a field it needs is missing."""
    values = {
        "albumartist": album_artist,
        "albumartistsort": artists.sorted_name(sort_names or {}, album_artist),
        "artist": meta.artist,
        "artistsort": artists.sorted_name(sort_names or {}, meta.artist),
        "album": meta.album,
        "title": meta.title,
        "genre": meta.genre,
        "year": str(meta.year) if meta.year else None,
        "track": f"{meta.tracknumber:02}" if meta.tracknumber else None,
        "disc": f"{meta.discnumber}-" if several_discs and meta.discnumber else "",
    }
    if any(not values[n] for n in names if n not in OPTIONAL_FIELDS):
        return None
    # Each value is made safe on its own, so that it cannot add or remove folders
    safe = {k: _safe(v) if v else "" for k, v in values.items()}
    rendered = template.strip().format(**safe)
    parts = [_tidy(part) for part in rendered.split("/")]
    if not all(parts):
        return None
    suffix = PurePosixPath(path).suffix.lower()
    return "/".join(parts) + suffix


def with_extras(album: AlbumMove, library: Library) -> AlbumMove:
    """Add the cover images of folders that the album's last tracks leave."""
    root = library.root
    leaving: dict[str, int] = defaultdict(int)
    for move in album.moves:
        leaving[str(PurePosixPath(move.file.path).parent)] += 1
    album.extras = []
    for source, count in leaving.items():
        try:
            names = os.listdir(os.path.join(root, source))
        except OSError:
            continue
        audio_left = [
            n for n in names
            if not n.startswith(".") and n.lower().endswith(
                (".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".flac", ".wma", ".aif", ".aiff", ".wav")
            )
        ]  # fmt: skip
        if len(audio_left) > count:
            continue  # other tracks stay in the folder, and so does the cover
        for name in names:
            if name.lower().endswith(IMAGE_EXTENSIONS) and not name.startswith("."):
                album.extras.append((f"{source}/{name}", f"{album.folder}/{name}"))
    return album


def propose(
    session: Session, library: Library, albums: list[AlbumMove], description: str
) -> Changeset:
    """One changeset moving the albums' files; a folder's cover images go with the last
    track that leaves it."""
    changeset = ch.new_changeset(session, description, "layout")
    add_moves(changeset, library, albums)
    session.commit()
    return changeset


def add_moves(changeset: Changeset, library: Library, albums: list[AlbumMove]) -> list[Change]:
    """Propose moving the albums' files in a changeset; a folder's cover images go with
    the last track that leaves it."""
    changes = []
    for album in albums:
        with_extras(album, library)
        last_from: dict[str, Move] = {}
        for move in album.moves:
            last_from[str(PurePosixPath(move.file.path).parent)] = move
        extras_by_source: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for source, target in album.extras:
            extras_by_source[str(PurePosixPath(source).parent)].append((source, target))
        for move in album.moves:
            folder = str(PurePosixPath(move.file.path).parent)
            extras = extras_by_source.get(folder) if last_from[folder] is move else None
            changes.append(ch.propose_move(changeset, move.file, move.target, extras))
    return changes


def _album_artists(rows) -> dict[int, str | None]:
    """The album artist of each file. When the tags (or the user) do not say, it is
    decided per album, so that an album's tracks stay together: among the tracks of the
    same album in the same top folder, the artist of more than half of them, otherwise
    "Various Artists". (A soundtrack by Danny Elfman with one song by Catherine O'Hara
    goes under Danny Elfman.)"""
    result: dict[int, str | None] = {}
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for file, meta in rows:
        if meta.compilation == "1":
            result[file.id] = VARIOUS_ARTISTS
        elif meta.albumartist:
            result[file.id] = meta.albumartist
        elif not meta.album:
            result[file.id] = meta.artist
        else:
            top = PurePosixPath(file.path).parts[0]
            groups[(normalize_name(meta.album), top)].append((file, meta))
    for members in groups.values():
        counts = Counter(meta.artist for _, meta in members if meta.artist)
        choice = None
        if counts:
            artist, n = counts.most_common(1)[0]
            choice = artist if n * 2 > len(members) else VARIOUS_ARTISTS
        for file, _ in members:
            result[file.id] = choice
    return result


def file_name(value: str) -> str:
    """A metadata value made safe as a file or folder name."""
    return _tidy(_safe(value))


def _safe(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    return UNSAFE.sub("_", value)


def _tidy(part: str) -> str:
    """A folder or file name: no leading or trailing spaces and dots (troublesome on
    Windows and SMB shares), single spaces, at most MAX_COMPONENT characters."""
    part = " ".join(part.split()).strip(" .")
    return part[:MAX_COMPONENT].rstrip(" .")
