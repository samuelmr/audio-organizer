"""Catalog schema.

Tags are evidence, not truth: every candidate value of a metadata field is stored as a
FieldValue with its source (file tag, path, manual edit, gnudb, ...), and the effective
value is resolved from them (see provenance.py). Files are identified across moves and
retags by a hash of the audio data alone (see audiohash.py). Every modification to files
goes through a Changeset of reviewable Changes that record how to undo them.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, datetime: DateTime(timezone=True)}


class Library(Base):
    """A collection root. Paths of its files are stored relative to it."""

    __tablename__ = "library"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    root: Mapped[str] = mapped_column(Text)

    files: Mapped[list["AudioFile"]] = relationship(back_populates="library")


class AudioFile(Base):
    __tablename__ = "audio_file"
    __table_args__ = (UniqueConstraint("library_id", "path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    library_id: Mapped[int] = mapped_column(ForeignKey("library.id"), index=True)
    path: Mapped[str] = mapped_column(Text)  # POSIX path relative to the library root
    size: Mapped[int] = mapped_column(BigInteger)
    mtime_ns: Mapped[int] = mapped_column(BigInteger)
    audio_hash: Mapped[str | None] = mapped_column(String(32), index=True)
    format: Mapped[str | None] = mapped_column(String(20))
    duration: Mapped[float | None] = mapped_column(Float)
    bitrate: Mapped[int | None]
    sample_rate: Mapped[int | None]
    missing: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str | None] = mapped_column(Text)
    first_seen: Mapped[datetime] = mapped_column(default=utcnow)
    scanned_at: Mapped[datetime] = mapped_column(default=utcnow)

    library: Mapped[Library] = relationship(back_populates="files")
    values: Mapped[list["FieldValue"]] = relationship(
        back_populates="file", cascade="all, delete-orphan", passive_deletes=True
    )
    snapshots: Mapped[list["TagSnapshot"]] = relationship(
        back_populates="file", cascade="all, delete-orphan", passive_deletes=True
    )


class FieldValue(Base):
    """One candidate value for one metadata field of a file, from one source."""

    __tablename__ = "field_value"
    __table_args__ = (UniqueConstraint("file_id", "field", "source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(
        ForeignKey("audio_file.id", ondelete="CASCADE"), index=True
    )
    field: Mapped[str] = mapped_column(String(40), index=True)
    value: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    file: Mapped[AudioFile] = relationship(back_populates="values")


class TagSnapshot(Base):
    """The tags of a file as they were at some point, e.g. when the file was first seen."""

    __tablename__ = "tag_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(
        ForeignKey("audio_file.id", ondelete="CASCADE"), index=True
    )
    taken_at: Mapped[datetime] = mapped_column(default=utcnow)
    reason: Mapped[str] = mapped_column(String(20))
    tags: Mapped[dict[str, Any]]

    file: Mapped[AudioFile] = relationship(back_populates="snapshots")


class Changeset(Base):
    """A batch of proposed changes the user reviews, applies and can undo together."""

    __tablename__ = "changeset"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    description: Mapped[str] = mapped_column(Text, default="")

    changes: Mapped[list["Change"]] = relationship(
        back_populates="changeset", cascade="all, delete-orphan", order_by="Change.id"
    )


class Change(Base):
    """One proposed modification of one file.

    kind "retag": payload {"fields": {field: new value or None to remove}}
    kind "move":  payload {"to": new relative path}
    status: proposed -> approved | rejected; approved -> applied | failed; applied -> undone
    """

    __tablename__ = "change"

    id: Mapped[int] = mapped_column(primary_key=True)
    changeset_id: Mapped[int] = mapped_column(
        ForeignKey("changeset.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[int] = mapped_column(ForeignKey("audio_file.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict[str, Any]]
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    undo: Mapped[dict[str, Any] | None]
    error: Mapped[str | None] = mapped_column(Text)
    applied_at: Mapped[datetime | None]

    changeset: Mapped[Changeset] = relationship(back_populates="changes")
    file: Mapped[AudioFile] = relationship()
