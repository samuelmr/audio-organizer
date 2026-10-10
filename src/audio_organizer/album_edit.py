"""Editing all tracks of an album or folder at once, such as fixing a typo in the album's
name: the same manual values for every track, stored in the catalog and written into the
files later with their other pending changes (writing.py). Optionally the folders named
after the old album or artist are renamed, proposed as a changeset of moves."""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from . import changes as ch
from . import layout as lo
from . import writing as wr
from .models import AudioFile, Change, Changeset, Library
from .provenance import resolve

# The fields whose renaming can rename folders (see layout.follow_renames)
FOLDER_FIELDS = ("album", "albumartist", "artist")


@dataclass
class AlbumEdit:
    pending: list[AudioFile]  # the files whose tags now differ from the edit
    changeset: Changeset | None  # the folder moves; None when there are none
    moves: list[Change]
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
    """Store the values as manual values of every file. With move_files, also propose
    renaming the folders named after an album or artist being renamed. Moving and
    writing the tags do not depend on each other, so either can be done first."""
    before = [(file, resolve(file.values)) for file in files]
    pending = wr.store_edit(session, files, values)
    plan, changeset, moves = None, None, []
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
        changeset = ch.new_changeset(session, f"Rename folders: {description}", "manual")
        moves = lo.add_moves(changeset, library, plan.albums)
        if not moves:
            session.expunge(changeset)
            changeset = None
        session.commit()
    return AlbumEdit(pending, changeset, moves, plan)


def description(album: str | None, folder: str | None) -> str:
    return (
        f"Manual edit of album {album}" if album is not None else f"Manual edit of folder {folder}/"
    )
