"""Leftover files in folders that no longer hold music.

Merging album folders, moving tracks and trashing duplicates empties folders, but the
files iTunes, Windows and old tools left next to the music stay (Folder.jpg, desktop.ini,
generated .m3u/.json/.html playlists), and a folder holding any file is never removed.
The catalog only knows audio files, so this looks at the disk: a folder with no audio
file anywhere below it is a candidate, and its files are proposed for the trash. Only
files known to be litter are proposed by default; anything else (a PDF booklet, a text
file) is listed for a look, and proposed only on request. The folders themselves
disappear when the last file leaves them.

A playlist (.m3u, .html, ...) that names tracks is not litter, whatever its extension:
only one without any track in it is. The other way round, an album folder that has no
cover.jpg or cover.png but another image (AlbumArtSmall.jpg, Folder.jpg) gets a proposal
to rename the biggest of them to cover.jpg or cover.png, the name sync and the players
look for.

A playlist that is dead is litter wherever it lies, also in artist and album folders that
still hold music: one whose entries all name files that are not there (an old generated
YUP.m3u after the loose copies it listed were merged into albums). It must name at least
one track, and an entry that cannot be checked from here (a URL, a Windows path) counts as
alive.
"""

import fnmatch
import html
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from sqlalchemy.orm import Session

from . import changes as ch
from .models import Library
from .scanner import AUDIO_EXTENSIONS
from .sync import COVER_NAMES, IMAGE_EXTENSIONS

# Names (case-insensitive patterns) of files that are safe to trash once the music is gone.
LITTER = (
    ".ds_store", "desktop.ini", "thumbs.db", "folder.jpg", "folder.png", "albumart*.jpg",
    "albumart*.png", "albumartsmall.jpg", "cover.jpg", "cover.png", "front.jpg", "*.m3u",
    "*.m3u8", "*.json", "*.html", "*.htm", "*.pls", "*.wpl", "*.cue", "*.log", "*.sfv",
    "*.md5", "*.nfo", "*.url",
)  # fmt: skip

