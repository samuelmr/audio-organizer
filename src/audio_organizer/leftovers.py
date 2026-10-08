"""Leftover files in folders that no longer hold music.

Merging album folders, moving tracks and trashing duplicates empties folders, but the
files iTunes, Windows and old tools left next to the music stay (Folder.jpg, desktop.ini,
generated .m3u/.json/.html playlists), and a folder holding any file is never removed.
The catalog only knows audio files, so this looks at the disk: a folder with no audio
file anywhere below it is a candidate, and its files are proposed for the trash. Only
files known to be litter are proposed by default; anything else (a PDF booklet, a text
file) is listed for a look, and proposed only on request. The folders themselves
disappear when the last file leaves them.
"""

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sqlalchemy.orm import Session

from . import changes as ch
from .models import Library
from .scanner import AUDIO_EXTENSIONS

# Names (case-insensitive patterns) of files that are safe to trash once the music is gone.
LITTER = (
    ".ds_store", "desktop.ini", "thumbs.db", "folder.jpg", "folder.png", "albumart*.jpg",
    "albumart*.png", "albumartsmall.jpg", "cover.jpg", "cover.png", "front.jpg", "*.m3u",
    "*.m3u8", "*.json", "*.html", "*.htm", "*.pls", "*.wpl", "*.cue", "*.log", "*.sfv",
    "*.md5", "*.nfo", "*.url",
)  # fmt: skip


def is_litter(name: str) -> bool:
    name = name.lower()
    return name.startswith("._") or any(fnmatch.fnmatchcase(name, p) for p in LITTER)


@dataclass
class Folder:
    path: str  # relative to the library root
    litter: list[str] = field(default_factory=list)  # library paths of files known as litter
    other: list[str] = field(default_factory=list)  # files that may matter


@dataclass
class Survey:
    folders: list[Folder] = field(default_factory=list)

    @property
    def litter(self) -> int:
        return sum(len(f.litter) for f in self.folders)

    @property
    def other(self) -> int:
        return sum(len(f.other) for f in self.folders)


def survey(library: Library, path_prefix: str | None = None) -> Survey:
    """Folders under the library root (optionally only under path_prefix) without any audio
    file below them, that still hold files. Hidden and system folders ('.', '@' prefixes,
    including the trash) are not looked into, nor are folders inside one that holds
    audio (an album's Scans or Artwork folder)."""
    root = Path(library.root)
    start = root / path_prefix if path_prefix else root
    found = Survey()
    _walk(root, start, found)
    found.folders.sort(key=lambda f: f.path)
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
    if beside:  # folders inside an album folder (Scans, Artwork) belong to the album
        del found.folders[before:]
    if has_audio:
        return True
    if files and directory != root:
        relative = PurePosixPath(*directory.relative_to(root).parts)
        folder = Folder(str(relative))
        for name in files:
            (folder.litter if is_litter(name) else folder.other).append(str(relative / name))
        found.folders.append(folder)
    return False


def propose(
    session: Session, library: Library, found: Survey, description: str, *, everything: bool = False
) -> ch.Changeset | None:
    """Propose trashing the litter of the surveyed folders (all their files with
    everything). None if there is nothing to trash."""
    paths = [p for f in found.folders for p in (f.litter + (f.other if everything else []))]
    if not paths:
        return None
    changeset = ch.new_changeset(session, description, "leftovers")
    for path in paths:
        ch.propose_trash_leftover(changeset, library, path)
    session.commit()
    return changeset
