"""Pending changes: values the catalog has decided on (edits, measurements, lookups, rules)
that the files do not hold yet.

Editing only changes the catalog, as an operation that can be undone (operations.py).
The files are written later, for a selection (an artist, an album, a folder, chosen
files), each file once with all its pending values, however many edits led to them. The
diff of the selection is the review; writing it makes a changeset that is applied at
once and can be undone (the edits stay, so the files are pending again).

A lookup (gnudb) ranks below the tags, so where they differ it is only evidence; the
user can accept its values for a selection, which makes them edits."""

from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from . import changes as ch
from . import operations as ops
from . import tags
from .models import AudioFile, Change, Changeset, EffectiveMetadata, FieldValue, Library
from .models import PendingField as P
from .provenance import resolve, unwritten
from .tracks import present

M = EffectiveMetadata
# Pending changes are grouped by album artist, or artist where there is none, and album
ARTIST = func.coalesce(func.nullif(M.albumartist, ""), func.nullif(M.artist, ""), "")
ALBUM = func.coalesce(M.album, "")


@dataclass
class Selection:
    """Which pending changes to look at or write. Empty: all of them."""

    path: str | None = None  # files under this path
    artist: str | None = None  # album artist, or artist where there is none ("" for none)
    album: str | None = None  # "" for tracks without an album
    file_ids: list[int] | None = None
    only: list[str] | None = None  # field names, or their beginnings ("replaygain")
    # Sources whose values replace a tag that differs from them ("gnudb"); see unwritten
    prefer: tuple[str, ...] = ()


@dataclass
class Pending:
    file: AudioFile
    fields: dict[str, str]  # field -> value to write
    before: dict[str, str | None] = field(default_factory=dict)  # field -> the tag now
    artist: str = ""
    album: str = ""


@dataclass
class Group:
    artist: str
    album: str
    files: int


@dataclass
class Survey:
    files: list[Pending] = field(default_factory=list)  # the first ones, up to a limit
    total: int = 0  # files with pending changes in the selection
    by_field: Counter = field(default_factory=Counter)  # field -> files
    waiting: int = 0  # files with changes waiting for review in a changeset


def matches(name: str, only: list[str] | None) -> bool:
    """Whether a field is among the chosen ones; a prefix such as "replaygain" chooses
    all ReplayGain fields."""
    return not only or any(name == o or name.startswith(o) for o in only)


def _field_condition(only: list[str] | None):
    if not only:
        return True
    return or_(*(P.field == o for o in only), *(P.field.startswith(o) for o in only))


def _waiting():
    """Files with proposed or approved changes: written by applying those changesets."""
    return select(Change.file_id).where(
        Change.status.in_(("proposed", "approved")), Change.file_id.is_not(None)
    )


def _selected(library: Library, selection: Selection) -> list:
    """Conditions on AudioFile and EffectiveMetadata choosing the selection's files."""
    conditions = [present(library.id)]
    if selection.path:
        conditions.append(AudioFile.path.startswith(selection.path, autoescape=True))
    if selection.artist is not None:
        conditions.append(func.lower(ARTIST) == selection.artist.lower())
    if selection.album is not None:
        conditions.append(func.lower(ALBUM) == selection.album.lower())
    if selection.file_ids is not None:
        conditions.append(AudioFile.id.in_(selection.file_ids))
    return conditions


def _candidates(library: Library, selection: Selection):
    """The ids of the selection's files that may have pending changes: those with a
    PendingField of the chosen fields, and with prefer also those with a value from a
    preferred source (whether it is to replace the tag is decided per file)."""
    ids = select(P.file_id).where(_field_condition(selection.only))
    if selection.prefer:
        ids = ids.union(
            select(FieldValue.file_id).where(
                FieldValue.source.in_(selection.prefer),
                or_(*(FieldValue.field.startswith(o) for o in selection.only or [""])),
            )
        )
    return (
        select(AudioFile.id)
        .join(M, M.file_id == AudioFile.id)
        .where(*_selected(library, selection), AudioFile.id.in_(ids))
    )


def survey(session: Session, library: Library, selection: Selection, limit: int = 100) -> Survey:
    """The selection's files with values not yet in their tags, the first `limit` of them
    in artist, album and path order with what would change in each.

    Without prefer, the counts come from the PendingField table alone. With prefer, every
    candidate is looked at, as which of its preferred values differ from the tags (beyond
    spelling) is not stored."""
    result = Survey()
    candidates = _candidates(library, selection).where(AudioFile.id.not_in(_waiting()))
    result.waiting = session.scalar(
        select(func.count()).select_from(
            _candidates(library, selection).where(AudioFile.id.in_(_waiting())).subquery()
        )
    )
    ordered = (
        select(AudioFile.id, ARTIST, ALBUM)
        .join(M, M.file_id == AudioFile.id)
        .where(AudioFile.id.in_(candidates))
        .order_by(func.lower(ARTIST), func.lower(ALBUM), AudioFile.path)
    )
    if not selection.prefer:
        result.total = session.scalar(select(func.count()).select_from(candidates.subquery()))
        result.by_field.update(
            dict(
                session.execute(
                    select(P.field, func.count())
                    .where(P.file_id.in_(candidates), _field_condition(selection.only))
                    .group_by(P.field)
                ).all()
            )
        )
        ordered = ordered.limit(limit)
    for batch in _batches(session.execute(ordered).all()):
        files = session.scalars(
            select(AudioFile)
            .where(AudioFile.id.in_([row.id for row in batch]))
            .options(selectinload(AudioFile.values))
        ).all()
        by_id = {file.id: file for file in files}
        for row in batch:
            file = by_id[row.id]
            fields = {
                k: v for k, v in unwritten(file.values, selection.prefer).items()
                if matches(k, selection.only)
            }  # fmt: skip
            if not fields:
                continue
            if selection.prefer:
                result.total += 1
                result.by_field.update(fields.keys())
                if len(result.files) >= limit:
                    continue
            tags_now = {fv.field: fv.value for fv in file.values if fv.source == "tag"}
            result.files.append(
                Pending(file, fields, {k: tags_now.get(k) for k in fields}, row[1], row[2])
            )
    return result


