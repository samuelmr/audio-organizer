"""Choosing the effective value of each field from the candidates of different sources."""

from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import delete
from sqlalchemy.orm import Session

from .models import AudioFile, FieldValue

# Higher wins. A manual edit always wins and is never overwritten by scans.
SOURCE_PRIORITY = {"manual": 100, "gnudb": 60, "tag": 50, "neighbours": 40, "path": 30}


@dataclass
class Resolved:
    value: str
    source: str
    candidates: dict[str, str] = field(default_factory=dict)  # source -> value

    @property
    def disputed(self) -> bool:
        return len(set(self.candidates.values())) > 1


def resolve(values: Iterable[FieldValue]) -> dict[str, Resolved]:
    by_field: dict[str, dict[str, str]] = {}
    for fv in values:
        by_field.setdefault(fv.field, {})[fv.source] = fv.value
    resolved = {}
    for name, candidates in by_field.items():
        source = max(candidates, key=lambda s: SOURCE_PRIORITY.get(s, 0))
        resolved[name] = Resolved(candidates[source], source, candidates)
    return resolved


def set_values(session: Session, file: AudioFile, source: str, fields: dict[str, str]) -> None:
    """Replace all values from one source for a file."""
    session.flush()  # a new file needs its id
    session.execute(
        delete(FieldValue).where(FieldValue.file_id == file.id, FieldValue.source == source)
    )
    session.add_all(
        FieldValue(file_id=file.id, field=name, value=value, source=source)
        for name, value in fields.items()
    )
    session.expire(file, ["values"])


def set_value(session: Session, file: AudioFile, source: str, name: str, value: str | None) -> None:
    """Set (or with None, remove) one field's value from one source."""
    session.execute(
        delete(FieldValue).where(
            FieldValue.file_id == file.id, FieldValue.source == source, FieldValue.field == name
        )
    )
    if value is not None:
        session.add(FieldValue(file_id=file.id, field=name, value=value, source=source))
    session.expire(file, ["values"])
