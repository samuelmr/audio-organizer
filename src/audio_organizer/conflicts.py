"""Files retagged outside aorg after the user decided on a value.

The user's edits (manual values) always win, so when another program changes a tag the
user has an edit for, the file would simply be pending again, and the next write would
quietly undo the change made elsewhere. Instead a rescan that sees such a tag change
records a conflict (FieldConflict): the field is no longer pending until the user keeps
the edit (it is pending again) or takes the file's value (the edit is dropped, as an
operation that can be undone). A new decision on the field settles it too, and so does a
later tag that agrees with the edit.
"""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import operations as ops
from .models import AudioFile, FieldConflict
from .provenance import comparable, refresh_effective
from .tracks import present


def detect(session: Session, file: AudioFile, new_tags: dict[str, str]) -> int:
    """Before a rescan replaces a file's tag values with new_tags: record a conflict for
    each field whose tag changed to something other than the user's edit, and drop the
    conflicts whose tag now agrees with it. Returns how many conflicts were found."""
    old_tags = {fv.field: fv.value for fv in file.values if fv.source == "tag"}
    decided = {fv.field: fv.value for fv in file.values if fv.source == "manual"}
    if not decided:
        return 0
    existing = {conflict.field: conflict for conflict in file.conflicts}
    found = 0
    for name, value in decided.items():
        old, new = old_tags.get(name), new_tags.get(name)
        agrees = new is not None and comparable(name, new) == comparable(name, value)
        if agrees:
            if name in existing:
                session.delete(existing[name])
            continue
        if old == new:
            continue  # this tag did not change: the edit is pending, or already in conflict
        if name in existing:
            existing[name].decided, existing[name].after = value, new
        else:
            session.add(FieldConflict(file=file, field=name, decided=value, before=old, after=new))
        found += 1
    return found


@dataclass
class Found:
    rows: list[tuple[FieldConflict, AudioFile]]  # the first ones, up to a limit
    total: int


def _query(library_id: int, path: str | None, file_ids: list[int] | None):
    query = (
        select(FieldConflict, AudioFile)
        .join(AudioFile, AudioFile.id == FieldConflict.file_id)
        .where(present(library_id))
    )
    if path:
        query = query.where(AudioFile.path.startswith(path, autoescape=True))
    if file_ids is not None:
        query = query.where(AudioFile.id.in_(file_ids))
    return query


def find(
    session: Session,
    library_id: int,
    path: str | None = None,
    file_ids: list[int] | None = None,
    limit: int = 500,
) -> Found:
    """The library's conflicts (under a path, or of chosen files), in path order."""
    query = _query(library_id, path, file_ids)
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.execute(query.order_by(AudioFile.path, FieldConflict.field).limit(limit))
    return Found([tuple(row) for row in rows], total)


def keep_edits(session: Session, rows: list[tuple[FieldConflict, AudioFile]]) -> int:
    """Settle conflicts by keeping the user's values: the fields are pending again, and
    the next write puts them into the files. The caller commits."""
    file_ids = sorted({file.id for _, file in rows})
    for conflict, _ in rows:
        session.delete(conflict)
    session.flush()
    for start in range(0, len(file_ids), ops.BATCH):
        refresh_effective(session, file_ids[start : start + ops.BATCH])
    return len(rows)


def take_files(
    session: Session, library_id: int, rows: list[tuple[FieldConflict, AudioFile]]
) -> int:
    """Settle conflicts by dropping the user's values, so that the files' tags count: an
    operation that can be undone. The caller commits."""
    count = len({file.id for _, file in rows})
    ops.record(
        session, library_id, "discard",
        f"Take the tags of {count} file{'s' if count != 1 else ''} retagged elsewhere",
        [ops.Value(file.id, "manual", conflict.field, None) for conflict, file in rows],
    )  # fmt: skip
    return len(rows)
