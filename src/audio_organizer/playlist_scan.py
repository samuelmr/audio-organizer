"""Reading playlists from the library disk into the catalog during a scan.

Only .m3u and .m3u8 files directly in the library's playlist folder are playlists; the
album playlists that many tools leave in the album folders are not. A file this tool
wrote itself (its content still has the hash it recorded) is not imported again. Any
other file becomes a static playlist named after the file; that includes the output of a
smart playlist that someone has edited, which then sits beside the smart playlist under
the same name. A file named like a static playlist that is not as this tool wrote it is
reported, not imported: the catalog's playlist is not replaced by a file.
"""

import posixpath
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Library, Playlist, PlaylistFile
from .playlist_files import digest
from .playlists import PLAYLIST_SUFFIXES, PathIndex, read_entries, set_entries


@dataclass
class PlaylistScan:
    imported: list[str] = field(default_factory=list)
    known: int = 0  # files that are this tool's own output or already imported
    conflicts: list[str] = field(default_factory=list)  # files that differ from a static playlist
    errors: list[tuple[str, str]] = field(default_factory=list)


def _same_entries(playlist: Playlist, pairs: list[tuple[str, int | None]]) -> bool:
    have = [(e.file_id, None if e.file_id is not None else e.path) for e in playlist.entries]
    want = [(file_id, None if file_id is not None else entry) for entry, file_id in pairs]
    return have == want


def _record(
    session: Session,
    library: Library,
    row: PlaylistFile | None,
    path: str,
    file: Path,
    data: bytes,
    playlist: Playlist,
) -> None:
    """Remember the file's content as this playlist's, so that it is not imported again."""
    if row is None:
        row = PlaylistFile(library_id=library.id, target_id=None, path=path)
        session.add(row)
    row.playlist_id, row.format = playlist.id, file.suffix.lower().lstrip(".")
    row.content_hash, row.size = digest(data), len(data)


def import_from_disk(session: Session, library: Library) -> PlaylistScan:
    """Import the playlist files in the library's playlist folder that are not known yet."""
    result = PlaylistScan()
    folder = library.playlist_folder
    directory = Path(library.root, folder)
    if not directory.is_dir():
        return result
    ledger = {
        row.path: row
        for row in session.scalars(
            select(PlaylistFile).where(
                PlaylistFile.library_id == library.id, PlaylistFile.target_id.is_(None)
            )
        )
    }
    index: PathIndex | None = None  # built when there is something to import
    for file in sorted(directory.iterdir(), key=lambda p: p.name.casefold()):
        if file.suffix.lower() not in PLAYLIST_SUFFIXES or not file.is_file():
            continue
        path = posixpath.join(folder, file.name) if folder else file.name
        try:
            data = file.read_bytes()
        except OSError as e:
            result.errors.append((path, str(e)))
            continue
        row = ledger.get(path)
        if row is not None and row.playlist_id is not None and row.content_hash == digest(data):
            result.known += 1
            continue
        name = unicodedata.normalize("NFC", file.stem)
        existing = session.scalar(
            select(Playlist).where(
                Playlist.library_id == library.id,
                Playlist.name == name,
                Playlist.rule.is_(None),
            )
        )
        if existing is not None:
            # The catalog's playlist stays; a file that says the same is just recorded
            index = index or PathIndex(session, library)
            if _same_entries(existing, read_entries(file, index)):
                _record(session, library, row, path, file, data, existing)
                result.known += 1
            else:
                result.conflicts.append(path)
            continue
        index = index or PathIndex(session, library)
        playlist = Playlist(library=library, name=name, sort="manual")
        set_entries(playlist, read_entries(file, index))
        session.add(playlist)
        session.flush()
        _record(session, library, row, path, file, data, playlist)
        result.imported.append(name)
    session.commit()
    return result
