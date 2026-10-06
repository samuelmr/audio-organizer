"""Reviewable, undoable modifications of files.

Changes are proposed into a Changeset, approved or rejected one by one (or all at
once), and only approved changes are applied. Applying records what is needed to undo
each change. A file is only touched if it is unchanged on disk since it was last
scanned, so edits made elsewhere are never silently overwritten.
"""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import pathparse, tags
from .models import AudioFile, Change, Changeset, utcnow
from .provenance import resolve, set_values


class ChangeError(Exception):
    pass


@dataclass
class Outcome:
    change: Change
    ok: bool
    message: str


def new_changeset(session: Session, description: str) -> Changeset:
    changeset = Changeset(description=description)
    session.add(changeset)
    return changeset


def propose_retag(changeset: Changeset, file: AudioFile, fields: dict[str, str | None]) -> Change:
    unknown = set(fields) - set(tags.FIELDS)
    if unknown:
        raise ChangeError(f"Not tag fields: {', '.join(sorted(unknown))}")
    change = Change(file=file, kind="retag", payload={"fields": fields}, status="proposed")
    changeset.changes.append(change)
    return change


def propose_move(changeset: Changeset, file: AudioFile, to: str) -> Change:
    change = Change(file=file, kind="move", payload={"to": Path(to).as_posix()}, status="proposed")
    changeset.changes.append(change)
    return change


def propose_tag_sync(changeset: Changeset, file: AudioFile) -> Change | None:
    """Propose writing the resolved values (e.g. manual edits) into the file's tags,
    for the fields where they differ from what the tags now say."""
    resolved = resolve(file.values)
    current = {fv.field: fv.value for fv in file.values if fv.source == "tag"}
    fields: dict[str, str | None] = {}
    for name in tags.FIELDS:
        if name in resolved and resolved[name].value != current.get(name):
            fields[name] = resolved[name].value
    return propose_retag(changeset, file, fields) if fields else None


def set_status(changes: list[Change], status: str) -> int:
    """Approve or reject proposed changes (or return approved/rejected ones to proposed)."""
    if status not in ("approved", "rejected", "proposed"):
        raise ValueError(status)
    count = 0
    for change in changes:
        if change.status in ("proposed", "approved", "rejected") and change.status != status:
            change.status = status
            count += 1
    return count


def describe(change: Change) -> str:
    if change.kind == "move":
        return f"move {change.file.path} -> {change.payload['to']}"
    current = {fv.field: fv.value for fv in change.file.values if fv.source == "tag"}
    if change.status == "applied" and change.undo:
        current = change.undo.get("fields", {})
    parts = [
        f"{name}: {current.get(name)!r} -> {value!r}"
        for name, value in change.payload["fields"].items()
    ]
    return f"retag {change.file.path}: " + "; ".join(parts)


def apply(session: Session, changeset: Changeset, *, dry_run: bool = False) -> list[Outcome]:
    """Apply the approved changes of a changeset. Each change is committed on its own,
    so a failure leaves the earlier ones applied (and undoable)."""
    outcomes = []
    for change in changeset.changes:
        if change.status != "approved":
            continue
        if dry_run:
            outcomes.append(Outcome(change, True, "would " + describe(change)))
            continue
        try:
            _check_unchanged(change.file)
            if change.kind == "retag":
                change.undo = _retag(session, change.file, change.payload["fields"])
            elif change.kind == "move":
                change.undo = _move(session, change.file, change.payload["to"])
            else:
                raise ChangeError(f"Unknown change kind {change.kind}")
            change.status, change.error, change.applied_at = "applied", None, utcnow()
            outcomes.append(Outcome(change, True, describe(change)))
        except (ChangeError, OSError, tags.UnsupportedFormat) as e:
            change.status, change.error = "failed", str(e)
            outcomes.append(Outcome(change, False, f"{describe(change)}: FAILED: {e}"))
        session.commit()
    return outcomes


def undo(session: Session, changeset: Changeset, *, dry_run: bool = False) -> list[Outcome]:
    """Revert the applied changes of a changeset, newest first."""
    outcomes = []
    for change in reversed(changeset.changes):
        if change.status != "applied":
            continue
        if dry_run:
            outcomes.append(Outcome(change, True, "would undo " + describe(change)))
            continue
        try:
            _check_unchanged(change.file)
            if change.kind == "retag":
                _retag(session, change.file, change.undo["fields"])
            else:
                _move(session, change.file, change.undo["from"])
            change.status = "undone"
            outcomes.append(Outcome(change, True, "undid " + describe(change)))
        except (ChangeError, OSError, tags.UnsupportedFormat) as e:
            change.error = f"undo failed: {e}"
            outcomes.append(Outcome(change, False, f"undo {describe(change)}: FAILED: {e}"))
        session.commit()
    return outcomes


def _full_path(file: AudioFile) -> Path:
    return Path(file.library.root) / file.path


def _check_unchanged(file: AudioFile) -> None:
    path = _full_path(file)
    try:
        st = path.stat()
    except FileNotFoundError:
        raise ChangeError(f"{file.path} no longer exists; rescan the library") from None
    if st.st_size != file.size or st.st_mtime_ns != file.mtime_ns:
        raise ChangeError(f"{file.path} has changed since it was scanned; rescan the library")


def _refresh(session: Session, file: AudioFile) -> None:
    path = _full_path(file)
    st = path.stat()
    file.size, file.mtime_ns, file.scanned_at = st.st_size, st.st_mtime_ns, utcnow()
    set_values(session, file, "tag", tags.read(path).fields)
    set_values(session, file, "path", pathparse.parse(file.path))


def _retag(session: Session, file: AudioFile, fields: dict[str, str | None]) -> dict:
    path = _full_path(file)
    before = tags.read(path).fields
    tags.write(path, fields)
    _refresh(session, file)
    return {"fields": {name: before.get(name) for name in fields}}


def _move(session: Session, file: AudioFile, to: str) -> dict:
    source, target = _full_path(file), Path(file.library.root) / to
    if target.exists():
        raise ChangeError(f"{to} already exists")
    clash = session.scalar(
        select(AudioFile.id).where(
            AudioFile.library_id == file.library_id, AudioFile.path == to, AudioFile.id != file.id
        )
    )
    if clash:
        raise ChangeError(f"{to} is still in the catalog as a missing file; rescan first")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(source, target)
    _remove_empty_dirs(source.parent, Path(file.library.root))
    old_path = file.path
    file.path = Path(to).as_posix()
    _refresh(session, file)
    return {"from": old_path}


def _remove_empty_dirs(directory: Path, root: Path) -> None:
    """Remove now-empty folders up to (not including) the library root. A folder that
    only holds macOS/Windows litter (.DS_Store, desktop.ini) counts as non-empty."""
    while directory != root and root in directory.parents:
        try:
            os.rmdir(directory)
        except OSError:
            return
        directory = directory.parent
