"""Command line interface: aorg <command>."""

import time
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import changes as ch
from . import playlists as pl
from . import sync as sy
from .db import make_sessionmaker
from .models import AudioFile, Change, Changeset, Library, Playlist, SyncTarget
from .provenance import resolve
from .scanner import ScanStats, scan
from .tags import FIELDS

app = typer.Typer(
    no_args_is_help=True, rich_markup_mode="markdown", help="Maintain a music collection."
)
library_app = typer.Typer(
    no_args_is_help=True, rich_markup_mode="markdown", help="Manage libraries (collection roots)."
)
changes_app = typer.Typer(
    no_args_is_help=True, rich_markup_mode="markdown", help="Review, apply and undo changesets."
)
target_app = typer.Typer(
    no_args_is_help=True, rich_markup_mode="markdown", help="Manage object storage sync targets."
)
app.add_typer(library_app, name="library")
app.add_typer(changes_app, name="changes")
playlists_app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="markdown",
    help="Smart and static playlists. See `aorg playlists create --help` for the rules.",
)
app.add_typer(target_app, name="target")
app.add_typer(playlists_app, name="playlists")


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
    retry_errors: Annotated[
        bool, typer.Option("--retry-errors", help="Read again files that could not be read")
    ] = False,
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
        stats = scan(
            session, lib, hash_audio=hash_audio, workers=workers, retry_errors=retry_errors,
            progress=progress,
        )  # fmt: skip
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
    values: dict[str, str | None] = {name: None for name in unset}
    for item in set_:
        name, sep, value = item.partition("=")
        if not sep or name not in FIELDS:
            raise typer.BadParameter(f"--set takes field=value; fields: {', '.join(FIELDS)}")
        values[name] = value.strip()
    with _session(ctx) as session:
        file = _find_file(session, path)
        changeset = ch.new_changeset(session, f"Manual edit of {file.path}")
        change = ch.propose_manual_edit(session, changeset, file, values)
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


# ---- object storage sync ---------------------------------------------------------------


def _target(session: Session, name: str) -> SyncTarget:
    target = session.scalar(select(SyncTarget).where(SyncTarget.name == name))
    if target is None:
        raise typer.BadParameter(f"No sync target named {name!r}")
    return target


@target_app.command("add")
def target_add(
    ctx: typer.Context,
    name: str,
    bucket: Annotated[str, typer.Option(help="Bucket name")],
    library: Annotated[str | None, typer.Option(help="Library to sync")] = None,
    prefix: Annotated[str, typer.Option(help="Key prefix inside the bucket")] = "",
    endpoint_url: Annotated[
        str | None, typer.Option(help="S3-compatible endpoint (not needed for AWS)")
    ] = None,
    region: Annotated[str | None, typer.Option()] = None,
    profile: Annotated[str | None, typer.Option(help="AWS credentials profile")] = None,
) -> None:
    """Add a bucket to sync a library to. Credentials come from the AWS environment
    variables, ~/.aws or --profile; they are not stored in the catalog."""
    with _session(ctx) as session:
        lib = _library(session, library)
        session.add(SyncTarget(
            name=name, library=lib, bucket=bucket, prefix=prefix.strip("/"),
            endpoint_url=endpoint_url, region=region, profile=profile,
        ))  # fmt: skip
        session.commit()
    typer.echo(f"Added target {name}: {lib.name} -> s3://{bucket}/{prefix.strip('/')}")


@target_app.command("list")
def target_list(ctx: typer.Context) -> None:
    with _session(ctx) as session:
        for target in session.scalars(select(SyncTarget).order_by(SyncTarget.name)):
            endpoint = f" at {target.endpoint_url}" if target.endpoint_url else ""
            typer.echo(
                f"{target.name}\t{target.library.name} -> "
                f"s3://{target.bucket}/{target.prefix}{endpoint}"
            )


