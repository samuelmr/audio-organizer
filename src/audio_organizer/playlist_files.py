"""Where playlist files are written and what is in them.

Both a library (on disk) and a sync target (in a bucket) have a playlist folder, relative
to the library root or the target's prefix, and the formats to write. The folder is empty
by default: playlists go in the root, next to the artist folders. Entries refer to tracks
relative to the playlist's own folder, so changing the folder changes the entries; the
files are generated from the catalog each time, never edited.
"""

import hashlib
import posixpath
import unicodedata
from collections.abc import Iterable

from .playlist_formats import FORMATS
from .playlists import safe_filename


def digest(data: bytes) -> str:
    """The hash that tells whether a playlist file is as this tool wrote it."""
    return hashlib.sha1(data).hexdigest()


def clean_folder(value: str) -> str:
    """A playlist folder as stored: POSIX, relative, NFC, without leading or trailing
    slashes. Raises ValueError for a path that leaves the library or bucket."""
    folder = unicodedata.normalize("NFC", value.strip().replace("\\", "/")).strip("/")
    parts = [part for part in folder.split("/") if part and part != "."]
    if ".." in parts:
        raise ValueError(f"The playlist folder {value!r} must stay inside the library")
    return "/".join(parts)


def parse_formats(value: str | Iterable[str]) -> list[str]:
    """Playlist formats from "m3u,json" (or a list), in the order given, without repeats.
    Raises ValueError for an unknown format or an empty choice."""
    items = value.split(",") if isinstance(value, str) else value
    formats = list(dict.fromkeys(f.strip().lower() for f in items if f.strip()))
    unknown = [f for f in formats if f not in FORMATS]
    if unknown:
        raise ValueError(
            f"Unknown playlist format {unknown[0]!r}; choose from {', '.join(FORMATS)}"
        )
    if not formats:
        raise ValueError("Choose at least one playlist format")
    return formats


def entry_location(folder: str, track_path: str) -> str:
    """How a playlist in `folder` refers to a track: its path relative to the folder, so
    "Artist/Album/01 Title.mp3" in the root and "../Artist/..." one folder down."""
    return posixpath.relpath(track_path, folder or ".")


def file_path(folder: str, name: str, fmt: str) -> str:
    """The playlist file's path relative to the library root or the target's prefix."""
    file_name = f"{safe_filename(name)}.{fmt}"
    return f"{folder}/{file_name}" if folder else file_name
