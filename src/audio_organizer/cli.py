"""Command line interface: aorg <command>."""

import time
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import changes as ch
from .db import make_sessionmaker
from .models import AudioFile, Change, Changeset, Library
from .provenance import resolve, set_value
from .scanner import ScanStats, scan
from .tags import FIELDS

app = typer.Typer(no_args_is_help=True, help="Maintain a music collection.")
library_app = typer.Typer(no_args_is_help=True, help="Manage libraries (collection roots).")
changes_app = typer.Typer(no_args_is_help=True, help="Review, apply and undo changesets.")
app.add_typer(library_app, name="library")
app.add_typer(changes_app, name="changes")


@app.callback()
def main(
    ctx: typer.Context,
    db: Annotated[
        str | None,
        typer.Option(help="Catalog database path or URL [default: $AORG_DB or ~/.local/share]"),
    ] = None,
) -> None:
    ctx.obj = make_sessionmaker(db if db is None or "://" in db else f"sqlite:///{db}")


def _session(ctx: typer.Context) -> Session:
    return ctx.obj()


def _library(session: Session, name: str | None) -> Library:
    libraries = session.scalars(select(Library).order_by(Library.name)).all()
    if name is None:
        if len(libraries) == 1:
            return libraries[0]
        raise typer.BadParameter(
            "Name the library: " + ", ".join(lib.name for lib in libraries)
            if libraries
            else "No libraries yet; add one with `aorg library add`"
        )
    for library in libraries:
        if library.name == name:
            return library
    raise typer.BadParameter(f"No library named {name!r}")


def _find_file(session: Session, path: str) -> AudioFile:
    """Find a file by absolute path, path relative to the working directory, or path
    relative to a library root."""
    absolute = Path(path).expanduser().resolve()
    for library in session.scalars(select(Library)):
        root = Path(library.root).resolve()
        if absolute.is_relative_to(root):
            relpath = absolute.relative_to(root).as_posix()
            file = session.scalar(
                select(AudioFile).where(
                    AudioFile.library_id == library.id, AudioFile.path == relpath
                )
            )
            if file:
                return file
    matches = session.scalars(select(AudioFile).where(AudioFile.path == path)).all()
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise typer.BadParameter(f"{path} is in several libraries; give the absolute path")
    raise typer.BadParameter(f"{path} is not in the catalog (scan the library first?)")


# ---- libraries -------------------------------------------------------------------------


@library_app.command("add")
def library_add(ctx: typer.Context, name: str, root: Path) -> None:
    """Register a collection root folder under a name."""
    if not root.is_dir():
        raise typer.BadParameter(f"{root} is not a folder")
    with _session(ctx) as session:
        session.add(Library(name=name, root=str(root.expanduser().resolve())))
        session.commit()
    typer.echo(f"Added library {name}: {root}")


@library_app.command("list")
def library_list(ctx: typer.Context) -> None:
    with _session(ctx) as session:
        rows = session.execute(
            select(Library.name, Library.root, func.count(AudioFile.id))
            .outerjoin(AudioFile)
            .group_by(Library.id)
            .order_by(Library.name)
        )
        for name, root, count in rows:
            typer.echo(f"{name}\t{count} files\t{root}")


# ---- scanning and inspection -----------------------------------------------------------


@app.command("scan")
def scan_command(
    ctx: typer.Context,
    library: Annotated[str | None, typer.Argument(help="Library name")] = None,
    hash_audio: Annotated[bool, typer.Option("--hash/--no-hash", help="Hash audio data")] = True,
    workers: Annotated[int, typer.Option(help="Parallel file readers")] = 8,
) -> None:
    """Catalog new and changed files. Unchanged files are skipped."""
    started = time.monotonic()
    last_report = 0.0

    def progress(stats: ScanStats, relpath: str) -> None:
        nonlocal last_report
        now = time.monotonic()
        if now - last_report >= 2:
            last_report = now
            typer.echo(
                f"  {stats.seen} seen, {stats.new + stats.changed} read: {relpath}", err=True
            )

    with _session(ctx) as session:
        lib = _library(session, library)
        stats = scan(session, lib, hash_audio=hash_audio, workers=workers, progress=progress)
    typer.echo(
        f"{stats.seen} files: {stats.new} new, {stats.changed} changed, {stats.moved} moved, "
        f"{stats.unchanged} unchanged, {stats.missing} missing, {len(stats.errors)} errors "
        f"({time.monotonic() - started:.0f} s)"
    )
    for relpath, error in stats.errors[:20]:
        typer.echo(f"  {relpath}: {error}")
    if len(stats.errors) > 20:
        typer.echo(f"  ... and {len(stats.errors) - 20} more")