@target_app.command("reconcile")
def target_reconcile(ctx: typer.Context, name: str) -> None:
    """Compare the catalog with the bucket's contents. Adopts objects uploaded by other
    means so they are not uploaded again; nothing is changed remotely."""
    with _session(ctx) as session:
        target = _target(session, name)
        result = sy.reconcile(session, target, sy.store_for(target))
    typer.echo(
        f"Adopted {result.adopted} existing objects, forgot {result.forgotten} that are gone, "
        f"{len(result.unknown)} objects match nothing in the library"
    )
    for key in result.unknown[:20]:
        typer.echo(f"  {key}")
    if len(result.unknown) > 20:
        typer.echo(f"  ... and {len(result.unknown) - 20} more")


@app.command("sync")
def sync_command(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Sync target")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Only show the plan")] = False,
    delete: Annotated[
        bool, typer.Option("--delete", help="Delete remote objects not in the library")
    ] = False,
    workers: Annotated[int, typer.Option(help="Parallel transfers")] = 4,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="List every action")] = False,
) -> None:
    """Upload new and changed tracks and covers; update metadata and moves server-side."""
    with _session(ctx) as session:
        target = _target(session, name)
        plan = sy.plan(session, target, delete_stale=delete)
        counts = ", ".join(f"{n} {kind}" for kind, n in sorted(plan.counts().items()))
        typer.echo(
            f"{target.name}: {counts or 'nothing to do'} ({plan.unchanged} up to date, "
            f"{plan.upload_bytes / 1e6:.0f} MB to upload)"
        )
        if plan.stale:
            typer.echo(
                f"{len(plan.stale)} remote objects are not in the library (--delete removes)"
            )
        if dry_run or verbose:
            shown = plan.actions if verbose else plan.actions[:30]
            for action in shown:
                typer.echo(f"  {action.describe()}")
            if len(shown) < len(plan.actions):
                typer.echo(f"  ... and {len(plan.actions) - len(shown)} more (-v lists all)")
        if dry_run or not plan.actions:
            return
        total, done = len(plan.actions), 0

        def progress(action: sy.Action, error: str | None) -> None:
            nonlocal done
            done += 1
            if error or verbose:
                typer.echo(
                    f"  [{done}/{total}] {action.describe()}" + (f": {error}" if error else "")
                )
            elif done % 100 == 0:
                typer.echo(f"  [{done}/{total}]", err=True)

        result = sy.execute(session, target, sy.store_for(target), plan.actions,
                            workers=workers, progress=progress)  # fmt: skip
    done_counts = ", ".join(f"{n} {kind}" for kind, n in sorted(result.done.items()))
    typer.echo(f"Done: {done_counts or 'nothing'}; {len(result.errors)} errors")
    if result.errors:
        raise typer.Exit(1)


# ---- playlists ---------------------------------------------------------------------------

LibraryOption = Annotated[
    str | None, typer.Option("--library", "-l", help="Library [default: the only one]")
]


def _playlist(session: Session, name: str, library: str | None) -> Playlist:
    lib = _library(session, library)
    playlist = session.scalar(
        select(Playlist).where(Playlist.library_id == lib.id, Playlist.name == name)
    )
    if playlist is None:
        raise typer.BadParameter(f"No playlist named {name!r} in {lib.name}")
    return playlist


def _check(playlist: Playlist) -> None:
    try:
        pl.validate(playlist)
    except pl.RuleError as e:
        raise typer.BadParameter(str(e)) from None


def _describe_playlist(playlist: Playlist) -> str:
    if not playlist.is_smart:
        kind = f"static, {len(playlist.entries)} entries"
    else:
        kind = f"rule: {playlist.rule}"
        if playlist.group_by:
            kind += f"; one per {playlist.group_by}"
    limit = f", max {playlist.max_tracks}" if playlist.max_tracks else ""
    return f"{playlist.name}\t{kind}; sort: {playlist.sort}{limit}"


@playlists_app.command("list")
def playlists_list(ctx: typer.Context, library: LibraryOption = None) -> None:
    with _session(ctx) as session:
        for playlist in pl.library_playlists(session, _library(session, library)):
            typer.echo(_describe_playlist(playlist))


