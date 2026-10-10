"""The decision log: what the user decided in the catalog, and undoing it.

An operation ("Rename album X → Y, 12 tracks", "Map MusicMatch ratings", "Accept gnudb's
values for album Z") changes FieldValues only, and records each value's before and after.
No file is touched: the files whose tags now differ from the catalog are pending, and
are written later by selection (writing.py). Undoing an operation sets its values back,
so the files are pending again (or no longer), and the next write puts the tags right.

An operation is undone by another operation, so the log only grows and an undo can be
undone too. Values that a later operation or scan has changed since are left as they
are: undoing reverts only what still holds the operation's value.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import batched
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import AudioFile, FieldValue, Operation, OperationValue
from .provenance import refresh_effective

# What an operation did: user edits (one file, an album, a folder), a rule set on the
# library (ratings from preferences), values of a lookup taken as the user's own, the
# user's edits dropped, and undoing another operation.
KINDS = ("edit", "ratings", "accept", "discard", "undo")

BATCH = 1000


class Value(NamedTuple):
    """The value one source is to have for one field of a file; None removes it."""

    file_id: int
    source: str
    field: str
    value: str | None


@dataclass
class Undone:
    operation: Operation | None  # the undoing operation; None when nothing could be undone
    reverted: int  # values set back
    kept: int  # values changed since, left as they are


def record(
    session: Session,
    library_id: int,
    kind: str,
    description: str,
    values: Iterable[Value],
    *,
    reverts: Operation | None = None,
) -> Operation | None:
    """Set the values and record them as one operation. Values that are already so are
    left out; when none is left, no operation is recorded and None is returned. The
    caller commits."""
    if kind not in KINDS:
        raise ValueError(f"Unknown operation kind: {kind}")
    operation = Operation(
        library_id=library_id, kind=kind, description=description,
        reverts=reverts.id if reverts else None,
    )  # fmt: skip
    changed_any = False
    for batch in batched(values, BATCH, strict=False):
        wanted = {(v.file_id, v.source, v.field): v.value for v in batch}  # the last one wins
        file_ids = {file_id for file_id, _, _ in wanted}
        current = {
            (fv.file_id, fv.source, fv.field): fv
            for fv in session.scalars(
                select(FieldValue).where(
                    FieldValue.file_id.in_(file_ids),
                    FieldValue.source.in_({source for _, source, _ in wanted}),
                )
            )
        }
        changed = []
        for (file_id, source, name), value in wanted.items():
            existing = current.get((file_id, source, name))
            before = existing.value if existing else None
            if before == value:
                continue
            if existing is None:
                session.add(FieldValue(file_id=file_id, field=name, value=value, source=source))
            elif value is None:
                session.delete(existing)
            else:
                existing.value = value
            changed.append(
                OperationValue(file_id=file_id, field=name, source=source, before=before,
                               after=value)
            )  # fmt: skip
        if not changed:
            continue
        if not changed_any:
            session.add(operation)
            changed_any = True
        operation.values.extend(changed)
        touched = {value.file_id for value in changed}
        session.flush()
        _expire_values(session, touched)
        refresh_effective(session, sorted(touched))
    if not changed_any:
        return None
    if reverts is not None:
        session.flush()
        reverts.reverted_by = operation.id
    return operation


def _expire_values(session: Session, file_ids: set[int]) -> None:
    """Files already loaded in the session see their new values on next access."""
    for obj in list(session.identity_map.values()):
        if isinstance(obj, AudioFile) and obj.id in file_ids:
            session.expire(obj, ["values"])


def undo(session: Session, operation: Operation) -> Undone:
    """Set the operation's values back to what they were before it, where they still hold
    the value it set. The caller commits."""
    if operation.reverted_by is not None:
        raise ValueError(f"Operation {operation.id} has already been undone")
    back, kept = [], 0
    for batch in batched(operation.values, BATCH, strict=False):
        current = {
            (fv.file_id, fv.source, fv.field): fv.value
            for fv in session.scalars(
                select(FieldValue).where(
                    FieldValue.file_id.in_({v.file_id for v in batch}),
                    FieldValue.source.in_({v.source for v in batch}),
                )
            )
        }
        for v in batch:
            if current.get((v.file_id, v.source, v.field)) == v.after:
                back.append(Value(v.file_id, v.source, v.field, v.before))
            else:
                kept += 1
    undoing = record(
        session, operation.library_id, "undo", f"Undo: {operation.description}", back,
        reverts=operation,
    )  # fmt: skip
    return Undone(undoing, len(back) if undoing else 0, kept)


@dataclass
class Entry:
    operation: Operation
    values: int  # how many values it changed
    files: int  # in how many files


def history(session: Session, library_id: int, limit: int = 50, offset: int = 0) -> list[Entry]:
    """The library's operations, newest first, with how much each changed."""
    counts = (
        select(
            OperationValue.operation_id,
            func.count().label("values"),
            func.count(func.distinct(OperationValue.file_id)).label("files"),
        )
        .group_by(OperationValue.operation_id)
        .subquery()
    )
    rows = session.execute(
        select(Operation, counts.c["values"], counts.c.files)
        .outerjoin(counts, counts.c.operation_id == Operation.id)
        .where(Operation.library_id == library_id)
        .order_by(Operation.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [Entry(operation, values or 0, files or 0) for operation, values, files in rows]


def changed_values(session: Session, operation: Operation, limit: int = 500) -> list:
    """The first `limit` values the operation changed, with their files, in path order."""
    return session.execute(
        select(OperationValue, AudioFile)
        .join(AudioFile, AudioFile.id == OperationValue.file_id)
        .where(OperationValue.operation_id == operation.id)
        .order_by(AudioFile.path, OperationValue.id)
        .limit(limit)
    ).all()