@app.command("stats")
def stats_command(
    ctx: typer.Context, library: Annotated[str | None, typer.Argument()] = None
) -> None:
    """Summarize the catalog of a library."""
    with _session(ctx) as session:
        lib = _library(session, library)
        present = (AudioFile.library_id == lib.id) & ~AudioFile.missing
        total, hours, gigabytes = session.execute(
            select(
                func.count(), func.sum(AudioFile.duration) / 3600, func.sum(AudioFile.size) / 1e9
            ).where(present)
        ).one()
        typer.echo(f"{lib.name}: {total} files, {hours or 0:.0f} hours, {gigabytes or 0:.1f} GB")
        for fmt, count in session.execute(
            select(AudioFile.format, func.count())
            .where(present)
            .group_by(AudioFile.format)
            .order_by(func.count().desc())
        ):
            typer.echo(f"  {fmt or 'unreadable'}: {count}")
        missing = session.scalar(
            select(func.count()).where(AudioFile.library_id == lib.id, AudioFile.missing)
        )
        errors = session.scalar(select(func.count()).where(present, AudioFile.error.is_not(None)))
        dupes = (
            select(AudioFile.audio_hash, func.count().label("n"))
            .where(present, AudioFile.audio_hash.is_not(None))
            .group_by(AudioFile.audio_hash)
            .having(func.count() > 1)
            .subquery()
        )
        groups, copies = session.execute(select(func.count(), func.sum(dupes.c.n))).one()
        typer.echo(f"  missing: {missing}, unreadable: {errors}")
        typer.echo(f"  identical audio: {groups} groups, {copies or 0} files")


@app.command("show")
def show(ctx: typer.Context, path: str) -> None:
    """Show a file's metadata: the effective value of each field and where it came from."""
    with _session(ctx) as session:
        file = _find_file(session, path)
        typer.echo(f"{file.library.name}: {file.path}")
        duration = f"{file.duration:.1f} s" if file.duration else "?"
        typer.echo(
            f"  {file.format}, {duration}, {file.bitrate or '?'} bps, hash {file.audio_hash}"
        )
        if file.error:
            typer.echo(f"  error: {file.error}")
        for name, r in sorted(resolve(file.values).items()):
            others = {s: v for s, v in r.candidates.items() if s != r.source and v != r.value}
            alt = "   (" + ", ".join(f"{s}: {v}" for s, v in others.items()) + ")" if others else ""
            typer.echo(f"  {name:12} {r.value}  [{r.source}]{alt}")


@app.command("edit")
def edit(
    ctx: typer.Context,
    path: str,
    set_: Annotated[list[str], typer.Option("--set", help="field=value")] = [],  # noqa: B006
    unset: Annotated[list[str], typer.Option(help="Field whose manual value to remove")] = [],  # noqa: B006
) -> None:
    """Edit metadata manually. The edit is stored in the catalog and proposed as a
    changeset that writes it into the file's tags."""
    with _session(ctx) as session:
        file = _find_file(session, path)
        for item in set_:
            name, sep, value = item.partition("=")
            if not sep or name not in FIELDS:
                raise typer.BadParameter(f"--set takes field=value; fields: {', '.join(FIELDS)}")
            set_value(session, file, "manual", name, value.strip())
        for name in unset:
            set_value(session, file, "manual", name, None)
        changeset = ch.new_changeset(session, f"Manual edit of {file.path}")
        change = ch.propose_tag_sync(changeset, file)
        if change is None:
            session.expunge(changeset)
            session.commit()
            typer.echo("Saved; the file's tags already match.")
            return
        session.commit()
        typer.echo(f"Changeset {changeset.id}: {ch.describe(change)}")
        typer.echo(f"Review with `aorg changes show {changeset.id}`, then approve and apply.")