@playlists_app.command("create")
def playlists_create(
    ctx: typer.Context,
    name: str,
    rule: Annotated[str | None, typer.Option(help="Rule of a smart playlist")] = None,
    group_by: Annotated[
        str | None, typer.Option(help="One playlist per value of this field")
    ] = None,
    sort: Annotated[
        str | None, typer.Option(help="shuffle, album, artist, title, added, path or manual")
    ] = None,
    max_tracks: Annotated[
        int | None, typer.Option("--max", help="Keep at most this many tracks")
    ] = None,
    library: LibraryOption = None,
) -> None:
    """Create a playlist: a smart one with --rule, otherwise an empty static one.

    A rule is a list of conditions that must all hold:

    `genre=Rock|Pop  preference="Very Good|Excellent"  year>=1990  title!~live`

    **=** equals, **!=** does not equal, **~** contains, **!~** does not contain (all
    case-insensitive; alternatives separated by |; an empty value means a missing
    field), and **>**, **>=**, **<**, **<=** for numbers. Fields are the tag fields plus
    year, path, format, duration (seconds) and bitrate (kbps).

    With --group-by, one playlist is made per value of the field, and the name must
    contain the field in braces: `aorg playlists create "Best Of {mood}" --group-by mood
    --rule 'preference="Very Good|Excellent"'`

    Smart playlists hold one copy of each song (identical audio, or the same artist and
    title) and are shuffled by default, in an order that stays the same between runs.
    """
    with _session(ctx) as session:
        lib = _library(session, library)
        playlist = Playlist(
            library_id=lib.id, name=name, rule=rule, group_by=group_by,
            sort=sort or ("shuffle" if rule else "manual"), max_tracks=max_tracks or None,
        )  # fmt: skip
        _check(playlist)
        existing = session.scalar(
            select(Playlist).where(Playlist.library_id == lib.id, Playlist.name == name)
        )
        if existing:
            raise typer.BadParameter(f"There is already a playlist named {name!r}")
        session.add(playlist)
        session.commit()
        typer.echo(f"Created {_describe_playlist(playlist)}")


@playlists_app.command("edit")
def playlists_edit(
    ctx: typer.Context,
    name: str,
    rule: Annotated[str | None, typer.Option(help="New rule")] = None,
    group_by: Annotated[str | None, typer.Option(help="Field to group by; '' for none")] = None,
    sort: Annotated[str | None, typer.Option()] = None,
    max_tracks: Annotated[int | None, typer.Option("--max", help="0 for no limit")] = None,
    rename: Annotated[str | None, typer.Option(help="New name")] = None,
    library: LibraryOption = None,
) -> None:
    """Change a playlist's rule, grouping, sorting, size or name."""
    with _session(ctx) as session:
        playlist = _playlist(session, name, library)
        if rule is not None:
            if not playlist.is_smart:
                raise typer.BadParameter("A static playlist has no rule")
            playlist.rule = rule
        if group_by is not None:
            playlist.group_by = group_by or None
        if sort is not None:
            playlist.sort = sort
        if max_tracks is not None:
            playlist.max_tracks = max_tracks or None
        if rename is not None:
            playlist.name = rename
        _check(playlist)
        session.commit()
        typer.echo(f"Changed {_describe_playlist(playlist)}")


@playlists_app.command("delete")
def playlists_delete(ctx: typer.Context, name: str, library: LibraryOption = None) -> None:
    with _session(ctx) as session:
        session.delete(_playlist(session, name, library))
        session.commit()
    typer.echo(f"Deleted {name}")


@playlists_app.command("show")
def playlists_show(
    ctx: typer.Context,
    name: str,
    tracks: Annotated[int, typer.Option("--tracks", "-n", help="Tracks to list per playlist")] = 20,
    library: LibraryOption = None,
) -> None:
    """Generate a playlist and list its tracks. The name may be a grouped playlist's
    ("Best Of {mood}", showing all of its playlists) or one generated from it
    ("Best Of Upbeat")."""
    with _session(ctx) as session:
        lib = _library(session, library)
        playlist = session.scalar(
            select(Playlist).where(Playlist.library_id == lib.id, Playlist.name == name)
        )
        if playlist is not None:
            results = pl.generate(session, lib, [playlist])
        else:
            grouped = [p for p in pl.library_playlists(session, lib) if p.group_by]
            results = [g for g in pl.generate(session, lib, grouped) if g.name == name]
            if not results:
                raise typer.BadParameter(f"No playlist named {name!r} in {lib.name}")
        for generated in results:
            unresolved = f", {generated.unresolved} not found" if generated.unresolved else ""
            typer.echo(f"{generated.name}: {len(generated.tracks)} tracks{unresolved}")
            for track in generated.tracks[:tracks]:
                artist, title = track.get("artist") or "?", track.get("title") or "?"
                album = f"  ({track.get('album')})" if track.get("album") else ""
                typer.echo(f"  {artist} - {title}{album}")
            if len(generated.tracks) > tracks:
                typer.echo(f"  ... and {len(generated.tracks) - tracks} more")