def _batches(rows: list, size: int = 500) -> list[list]:
    return [rows[start : start + size] for start in range(0, len(rows), size)]


def groups(session: Session, library: Library, selection: Selection) -> list[Group]:
    """The albums with pending changes in the selection, and how many files each has,
    in artist and album order. Files waiting for review in a changeset are left out.
    Without prefer only: with it, see survey."""
    candidates = _candidates(library, selection).where(AudioFile.id.not_in(_waiting()))
    rows = session.execute(
        select(ARTIST, ALBUM, func.count())
        .join(AudioFile, AudioFile.id == M.file_id)
        .where(M.file_id.in_(candidates))
        .group_by(func.lower(ARTIST), func.lower(ALBUM))
        .order_by(func.lower(ARTIST), func.lower(ALBUM))
    )
    return [Group(artist, album, count) for artist, album, count in rows]


def group_of(file: AudioFile) -> tuple[str, str]:
    """The artist and album a file's pending changes are grouped under."""
    resolved = resolve(file.values)
    get = lambda name: resolved[name].value if name in resolved else ""  # noqa: E731
    return get("albumartist") or get("artist"), get("album")


def store_edit(
    session: Session, files: list[AudioFile], values: dict[str, str | None], description: str
) -> list[AudioFile]:
    """Store the user's values for these files (None removes one, falling back to tags
    and paths), as an operation that can be undone. Nothing is written to the files: the
    files whose tags now differ are returned, and are pending until written."""
    unknown = set(values) - set(tags.FIELDS)
    if unknown:
        raise ValueError(f"Not tag fields: {', '.join(sorted(unknown))}")
    if files:
        ops.record(
            session, files[0].library_id, "edit", description,
            (ops.Value(file.id, "manual", name, value)
             for file in files for name, value in values.items()),
        )  # fmt: skip
    session.commit()
    return [file for file in files if set(unwritten(file.values)) & set(values)]


def write(
    session: Session, files: list[Pending], description: str, id3_version: int | None = None
) -> tuple[Changeset, list[ch.Outcome]]:
    """Write each file's pending values into its tags, in one change per file, and keep
    them as a changeset that can be undone."""
    changeset = ch.new_changeset(session, description, "writing")
    for pending in files:
        change = ch.propose_retag(changeset, pending.file, dict(pending.fields), id3_version)
        change.status = "approved"
    session.commit()
    return changeset, ch.apply(session, changeset)


def discard(session: Session, files: list[Pending]) -> tuple[int, int]:
    """Drop the user's edits among these pending values, so that the tags' values count
    again, as an operation that can be undone. Measurements, lookups and rules are kept
    (they are not the user's edits). Returns how many values were dropped and how many
    kept."""
    dropped, kept = [], 0
    for pending in files:
        resolved = resolve(pending.file.values)
        for name in pending.fields:
            if name in resolved and resolved[name].source == "manual":
                dropped.append(ops.Value(pending.file.id, "manual", name, None))
            else:
                kept += 1
    if dropped:
        count = len({value.file_id for value in dropped})
        ops.record(
            session, files[0].file.library_id, "discard",
            f"Discard the edits of {count} file{'s' if count != 1 else ''}", dropped,
        )  # fmt: skip
    session.commit()
    return len(dropped), kept


def accept(session: Session, files: list[Pending], source: str, description: str) -> int:
    """Take a lookup's values among these pending values (surveyed with prefer=(source,))
    as the user's own, as an operation that can be undone. They are then pending like any
    edit, whatever the tags say. Returns how many values were taken."""
    taken = []
    for pending in files:
        resolved = resolve(pending.file.values)
        for name, value in pending.fields.items():
            found = resolved.get(name)
            if found and found.source != "manual" and found.candidates.get(source) == value:
                taken.append(ops.Value(pending.file.id, "manual", name, value))
    if taken:
        ops.record(session, files[0].file.library_id, "accept", description, taken)
    session.commit()
    return len(taken)


def description(selection: Selection, count: int, accept: str | None = None) -> str:
    """A description naming what was written (a changeset), or with accept, whose values
    were accepted for what (an operation)."""
    what = []
    if selection.artist is not None:
        what.append(selection.artist or "no artist")
    if selection.album is not None:
        what.append(selection.album or "no album")
    if selection.path:
        what.append(selection.path)
    files = f"{count} file{'s' if count != 1 else ''}"
    where = f" ({' – '.join(what)})" if what else ""
    if accept:
        return f"Accept {accept}'s values for {files}{where}"
    return f"Write the tags of {files}{where}"