# ---- changesets --------------------------------------------------------------------------


def _changeset(session: Session, changeset_id: int) -> Changeset:
    changeset = session.get(Changeset, changeset_id)
    if changeset is None:
        raise typer.BadParameter(f"No changeset {changeset_id}")
    return changeset


@changes_app.command("list")
def changes_list(
    ctx: typer.Context,
    all_: Annotated[bool, typer.Option("--all", help="Include done ones")] = False,
) -> None:
    with _session(ctx) as session:
        for changeset in session.scalars(select(Changeset).order_by(Changeset.id)):
            counts: dict[str, int] = {}
            for change in changeset.changes:
                counts[change.status] = counts.get(change.status, 0) + 1
            if not all_ and not ({"proposed", "approved", "failed"} & counts.keys()):
                continue
            summary = ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
            typer.echo(f"{changeset.id}\t{changeset.description}\t{summary}")


@changes_app.command("show")
def changes_show(ctx: typer.Context, changeset_id: int) -> None:
    with _session(ctx) as session:
        changeset = _changeset(session, changeset_id)
        typer.echo(f"Changeset {changeset.id}: {changeset.description}")
        for change in changeset.changes:
            error = f"  ({change.error})" if change.error else ""
            typer.echo(f"  #{change.id} [{change.status}] {ch.describe(change)}{error}")


def _select_changes(changeset: Changeset, change_ids: list[int] | None) -> list[Change]:
    if not change_ids:
        return list(changeset.changes)
    selected = [c for c in changeset.changes if c.id in change_ids]
    if len(selected) != len(set(change_ids)):
        raise typer.BadParameter("Some change ids are not in this changeset")
    return selected


@changes_app.command("approve")
def changes_approve(
    ctx: typer.Context,
    changeset_id: int,
    change_ids: Annotated[
        list[int] | None, typer.Argument(help="Changes to approve [default: all]")
    ] = None,
) -> None:
    with _session(ctx) as session:
        changeset = _changeset(session, changeset_id)
        count = ch.set_status(_select_changes(changeset, change_ids), "approved")
        session.commit()
    typer.echo(f"Approved {count} changes")


@changes_app.command("reject")
def changes_reject(
    ctx: typer.Context,
    changeset_id: int,
    change_ids: Annotated[
        list[int] | None, typer.Argument(help="Changes to reject [default: all]")
    ] = None,
) -> None:
    with _session(ctx) as session:
        changeset = _changeset(session, changeset_id)
        count = ch.set_status(_select_changes(changeset, change_ids), "rejected")
        session.commit()
    typer.echo(f"Rejected {count} changes")


def _report(outcomes: list[ch.Outcome]) -> None:
    for outcome in outcomes:
        typer.echo(("  " if outcome.ok else "! ") + outcome.message)
    if not outcomes:
        typer.echo("Nothing to do")
    if any(not o.ok for o in outcomes):
        raise typer.Exit(1)


@changes_app.command("apply")
def changes_apply(
    ctx: typer.Context,
    changeset_id: int,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Only show what would be done")
    ] = False,
) -> None:
    """Apply the approved changes of a changeset."""
    with _session(ctx) as session:
        _report(ch.apply(session, _changeset(session, changeset_id), dry_run=dry_run))


@changes_app.command("undo")
def changes_undo(
    ctx: typer.Context,
    changeset_id: int,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Only show what would be done")
    ] = False,
) -> None:
    """Revert the applied changes of a changeset."""
    with _session(ctx) as session:
        _report(ch.undo(session, _changeset(session, changeset_id), dry_run=dry_run))
