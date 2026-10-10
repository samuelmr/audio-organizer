"""Editing all tracks of an album or folder at once, such as fixing a typo in the album's
name: the same manual values for every track, proposed in one changeset, optionally with
moves that rename the folders named after the old album or artist."""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from . import changes as ch
from . import layout as lo
from .models import AudioFile, Change, Changeset, Library
from .provenance import resolve

# The fields whose renaming can rename folders (see layout.follow_renames)
FOLDER_FIELDS = ("album", "albumartist", "artist")


@dataclass
class AlbumEdit:
    changeset: Changeset | None  # None when nothing needs changing in the files
    retags: list[Change]
    moves: list[Change]
    plan: lo.Plan | None  # the moves' plan, with what could not be moved and why


def propose(
    session: Session,
    library: Library,
    files: list[AudioFile],
    values: dict[str, str | None],
    description: str,
    *,
    move_files: bool = False,
) -> AlbumEdit:
    """Store the values as manual values of every file and propose writing them into the
    files' tags. With move_files, also propose renaming the folders named after an
    album or artist being renamed; the moves come after the tag changes, so they are
    applied after them and undone before them."""
    before = [(file, resolve(file.values)) for file in files]
    changeset = ch.new_changeset(session, description, "manual")
    retags = ch.propose_manual_edits(session, changeset, files, values)
    plan, moves = None, []
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
        moves = lo.add_moves(changeset, library, plan.albums)
    if not retags and not moves:
        session.expunge(changeset)
        session.commit()
        return AlbumEdit(None, [], [], plan)
    session.commit()
    return AlbumEdit(changeset, retags, moves, plan)


def description(album: str | None, folder: str | None) -> str:
    return (
        f"Manual edit of album {album}" if album is not None else f"Manual edit of folder {folder}/"
    )
