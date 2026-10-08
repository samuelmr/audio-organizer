"""Writing values that are not yet in the files (edits, measurements, lookups) in bulk:
for files under a path, optionally only some fields, in batches of changesets."""

from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import changes as ch
from .models import AudioFile, Change, Changeset, FieldValue, Library
from .tracks import present


@dataclass
class Pending:
    file: AudioFile
    fields: dict[str, str]  # field -> value to write


@dataclass
class Survey:
    files: list[Pending] = field(default_factory=list)
    by_field: Counter = field(default_factory=Counter)  # field -> files
    waiting: int = 0  # files with changes waiting for review


def matches(name: str, only: list[str] | None) -> bool:
    """Whether a field is among the chosen ones; a prefix such as "replaygain" chooses
    all ReplayGain fields."""
    return not only or any(name == o or name.startswith(o) for o in only)


def survey(
    session: Session,
    library: Library,
    *,
    path_prefix: str | None = None,
    only: list[str] | None = None,
) -> Survey:
    result = Survey()
    candidates = select(FieldValue.file_id).where(FieldValue.source.in_(ch.WRITTEN_SOURCES))
    query = (
        select(AudioFile)
        .where(present(library.id), AudioFile.id.in_(candidates))
        .options(selectinload(AudioFile.values))
        .order_by(AudioFile.path)
    )
    if path_prefix:
        query = query.where(AudioFile.path.startswith(path_prefix, autoescape=True))
    waiting = set(
        session.scalars(select(Change.file_id).where(Change.status.in_(("proposed", "approved"))))
    )
    for file in session.scalars(query):
        fields = {k: v for k, v in ch.unwritten(file).items() if matches(k, only)}
        if not fields:
            continue
        if file.id in waiting:
            result.waiting += 1
            continue
        result.files.append(Pending(file, fields))
        result.by_field.update(fields.keys())
    return result


def propose(
    session: Session, files: list[Pending], description: str, id3_version: int | None = None
) -> Changeset:
    changeset = ch.new_changeset(session, description)
    for pending in files:
        ch.propose_retag(changeset, pending.file, dict(pending.fields), id3_version)
    session.commit()
    return changeset