PLAYLIST_EXTENSIONS = (".m3u", ".m3u8", ".pls", ".wpl", ".html", ".htm", ".json")
PLAYLIST_READ = 2_000_000  # bytes; generated playlists are small
_TRACK = re.compile(
    "|".join(re.escape(e) + r"\b" for e in sorted(AUDIO_EXTENSIONS)) + "|MusicRecording",
    re.IGNORECASE,
)
_AUDIO_END = tuple(sorted(AUDIO_EXTENSIONS))
_FOREIGN = re.compile(
    r"^([a-z][a-z0-9+.-]*://|[a-z]:[\\/]|\\\\)", re.IGNORECASE
)  # URL, C:\, \\host
_PLS_ENTRY = re.compile(r"^file\d+=(.*)$", re.IGNORECASE)
_HREF = re.compile(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
NAMELESS_PLAYLIST_BYTES = 64_000  # a file without extension is a playlist only if small


# Files the operating system or file manager makes; they are never worth keeping twice.
SYSTEM_FILES = (".ds_store", "desktop.ini", "thumbs.db")


def is_system_file(name: str) -> bool:
    name = name.lower()
    return name.startswith("._") or name in SYSTEM_FILES


def is_audio(name: str) -> bool:
    return not name.startswith(".") and os.path.splitext(name)[1].lower() in AUDIO_EXTENSIONS


def folder_files(directory: Path) -> tuple[int, list[str]] | None:
    """(audio files directly in an album folder, its other files) when its music leaves:
    the cover, a .cue, a booklet, and the files of subfolders without audio (Scans,
    Artwork), as paths relative to it, sorted. Subfolders holding audio (another disc)
    and hidden or system ones ('.', '@' prefixes, such as Synology's @eaDir) are left out.
    None if the folder cannot be read."""
    try:
        entries = sorted(os.scandir(directory), key=lambda e: e.name)
    except OSError:
        return None
    audio, others = 0, []
    for entry in entries:
        if entry.is_dir(follow_symlinks=False):
            if entry.name.startswith((".", "@")):
                continue
            below = _files_without_audio(Path(entry.path))
            others.extend(f"{entry.name}/{name}" for name in below or [])
        elif is_audio(entry.name):
            audio += 1
        elif entry.is_file(follow_symlinks=False):
            others.append(entry.name)
    return audio, others


def _files_without_audio(directory: Path) -> list[str] | None:
    """The files below directory, relative to it, or None if any of them is audio (or the
    folder cannot be read)."""
    found = []
    try:
        entries = sorted(os.scandir(directory), key=lambda e: e.name)
    except OSError:
        return None
    for entry in entries:
        if entry.is_dir(follow_symlinks=False):
            if entry.name.startswith((".", "@")):
                continue
            below = _files_without_audio(Path(entry.path))
            if below is None:
                return None
            found.extend(f"{entry.name}/{name}" for name in below)
        elif is_audio(entry.name):
            return None
        elif entry.is_file(follow_symlinks=False):
            found.append(entry.name)
    return found


def is_litter(name: str) -> bool:
    name = name.lower()
    return name.startswith("._") or any(fnmatch.fnmatchcase(name, p) for p in LITTER)


def names_tracks(path: Path) -> bool:
    """Whether a playlist-like file names tracks (an audio file name, or a schema.org
    MusicRecording), which makes it a playlist worth keeping. Unreadable counts as yes."""
    if path.suffix.lower() not in PLAYLIST_EXTENSIONS:
        return False
    try:
        with open(path, "rb") as f:
            return _TRACK.search(f.read(PLAYLIST_READ).decode("utf-8", "ignore")) is not None
    except OSError:
        return True


def _read_text(path: Path, limit: int) -> str | None:
    try:
        with open(path, "rb") as f:
            data = f.read(limit)
    except OSError:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")  # old players wrote the system's code page


def _json_urls(node: object) -> list[str]:
    if isinstance(node, dict):
        found = [v for k, v in node.items() if k == "url" and isinstance(v, str)]
        return found + [u for v in node.values() for u in _json_urls(v)]
    if isinstance(node, list):
        return [u for v in node for u in _json_urls(v)]
    return []


def playlist_entries(path: Path) -> list[str] | None:
    """The track locations a playlist file names, as written in it, or None when it is not
    a playlist we can read: .m3u/.m3u8, .pls, .wpl, and the schema.org JSON-LD and HTML
    that `aorg` writes (only locations of audio files count in those). A file without any
    extension (old tools garbled names such as "Album.m3u" into "Albu毭3u") is read as an
    m3u when it is small and every line of it is an audio file name."""
    suffix = path.suffix.lower()
    if suffix == "" and not path.name.startswith("."):
        text = _read_text(path, NAMELESS_PLAYLIST_BYTES + 1)
        if text is None or len(text) > NAMELESS_PLAYLIST_BYTES:
            return None
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        entries = [line for line in lines if not line.startswith("#")]
        return entries if entries and all(e.lower().endswith(_AUDIO_END) for e in entries) else None
    if suffix not in PLAYLIST_EXTENSIONS:
        return None
    text = _read_text(path, PLAYLIST_READ)
    if text is None:
        return None
    if suffix in (".m3u", ".m3u8"):
        return [s for line in text.splitlines() if (s := line.strip()) and not s.startswith("#")]
    if suffix == ".pls":
        return [
            m.group(1).strip()
            for line in text.splitlines()
            if (m := _PLS_ENTRY.match(line.strip()))
        ]
    if suffix == ".json":
        try:
            urls = _json_urls(json.loads(text))
        except ValueError:
            return None
        return [u for u in urls if u.lower().endswith(_AUDIO_END)]
    links = [html.unescape(m) for m in _HREF.findall(text)]  # .wpl, .html, .htm
    entries = [u if suffix == ".wpl" else unquote(u) for u in links]
    return [e for e in entries if e.lower().endswith(_AUDIO_END)]


def _exists(path: str) -> bool:
    """Whether a file is there, whichever way its name is composed (macOS writes "ä" as
    "a" and a combining mark, other systems as one character)."""
    return any(
        os.path.exists(unicodedata.normalize(form, path)) for form in ("NFC", "NFD")
    ) or os.path.exists(path)


def is_dead_playlist(path: Path, root: Path) -> bool:
    """Whether a playlist names tracks, and not one of them is there: each entry is looked
    for beside the playlist and under the library root (or where it says, if absolute).
    Entries that cannot be checked from here count as alive, so a playlist is never
    called dead by mistake; so does one with no entries."""
    entries = playlist_entries(path)
    if not entries:
        return False
    for entry in entries:
        if _FOREIGN.match(entry):
            return False
        entry = entry.replace("\\", "/")
        places = [entry] if os.path.isabs(entry) else [path.parent / entry, root / entry]
        if any(_exists(os.fspath(p)) for p in places):
            return False
    return True


def cover_rename(directory: Path, names: list[str]) -> tuple[str, str] | None:
    """For an album folder's non-audio files: (image, new name) when the folder has no
    cover.jpg/.jpeg/.png but another image. The biggest image becomes cover.jpg or
    cover.png, following its format. None if the folder has a cover or no image."""
    images = [n for n in names if not n.startswith("._") and n.lower().endswith(IMAGE_EXTENSIONS)]
    stems = {os.path.splitext(n)[0].lower() for n in images}
    if not images or COVER_NAMES[0] in stems:
        return None
    try:
        best = max(images, key=lambda n: (os.stat(directory / n).st_size, n))
    except OSError:
        return None
    extension = ".png" if best.lower().endswith(".png") else ".jpg"
    return best, COVER_NAMES[0] + extension


@dataclass
class Folder:
    path: str  # relative to the library root
    litter: list[str] = field(default_factory=list)  # library paths of files known as litter
    other: list[str] = field(default_factory=list)  # files that may matter


@dataclass
class Survey:
    folders: list[Folder] = field(default_factory=list)
    renames: list[tuple[str, str]] = field(default_factory=list)  # library paths (from, to)
    dead: list[str] = field(default_factory=list)  # dead playlists in folders that hold music

    @property
    def litter(self) -> int:
        return sum(len(f.litter) for f in self.folders)

    @property
    def other(self) -> int:
        return sum(len(f.other) for f in self.folders)


def survey(library: Library, path_prefix: str | None = None) -> Survey:
    """Folders under the library root (optionally only under path_prefix) without any audio
    file below them, that still hold files, and album folders lacking a cover image.
    Hidden and system folders ('.', '@' prefixes, including the trash) are not looked
    into, nor are folders inside one that holds audio (an album's Scans or Artwork
    folder)."""
    root = Path(library.root)
    start = root / path_prefix if path_prefix else root
    found = Survey()
    # The library's own playlist folder is where `aorg` keeps its playlists: not looked at
    keep = root / library.playlist_folder if library.playlist_folder else None
    _walk(root, start, found, keep)
    found.folders.sort(key=lambda f: f.path)
    found.renames.sort()
    found.dead.sort()
    return found


def _walk(root: Path, directory: Path, found: Survey, keep: Path | None = None) -> bool:
    """Collect the leftover folders below directory; True if it holds audio somewhere."""
    has_audio = beside = False
    files: list[str] = []
    before = len(found.folders)
    try:
        entries = sorted(os.scandir(directory), key=lambda e: e.name)
    except OSError:
        return True  # unreadable: assume the worst, propose nothing
    for entry in entries:
        if entry.is_dir(follow_symlinks=False):
            if entry.name.startswith((".", "@")):
                continue
            if _walk(root, Path(entry.path), found, keep):
                has_audio = True
        elif entry.name.startswith("._") or (
            os.path.splitext(entry.name)[1].lower() not in AUDIO_EXTENSIONS
        ):
            files.append(entry.name)
        else:
            has_audio = beside = True
    relative = PurePosixPath(*directory.relative_to(root).parts)
    if beside:  # folders inside an album folder (Scans, Artwork) belong to the album
        del found.folders[before:]
        rename = cover_rename(directory, files)
        if rename and directory != root:
            found.renames.append((str(relative / rename[0]), str(relative / rename[1])))
    dead: set[str] = set()
    if directory != root and not (keep and (directory == keep or keep in directory.parents)):
        dead = {n for n in files if is_dead_playlist(directory / n, root)}
    if has_audio:
        found.dead.extend(str(relative / name) for name in files if name in dead)
        return True
    if files and directory != root:
        folder = Folder(str(relative))
        for name in files:
            litter = name in dead or (is_litter(name) and not names_tracks(directory / name))
            (folder.litter if litter else folder.other).append(str(relative / name))
        found.folders.append(folder)
    return False


def propose(
    session: Session,
    library: Library,
    found: Survey,
    description: str,
    *,
    everything: bool = False,
    covers: bool = True,
) -> ch.Changeset | None:
    """Propose trashing the litter of the surveyed folders (all their files with
    everything), the dead playlists in folders that hold music, and renaming images to
    cover images (unless covers is off). None if there is nothing to do."""
    paths = [p for f in found.folders for p in (f.litter + (f.other if everything else []))]
    paths += found.dead
    renames = found.renames if covers else []
    if not paths and not renames:
        return None
    changeset = ch.new_changeset(session, description, "leftovers")
    for source, target in renames:
        ch.propose_rename_file(changeset, library, source, target)
    for path in paths:
        ch.propose_trash_leftover(changeset, library, path)
    session.commit()
    return changeset
