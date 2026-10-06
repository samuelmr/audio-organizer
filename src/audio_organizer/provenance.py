"""Choosing the effective value of each field from the candidates of different sources."""

from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import AudioFile, EffectiveMetadata, FieldValue
from .pathparse import normalize_name
from .tags import FIELDS

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
    refresh_effective(session, [file.id])


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
    refresh_effective(session, [file.id])


def refresh_effective(session: Session, file_ids: list[int]) -> None:
    """Recompute the EffectiveMetadata rows of the given files from their FieldValues."""
    session.flush()
    rows: dict[int, list] = {file_id: [] for file_id in file_ids}
    for row in session.execute(
        select(FieldValue.file_id, FieldValue.field, FieldValue.source, FieldValue.value).where(
            FieldValue.file_id.in_(file_ids)
        )
    ):
        rows[row.file_id].append(row)
    for file_id, values in rows.items():
        resolved = {k: r for k, r in resolve(values).items() if k in FIELDS}
        # A field the user has decided (a manual value) is no longer disputed
        disputed = any(
            r.source != "manual" and len({normalize_name(v) for v in r.candidates.values()}) > 1
            for r in resolved.values()
        )
        session.merge(
            EffectiveMetadata(
                file_id=file_id,
                disputed=disputed,
                **{name: (resolved[name].value if name in resolved else None) for name in FIELDS},
            )
        )


def backfill_effective(session: Session, batch: int = 2000) -> int:
    """Create the missing EffectiveMetadata rows, e.g. for a catalog made before they
    existed. Returns how many were created."""
    missing = session.scalars(
        select(AudioFile.id).where(~AudioFile.id.in_(select(EffectiveMetadata.file_id)))
    ).all()
    for start in range(0, len(missing), batch):
        refresh_effective(session, list(missing[start : start + batch]))
        session.commit()
    return len(missing)
