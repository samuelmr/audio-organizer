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

import json
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import batched
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    AudioFile,
    FieldConflict,
    FieldValue,
    FileDesire,
    Operation,
    OperationValue,
)
from .provenance import refresh_effective

# What an operation did: user edits (one file, an album, a folder), a rule set on the
# library (ratings from preferences), values of a lookup taken as the user's own, the
# user's edits dropped, covers chosen, duplicates trashed, files moved into the layout, albums
# merged, encodings chosen, loose tracks put in
# their albums, and undoing another operation.
KINDS = (
    "edit", "ratings", "accept", "discard", "covers", "duplicates", "layout", "merge", "encodings",
    "neighbours", "undo",
)  # fmt: skip

# The source of an operation value that is a desired state (models.FileDesire), not a
# FieldValue: its field is the desire's kind, before and after are its spec as JSON.
DESIRE = "desire"

BATCH = 1000


class Value(NamedTuple):
    """The value one source is to have for one field of a file; None removes it."""

    file_id: int
    source: str
    field: str
    value: str | None


class Desire(NamedTuple):
    """What the user wants done to a file (a cover, the trash, a move); spec None removes it."""

    file_id: int
    kind: str
    spec: dict | None


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
    desires: Iterable[Desire] = (),
) -> Operation | None:
    """Set the values (and desired covers) and record them as one operation. Values that
    are already so are left out; when none is left, no operation is recorded and None is
    returned. The caller commits."""
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
        _settle_conflicts(session, {(v.file_id, v.field) for v in changed if v.source == "manual"})
        session.flush()
        _expire_values(session, touched)
        refresh_effective(session, sorted(touched))
    changed = _set_desires(session, desires)
    if changed:
        if not changed_any:
            session.add(operation)
            changed_any = True
        operation.values.extend(changed)
    if not changed_any:
        return None
    if reverts is not None:
        session.flush()
        reverts.reverted_by = operation.id
    return operation


def _set_desires(session: Session, desires: Iterable[Desire]) -> list[OperationValue]:
    """Set the desired covers; a decided cover is pending again, even if the same one was
    written before. Returns what changed, as operation values."""
    wanted = {(d.file_id, d.kind): d.spec for d in desires}
    if not wanted:
        return []
    current = {
        (row.file_id, row.kind): row
        for batch in batched(wanted, BATCH, strict=False)
        for row in session.scalars(
            select(FileDesire).where(FileDesire.file_id.in_({f for f, _ in batch}))
        )
    }
    changed = []
    for (file_id, kind), spec in wanted.items():
        row = current.get((file_id, kind))
        before = row.spec if row and not row.written else None
        if row and not row.written and row.spec == spec:
            continue
        if row is None and spec is None:
            continue
        if row is None:
            session.add(FileDesire(file_id=file_id, kind=kind, spec=spec))
        elif spec is None:
            session.delete(row)
        else:
            row.spec, row.written = spec, False
        changed.append(
            OperationValue(
                file_id=file_id, field=kind, source=DESIRE,
                before=json.dumps(before) if before else None,
                after=json.dumps(spec) if spec else None,
            )
        )  # fmt: skip
    return changed


def _settle_conflicts(session: Session, keys: set[tuple[int, str]]) -> None:
    """A new decision on a field retagged elsewhere (conflicts.py) settles the conflict:
    drop those of these (file id, field) pairs."""
    if not keys:
        return
    for conflict in session.scalars(
        select(FieldConflict).where(FieldConflict.file_id.in_({f for f, _ in keys}))
    ):
        if (conflict.file_id, conflict.field) in keys:
            session.delete(conflict)


def _expire_values(session: Session, file_ids: set[int]) -> None:
    """Files already loaded in the session see their new values on next access."""
    for obj in list(session.identity_map.values()):
        if isinstance(obj, AudioFile) and obj.id in file_ids:
            session.expire(obj, ["values", "conflicts"])


def undo(session: Session, operation: Operation) -> Undone:
    """Set the operation's values back to what they were before it, where they still hold
    the value it set. The caller commits."""
    if operation.reverted_by is not None:
        raise ValueError(f"Operation {operation.id} has already been undone")
    back, back_desires, kept = [], [], 0
    for batch in batched(operation.values, BATCH, strict=False):
        for v in batch:
            if v.source != DESIRE:
                continue
            row = session.get(FileDesire, (v.file_id, v.field))
            now = None if row is None else "written" if row.written else json.dumps(row.spec)
            if now == v.after:
                back_desires.append(
                    Desire(v.file_id, v.field, json.loads(v.before) if v.before else None)
                )
            else:  # changed since, or already written: undo the write first
                kept += 1
        batch = [v for v in batch if v.source != DESIRE]
        if not batch:
            continue
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
        reverts=operation, desires=back_desires,
    )  # fmt: skip
    return Undone(undoing, len(back) + len(back_desires) if undoing else 0, kept)


@dataclass
class Entry:
    operation: Operation
    values: int  # how many values it changed
    files: int  # in how many files


class Summed(NamedTuple):
    field: str
    after: str | None  # the value it set; None when it removed one, or for a desire
    files: int
    desire: bool  # a cover, trash or move: "after" is a spec, not worth showing


def summary(session: Session, operation: Operation, limit: int = 8) -> list[Summed]:
    """What the operation set, in a line per field and value ("genre → Pop Rock: 30
    files"), the most files first: a reminder of the decision above the files it touched,
    where a rule or edit has far too many values to read one by one."""
    rows = session.execute(
        select(
            OperationValue.field,
            OperationValue.source,
            OperationValue.after,
            func.count(func.distinct(OperationValue.file_id)).label("files"),
        )
        .where(OperationValue.operation_id == operation.id)
        .group_by(OperationValue.field, OperationValue.source, OperationValue.after)
        .order_by(func.count(func.distinct(OperationValue.file_id)).desc(), OperationValue.field)
        .limit(limit)
    )
    return [
        Summed(field, None if source == DESIRE else after, files, source == DESIRE)
        for field, source, after, files in rows
    ]


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
