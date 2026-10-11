"""Choosing the effective value of each field from the candidates of different sources."""

from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from .models import AudioFile, EffectiveMetadata, FieldValue, PendingField
from .pathparse import normalize_name
from .tags import FIELDS, NUMBER_FIELDS, parse_number, parse_year

# Higher wins. A manual edit always wins and is never overwritten by scans.
SOURCE_PRIORITY = {
    "manual": 100,
    "analysis": 55,  # measured from the audio (ReplayGain): newer than what tags say
    "tag": 50,
    "gnudb": 48,  # a lookup by durations can be wrong: fills gaps, tags win when they differ
    "rule": 45,  # derived by a rule the user set up (stars from MusicMatch preference)
    "neighbours": 40,
    "path": 30,
}
EFFECTIVE_COLUMNS = set(EffectiveMetadata.__table__.columns.keys())

# Sources whose values are meant to end up in the files: the user's edits, measurements,
# lookups and rules. Values inferred from paths or neighbouring files are only evidence.
WRITTEN_SOURCES = ("manual", "analysis", "gnudb", "rule")


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


def unwritten(
    values: Iterable, prefer: Iterable[str] = (), only_preferred: bool = False
) -> dict[str, str]:
    """The fields whose chosen value is an edit, a measurement or a lookup that differs
    from what the file's tags say: values not yet written into the file. values are the
    file's FieldValues (or rows with field, source and value).

    A lookup ranks below the tags, so where it differs it is not the chosen value. With
    prefer, naming such sources (e.g. "gnudb"), their values are taken instead of the
    tags' (not instead of the user's edits, and not where they only spell a number or a
    date differently). only_preferred leaves out the rest, such as measurements: what is
    left is what accepting those sources' values would take."""
    values = list(values)
    resolved = resolve(values)
    current = {fv.field: fv.value for fv in values if fv.source == "tag"}
    result = {}
    for name in FIELDS:
        found = resolved.get(name)
        if found is None:
            continue
        preferred = next((found.candidates[s] for s in prefer if s in found.candidates), None)
        if found.source in WRITTEN_SOURCES:
            value = found.value
        elif preferred is not None and found.source != "manual":
            value = preferred
            if name in current and comparable(name, value) == comparable(name, current[name]):
                continue
        else:
            continue
        if only_preferred and (found.source == "manual" or value != preferred):
            continue
        if value != current.get(name):
            result[name] = value
    return result


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
    """Recompute the EffectiveMetadata and PendingField rows of the given files from their
    FieldValues. The rows are replaced in bulk: merging them one by one costs a query and
    a flush per file, which dominated operations on thousands of files."""
    session.flush()
    session.execute(delete(PendingField).where(PendingField.file_id.in_(file_ids)))
    session.execute(delete(EffectiveMetadata).where(EffectiveMetadata.file_id.in_(file_ids)))
    effective_rows, pending_rows = [], []
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
            r.source != "manual" and len({comparable(name, v) for v in r.candidates.values()}) > 1
            for name, r in resolved.items()
        )
        effective: dict[str, object] = {
            name: (resolved[name].value if name in resolved else None)
            for name in FIELDS
            if name in EFFECTIVE_COLUMNS  # the table holds the fields used in searches
        }
        for name in NUMBER_FIELDS:
            effective[name] = parse_number(effective[name])
        effective["year"] = parse_year(effective["date"])
        effective_rows.append({"file_id": file_id, "disputed": disputed, **effective})
        tag = {row.field: row.value for row in values if row.source == "tag"}
        pending_rows.extend(
            {"file_id": file_id, "field": name, "value": value,
             "source": resolved[name].source, "tag": tag.get(name)}
            for name, value in unwritten(values).items()
        )  # fmt: skip
    if effective_rows:
        session.execute(insert(EffectiveMetadata), effective_rows)
    if pending_rows:
        session.execute(insert(PendingField), pending_rows)


def comparable(field: str, value: str) -> object:
    """What has to differ between two values of a field for them to disagree: the number
    of track and disc numbers ('02.' and '2' agree), the year of dates, and otherwise the
    name, ignoring case, accents, punctuation and a leading 'The'."""
    if field in NUMBER_FIELDS:
        number = parse_number(value)
        return number if number is not None else normalize_name(value)
    if field == "date":
        year = parse_year(value)
        return year if year is not None else normalize_name(value)
    return normalize_name(value)


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