def _static(session: Session, name: str, library: str | None) -> Playlist:
    playlist = _playlist(session, name, library)
    if playlist.is_smart:
        raise typer.BadParameter(f"{name} is a smart playlist; change its rule instead")
    return playlist


@playlists_app.command("add")
def playlists_add(
    ctx: typer.Context, name: str, paths: list[str], library: LibraryOption = None
) -> None:
    """Add tracks to the end of a static playlist."""
    with _session(ctx) as session:
        playlist = _static(session, name, library)
        files = [_find_file(session, path) for path in paths]
        pairs = [(e.path, e.file_id) for e in playlist.entries] + [(f.path, f.id) for f in files]
        pl.set_entries(playlist, pairs)
        session.commit()
    typer.echo(f"Added {len(files)} tracks to {name}")


@playlists_app.command("remove")
def playlists_remove(
    ctx: typer.Context, name: str, paths: list[str], library: LibraryOption = None
) -> None:
    """Remove tracks from a static playlist."""
    with _session(ctx) as session:
        playlist = _static(session, name, library)
        ids = {_find_file(session, path).id for path in paths}
        kept = [(e.path, e.file_id) for e in playlist.entries if e.file_id not in ids]
        removed = len(playlist.entries) - len(kept)
        pl.set_entries(playlist, kept)
        session.commit()
    typer.echo(f"Removed {removed} entries from {name}")


@playlists_app.command("import")
def playlists_import(
    ctx: typer.Context,
    files: list[Path],
    name: Annotated[str | None, typer.Option(help="Playlist name [default: file name]")] = None,
    library: LibraryOption = None,
) -> None:
    """Import .m3u files as static playlists. Entries may be absolute paths, or relative
    to the library or to the .m3u file."""
    if name and len(files) > 1:
        raise typer.BadParameter("--name only works when importing one file")
    with _session(ctx) as session:
        lib = _library(session, library)
        for file in files:
            try:
                playlist, unmatched = pl.import_m3u(session, lib, file, name)
            except pl.RuleError as e:
                typer.echo(f"! {file}: {e}")
                continue
            session.commit()
            found = len(playlist.entries) - len(unmatched)
            typer.echo(f"{playlist.name}: {found} of {len(playlist.entries)} tracks found")
            for entry in unmatched[:5]:
                typer.echo(f"  not found: {entry}")
            if len(unmatched) > 5:
                typer.echo(f"  ... and {len(unmatched) - 5} more")


@playlists_app.command("export")
def playlists_export(
    ctx: typer.Context,
    directory: Path,
    names: Annotated[list[str] | None, typer.Argument(help="Playlists [default: all]")] = None,
    formats: Annotated[
        str, typer.Option("--format", help="Comma-separated: m3u, json, html")
    ] = "m3u",
    library: LibraryOption = None,
) -> None:
    """Write playlist files into a folder, referring to tracks by relative paths."""
    fmts = [f.strip() for f in formats.split(",") if f.strip()]
    unknown = set(fmts) - {"m3u", "json", "html"}
    if unknown:
        raise typer.BadParameter(f"Unknown formats: {', '.join(sorted(unknown))}")
    with _session(ctx) as session:
        lib = _library(session, library)
        playlists = [_playlist(session, n, library) for n in names] if names else None
        generated = pl.generate(session, lib, playlists)
        written = pl.export(generated, Path(lib.root), directory, fmts)
    typer.echo(f"Wrote {len(written)} files for {len(generated)} playlists to {directory}")
