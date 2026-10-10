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
"""

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

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
    _walk(root, start, found)
    found.folders.sort(key=lambda f: f.path)
    found.renames.sort()
    return found


def _walk(root: Path, directory: Path, found: Survey) -> bool:
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
            if _walk(root, Path(entry.path), found):
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
    if has_audio:
        return True
    if files and directory != root:
        folder = Folder(str(relative))
        for name in files:
            litter = is_litter(name) and not names_tracks(directory / name)
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
    everything) and renaming images to cover images (unless covers is off). None if
    there is nothing to do."""
    paths = [p for f in found.folders for p in (f.litter + (f.other if everything else []))]
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
