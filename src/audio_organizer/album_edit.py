"""Editing all tracks of an album or folder at once, such as fixing a typo in the album's
name, or any chosen tracks, such as the genre of a search result: the same manual values
for every track, stored in the catalog as one operation
(which can be undone) and written into the files later with their other pending changes
(writing.py). Optionally the folders named after the old album or artist are renamed:
the moves are decided too, and written with the tags."""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from . import layout as lo
from . import writing as wr
from .models import AudioFile, Library, Operation
from .provenance import resolve

# The fields whose renaming can rename folders (see layout.follow_renames)
FOLDER_FIELDS = ("album", "albumartist", "artist")


@dataclass
class AlbumEdit:
    pending: list[AudioFile]  # the files whose tags now differ from the edit
    operation: Operation | None  # the decision to move the folders; None when none is new
    moves: list[lo.Move]  # the folder moves decided on
    plan: lo.Plan | None  # the moves' plan, with what could not be moved and why


def edit(
    session: Session,
    library: Library,
    files: list[AudioFile],
    values: dict[str, str | None],
    description: str,
    *,
    move_files: bool = False,
) -> AlbumEdit:
    """Store the values as manual values of every file. With move_files, also decide on
    renaming the folders named after an album or artist being renamed; the files move
    when written from the Pending page, with their tags."""
    before = [(file, resolve(file.values)) for file in files]
    pending = wr.store_edit(session, files, values, description)
    plan, operation, moves = None, None, []
    if move_files:
        renames = []
        for file, resolved in before:
            changed = {
                name: (resolved[name].value, new)
                for name, new in values.items()
                if name in FOLDER_FIELDS and new and name in resolved
                and resolved[name].value != new
            }  # fmt: skip
            if changed:
                renames.append((file, changed))
        plan = lo.follow_renames(session, library, renames)
        moves = [move for album in plan.albums for move in album.moves]
        operation = lo.decide(session, library, plan.albums, f"Rename folders: {description}")
    return AlbumEdit(pending, operation, moves, plan)


def description(album: str | None, folder: str | None) -> str:
    return (
        f"Manual edit of album {album}" if album is not None else f"Manual edit of folder {folder}/"
    )


def selection_description(files: list[AudioFile]) -> str:
    """Names the edit of chosen tracks (say, from a search) in History."""
    return f"Manual edit of {len(files)} chosen track{'s' if len(files) != 1 else ''}"
