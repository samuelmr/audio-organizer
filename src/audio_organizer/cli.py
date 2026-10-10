"""Command line interface: aorg <command>."""

import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Annotated

import typer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import __version__, coverart, gnudb, loudness
from . import album_merge as am
from . import artists as art
from . import changes as ch
from . import covers as cv
from . import duplicates as du
from . import encodings as rec
from . import layout as lo
from . import leftovers as lf
from . import neighbours as nb
from . import playlist_files as pf
from . import playlist_store as ps
from . import playlists as pl
from . import ratings as rt
from . import sync as sy
from . import sync_jobs as jobs
from . import writing as wr
from .db import make_sessionmaker
from .models import AudioFile, Change, Changeset, Library, Playlist, SyncTarget
from .provenance import resolve
from .scanner import ScanStats, scan
from .tags import FIELDS
from .tracks import album_files, relative_folder

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


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"audio-organizer {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    db: Annotated[
        str | None,
        typer.Option(help="Catalog database path or URL [default: $AORG_DB or ~/.local/share]"),
    ] = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version", help="Show the version and exit", is_eager=True, callback=_show_version
        ),
    ] = False,
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


@library_app.command("set")
def library_set(
    ctx: typer.Context,
    name: str,
    playlist_folder: Annotated[
        str | None,
        typer.Option(help="Folder for playlist files, relative to the library root ('' = root)"),
    ] = None,
    playlist_formats: Annotated[
        str | None, typer.Option(help="Playlist file formats, comma-separated: m3u, json, html")
    ] = None,
) -> None:
    """Change where playlist files are written in a library, and in which formats."""
    with _session(ctx) as session:
        lib = _library(session, name)
        try:
            if playlist_folder is not None:
                lib.playlist_folder = pf.clean_folder(playlist_folder)
            if playlist_formats is not None:
                lib.playlist_formats = ",".join(pf.parse_formats(playlist_formats))
        except ValueError as e:
            raise typer.BadParameter(str(e)) from None
        session.commit()
        typer.echo(
            f"{lib.name}: playlists go to {Path(lib.root, lib.playlist_folder)} "
            f"as {lib.playlist_formats}"
        )


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
    reread: Annotated[
        bool,
        typer.Option(
            "--reread", help="Read every file again, e.g. after an update that reads more tags"
        ),
    ] = False,
    deep: Annotated[
        bool,
        typer.Option(
            "--deep",
            help="Also measure the loudness of every file (ReplayGain); reads all files in full",
        ),
    ] = False,
    deep_limit: Annotated[
        int | None,
        typer.Option(help="Measure at most this many files, and look up this many albums"),
    ] = None,
    with_gnudb: Annotated[
        bool,
        typer.Option(
            "--with-gnudb",
            help="Also look up albums in gnudb (slow; best once the library is tidy)",
        ),
    ] = False,
    with_covers: Annotated[
        bool,
        typer.Option(
            "--with-covers",
            help="Also look up missing covers online (MusicBrainz, Cover Art Archive)",
        ),
    ] = False,
    min_pixels: Annotated[
        int, typer.Option(help="Look up covers for tracks whose cover is smaller than this")
    ] = cv.MIN_PIXELS,
    playlists: Annotated[
        bool,
        typer.Option(
            "--playlists/--no-playlists",
            help="Import .m3u files in the library's playlist folder as static playlists",
        ),
    ] = True,
) -> None:
    """Catalog new and changed files. Unchanged files are skipped.

    Playlist files (.m3u, .m3u8) directly in the library's playlist folder (the library
    root unless changed with `aorg library set`) are imported as static playlists, except
    files this tool wrote itself. Playlists in album folders are not imported.

    With --deep, files are also read in full to measure their loudness: ReplayGain 2.0
    track gain and peak, and album gain and peak once all of an album's tracks are
    measured. With --with-gnudb, complete single-disc album folders are looked up in
    gnudb (needs your email address in AORG_GNUDB_EMAIL) for the album's title, year,
    genre and track titles. With --with-covers, albums whose tracks have no cover, or
    one smaller than --min-pixels on a side, and no big enough image in their folder,
    are looked up in MusicBrainz and the Cover Art Archive. The options work with or
    without each other, in that order. The results are kept in the catalog, not written
    into the files (see `aorg write-tags` and `aorg covers`).

    The lookups ask a service about one album at a time, which takes seconds per album,
    so tidy the library first, or use `aorg gnudb` and `aorg coverart` on their own. It
    takes long; it continues where an interrupted run stopped, and --deep-limit splits
    it into parts.
    """
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
            reread=reread, import_playlists=playlists, progress=progress,
        )  # fmt: skip
    typer.echo(
        f"{stats.seen} files: {stats.new} new, {stats.changed} changed, {stats.moved} moved, "
        f"{stats.unchanged} unchanged, {stats.missing} missing, {len(stats.errors)} errors "
        f"({time.monotonic() - started:.0f} s)"
    )
    found = stats.playlists
    if found.imported:
        typer.echo(f"Imported {len(found.imported)} playlists: {', '.join(found.imported[:10])}")
        if len(found.imported) > 10:
            typer.echo(f"  ... and {len(found.imported) - 10} more")
    for path in found.conflicts:
        typer.echo(f"  {path} differs from the static playlist of that name; not imported")
    for path, error in found.errors:
        typer.echo(f"  {path}: {error}")
    for relpath, error in stats.errors[:20]:
        typer.echo(f"  {relpath}: {error}")
    if len(stats.errors) > 20:
        typer.echo(f"  ... and {len(stats.errors) - 20} more")
    if deep:
        _deep_scan(ctx, library, deep_limit)
    if with_gnudb:
        if gnudb.has_email():
            _gnudb_lookups(ctx, library, deep_limit)
        else:
            typer.echo("Skipping gnudb: set AORG_GNUDB_EMAIL to your email address")
    if with_covers:
        _cover_lookups(ctx, library, deep_limit, min_pixels)


def _deep_scan(ctx: typer.Context, library: str | None, limit: int | None) -> None:
    started = time.monotonic()
    last_report = 0.0

    def progress(done: int, total: int, relpath: str, error: str | None) -> None:
        nonlocal last_report
        now = time.monotonic()
        if error:
            typer.echo(f"  ! {error}", err=True)
        if now - last_report >= 5 or done == total:
            last_report = now
            rate = done / max(now - started, 0.001)
            left = (total - done) / rate if rate else 0
            typer.echo(
                f"  measured {done}/{total} ({rate:.1f} files/s, ~{left / 60:.0f} min left): "
                f"{relpath}",
                err=True,
            )

    with _session(ctx) as session:
        lib = _library(session, library)
        todo = len(loudness.unmeasured(session, lib))
        typer.echo(f"Measuring loudness: {min(todo, limit or todo)} of {todo} files not measured")
        stats = loudness.measure_library(session, lib, limit=limit, progress=progress)
    typer.echo(
        f"Measured {stats.measured} files, {len(stats.errors)} errors; album gain for "
        f"{stats.albums} albums ({time.monotonic() - started:.0f} s)"
    )


def _gnudb_lookups(ctx: typer.Context, library: str | None, limit: int | None) -> None:
    started = time.monotonic()

    def progress(done: int, total: int, album: str, error: str | None) -> None:
        if error:
            typer.echo(f"  ! {error}", err=True)
        typer.echo(f"  looked up {done}/{total}: {album}", err=True)

    with _session(ctx) as session:
        lib = _library(session, library)
        typer.echo("Looking up albums in gnudb (one request every few seconds)")
        stats = gnudb.lookup_library(session, lib, limit=limit, progress=progress)
    typer.echo(
        f"gnudb: {stats.found} found, {stats.not_found} not found, {len(stats.errors)} "
        f"errors, of {stats.albums} albums to look up ({time.monotonic() - started:.0f} s)"
    )
    if stats.stopped:
        typer.echo(f"Stopped: {stats.stopped}")


@app.command("gnudb")
def gnudb_command(
    ctx: typer.Context,
    library: Annotated[str | None, typer.Argument()] = None,
    limit: Annotated[int | None, typer.Option(help="Look up at most this many albums")] = None,
    path: Annotated[
        str | None,
        typer.Option(
            help="Look up just this album folder now (relative to the library, or inside it)"
        ),
    ] = None,
) -> None:
    """Look up album folders in gnudb, without a scan.

    The same lookup as in `aorg scan --with-gnudb`, on its own and in parts: --limit 100 asks
    about 100 albums, and the next run continues with the rest. gnudb needs your email
    address as a contact: set AORG_GNUDB_EMAIL. Requests are spread out (every few
    seconds), so 100 albums take 10 to 25 minutes.

    With --path, one folder is looked up at once, even if it was looked up before.
    """
    if not gnudb.has_email():
        raise typer.BadParameter("set AORG_GNUDB_EMAIL to your email address")
    if path is None:
        _gnudb_lookups(ctx, library, limit)
        return
    with _session(ctx) as session:
        lib = _library(session, library)
        try:
            results = gnudb.lookup_folder(session, lib, path)
        except gnudb.GnudbError as e:
            typer.echo(f"gnudb: {e}", err=True)
            raise typer.Exit(1) from None
    if not results:
        typer.echo(
            f"{path}: not an album folder that can be looked up: it needs all the tracks of a "
            "single disc (numbered 1..n), with a duration, an artist and an album title",
            err=True,
        )
        raise typer.Exit(1)
    for album, entry in results:
        if entry is None:
            typer.echo(f"{album.artist} – {album.album}: no matching entry in gnudb")
            continue
        typer.echo(
            f"{album.artist} – {album.album}: found {entry.artist} – {entry.album}"
            f" ({entry.year or 'no year'}, {entry.genre or 'no genre'}, disc {entry.discid})"
        )
        for number, title in enumerate(entry.titles, start=1):
            typer.echo(f"  {number:2}. {title}")
    if any(entry for _, entry in results):
        typer.echo("Values are kept in the catalog; write them with `aorg write-tags`.")


def _cover_lookups(ctx: typer.Context, library: str | None, limit: int | None, min_pixels: int):
    started = time.monotonic()
    last_report = 0.0

    def progress(done: int, total: int, album: str, error: str | None) -> None:
        nonlocal last_report
        now = time.monotonic()
        if error:
            typer.echo(f"  ! {error}", err=True)
        if now - last_report >= 5 or done == total:
            last_report = now
            typer.echo(f"  looked up {done}/{total}: {album}", err=True)

    with _session(ctx) as session:
        lib = _library(session, library)
        checked = cv.refresh_sizes(session, lib)
        if checked:
            typer.echo(f"Read the size of {checked} embedded covers")
        typer.echo("Looking up covers online (MusicBrainz, Cover Art Archive)")
        stats = coverart.lookup_library(
            session, lib, min_pixels=min_pixels, limit=limit, progress=progress
        )
    typer.echo(
        f"Covers: {stats.found} found, {stats.not_found} not found, {len(stats.errors)} "
        f"errors, of {stats.albums} albums to look up ({time.monotonic() - started:.0f} s)"
    )
    if stats.stopped:
        typer.echo(f"Stopped: {stats.stopped}")
    if stats.found:
        typer.echo("Embed them with `aorg covers`.")


@app.command("coverart")
def coverart_command(
    ctx: typer.Context,
    library: Annotated[str | None, typer.Argument()] = None,
    limit: Annotated[int | None, typer.Option(help="Look up at most this many albums")] = None,
    path: Annotated[
        str | None,
        typer.Option(
            help="Look up just this album folder now (relative to the library, or inside it)"
        ),
    ] = None,
    min_pixels: Annotated[
        int, typer.Option(help="Look up covers for tracks whose cover is smaller than this")
    ] = cv.MIN_PIXELS,
) -> None:
    """Look up missing album covers online, without a scan.

    The same lookup as in `aorg scan --with-covers`, on its own and in parts: --limit 100
    asks about 100 albums, and the next run continues with the rest. Covers are fetched
    from MusicBrainz and the Cover Art Archive (at most a request a second). With --path,
    one folder is looked up at once, even if it was looked up before. Embed what was
    found with `aorg covers`.
    """
    if path is None:
        _cover_lookups(ctx, library, limit, min_pixels)
        return
    with _session(ctx) as session:
        lib = _library(session, library)
        try:
            results = coverart.lookup_folder(session, lib, path, min_pixels=min_pixels)
        except coverart.CoverArtError as e:
            typer.echo(f"Cover Art Archive: {e}", err=True)
            raise typer.Exit(1) from None
    if not results:
        typer.echo(
            f"{path}: no album there needs a cover (it has one, embedded, as an image in the "
            f"folder or found online, of at least {min_pixels} pixels), or it lacks an artist "
            "and album",
            err=True,
        )
        raise typer.Exit(1)
    for album, found in results:
        if found is None:
            typer.echo(f"{album.artist} – {album.album}: no cover found")
        else:
            typer.echo(f"{album.artist} – {album.album}: found a cover ({found.release_group})")
    typer.echo("Embed it with `aorg covers`.")


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


@app.command("web")
def web(
    ctx: typer.Context,
    host: Annotated[str, typer.Option(help="Address to listen on")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on")] = 8000,
) -> None:
    """Run the web UI for browsing, editing and reviewing changes."""
    import uvicorn

    from .web import create_app

    if host not in ("127.0.0.1", "localhost", "::1"):
        typer.echo(
            f"Warning: the web UI has no login; anyone who can reach {host}:{port} can "
            "change your catalog and files.",
            err=True,
        )
    typer.echo(f"audio-organizer at http://{host}:{port}/")
    uvicorn.run(create_app(ctx.obj), host=host, port=port, log_level="warning")


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
    values = _edit_values(set_, unset)
    with _session(ctx) as session:
        file = _find_file(session, path)
        changeset = ch.new_changeset(session, f"Manual edit of {file.path}", "manual")
        change = ch.propose_manual_edit(session, changeset, file, values)
        if change is None:
            session.expunge(changeset)
            session.commit()
            typer.echo("Saved; the file's tags already match.")
            return
        session.commit()
        typer.echo(f"Changeset {changeset.id}: {ch.describe(change)}")
        typer.echo(f"Review with `aorg changes show {changeset.id}`, then approve and apply.")


@app.command("edit-album")
def edit_album(
    ctx: typer.Context,
    library: Annotated[str | None, typer.Argument()] = None,
    album: Annotated[str | None, typer.Option(help="The album's name (ignoring case)")] = None,
    albumartist: Annotated[
        str | None, typer.Option(help="With --album: only the album by this album artist")
    ] = None,
    folder: Annotated[
        str | None,
        typer.Option(
            help="The tracks in this folder, not its subfolders (relative to the "
            "library, or inside it)"
        ),
    ] = None,  # fmt: skip
    set_: Annotated[list[str], typer.Option("--set", help="field=value")] = [],  # noqa: B006
    unset: Annotated[list[str], typer.Option(help="Field whose manual value to remove")] = [],  # noqa: B006
) -> None:
    """Edit all tracks of an album or folder at once, e.g. to fix a typo in the album's
    name. The edit is stored in the catalog for every track and proposed as one changeset
    that writes it into their tags:

        aorg edit-album --album "Best of Foriegner" --set album="Best of Foreigner"
        aorg edit-album --folder "Foreigner/Best of Foriegner" --set date=1992
    """
    values = _edit_values(set_, unset)
    if not values:
        raise typer.BadParameter("give at least one --set or --unset")
    if (album is None) == (folder is None):
        raise typer.BadParameter("give either --album or --folder")
    with _session(ctx) as session:
        lib = _library(session, library)
        if folder is not None:
            try:
                folder = relative_folder(lib, folder)
            except ValueError as e:
                raise typer.BadParameter(str(e)) from None
        files = album_files(session, lib.id, album, albumartist, folder)
        if not files:
            typer.echo("No tracks found.")
            raise typer.Exit(1)
        what = f"album {album}" if album is not None else f"folder {folder}/"
        changeset = ch.new_changeset(session, f"Manual edit of {what}", "manual")
        changes = ch.propose_manual_edits(session, changeset, files, values)
        if not changes:
            session.expunge(changeset)
            session.commit()
            typer.echo(f"Saved for {len(files)} tracks; their tags already match.")
            return
        session.commit()
        folders = sorted({str(PurePosixPath(f.path).parent) for f in files})
        typer.echo(
            f"Changeset {changeset.id}: {len(changes)} of {len(files)} tracks, in "
            + (folders[0] + "/" if len(folders) == 1 else f"{len(folders)} folders:")
        )
        if len(folders) > 1:
            for name in folders:
                typer.echo(f"  {name}/")
        for change in changes:
            typer.echo(f"  {ch.describe(change)}")
        typer.echo(f"Review with `aorg changes show {changeset.id}`, then approve and apply.")


def _edit_values(set_: list[str], unset: list[str]) -> dict[str, str | None]:
    """The fields given as --set field=value, and as --unset field (None)."""
    values: dict[str, str | None] = {name: None for name in unset}
    for item in set_:
        name, sep, value = item.partition("=")
        if not sep or name not in FIELDS:
            raise typer.BadParameter(f"--set takes field=value; fields: {', '.join(FIELDS)}")
        values[name] = value.strip()
    unknown = [name for name in unset if name not in FIELDS]
    if unknown:
        raise typer.BadParameter(f"Not fields: {', '.join(unknown)}")
    return values


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
            typer.echo(
                f"{changeset.id}\t{changeset.source or '-'}\t{changeset.description}\t{summary}"
            )


@changes_app.command("show")
def changes_show(ctx: typer.Context, changeset_id: int) -> None:
    with _session(ctx) as session:
        changeset = _changeset(session, changeset_id)
        source = changeset.source or "unknown source"
        typer.echo(f"Changeset {changeset.id} ({source}): {changeset.description}")
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


@changes_app.command("retry")
def changes_retry(
    ctx: typer.Context,
    changeset_id: int,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Only show what would be done")
    ] = False,
) -> None:
    """Try the failed changes of a changeset again (other approved changes are applied too)."""
    with _session(ctx) as session:
        changeset = _changeset(session, changeset_id)
        if dry_run:
            failed = [c for c in changeset.changes if c.status == "failed"]
            outcomes = [ch.Outcome(c, True, "would retry " + ch.describe(c)) for c in failed]
        else:
            ch.retry_failed(changeset)
            outcomes = ch.apply(session, changeset)
        _report(outcomes)


@changes_app.command("delete")
def changes_delete(ctx: typer.Context, changeset_id: int) -> None:
    """Delete a changeset that has no applied changes (undo those first)."""
    with _session(ctx) as session:
        try:
            ch.delete_changeset(session, _changeset(session, changeset_id))
        except ch.ChangeError as e:
            raise typer.BadParameter(str(e)) from e
        typer.echo(f"Deleted changeset {changeset_id}")


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
    try:
        sy.check_target_name(name)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None
    with _session(ctx) as session:
        lib = _library(session, library)
        session.add(SyncTarget(
            name=name, library=lib, bucket=bucket, prefix=prefix.strip("/"),
            endpoint_url=endpoint_url, region=region, profile=profile,
        ))  # fmt: skip
        session.commit()
    typer.echo(f"Added target {name}: {lib.name} -> s3://{bucket}/{prefix.strip('/')}")


@target_app.command("set")
def target_set(
    ctx: typer.Context,
    name: str,
    playlist_folder: Annotated[
        str | None,
        typer.Option(help="Folder for playlist files, relative to the prefix ('' = the prefix)"),
    ] = None,
    playlist_formats: Annotated[
        str | None, typer.Option(help="Playlist file formats, comma-separated: m3u, json, html")
    ] = None,
) -> None:
    """Change where playlist files go in a target's bucket, and in which formats. The
    next sync writes them there; copies in the old place are left (see `aorg sync`)."""
    with _session(ctx) as session:
        target = _target(session, name)
        try:
            if playlist_folder is not None:
                target.playlist_folder = pf.clean_folder(playlist_folder)
            if playlist_formats is not None:
                target.playlist_formats = ",".join(pf.parse_formats(playlist_formats))
        except ValueError as e:
            raise typer.BadParameter(str(e)) from None
        session.commit()
        for line in sy.bucket_layout(target)[2:]:
            typer.echo(line)


@target_app.command("list")
def target_list(ctx: typer.Context) -> None:
    """The sync targets: name, library, bucket and prefix, and how much of the bucket
    this catalog knows (see `aorg target show` for the layout)."""
    with _session(ctx) as session:
        for target in session.scalars(select(SyncTarget).order_by(SyncTarget.name)):
            typer.echo(_target_line(session, target))


def _target_line(session: Session, target: SyncTarget) -> str:
    where = f"s3://{target.bucket}/{target.prefix}"
    extras = [
        f"{label} {value}"
        for label, value in (
            ("endpoint", target.endpoint_url),
            ("region", target.region),
            ("profile", target.profile),
        )
        if value
    ]
    objects, last = sy.remote_summary(session, target)
    state = f"{objects} objects, last synced {last:%Y-%m-%d %H:%M}" if objects else "never synced"
    return f"{target.name}\t{target.library.name} -> {where}\t{state}" + "".join(
        f"\t{e}" for e in extras
    )


@target_app.command("show")
def target_show(ctx: typer.Context, name: str) -> None:
    """One target, and where its tracks, covers and playlists go in the bucket."""
    with _session(ctx) as session:
        target = _target(session, name)
        typer.echo(_target_line(session, target))
        typer.echo("")
        typer.echo("Layout:")
        for line in sy.bucket_layout(target):
            typer.echo(f"  {line}")


@target_app.command("rename")
def target_rename(ctx: typer.Context, name: str, new_name: str) -> None:
    """Rename a target (what is in the bucket is not touched)."""
    try:
        sy.check_target_name(new_name)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None
    with _session(ctx) as session:
        target = _target(session, name)
        if session.scalar(select(SyncTarget).where(SyncTarget.name == new_name)):
            raise typer.BadParameter(f"There is already a target named {new_name!r}")
        target.name = new_name
        session.commit()
    typer.echo(f"Renamed {name} to {new_name}")


@target_app.command("remove")
def target_remove(ctx: typer.Context, name: str) -> None:
    """Forget a target. Nothing in the bucket is deleted; if you add it again, run
    `aorg target reconcile` first so that the files are not uploaded twice."""
    with _session(ctx) as session:
        try:
            objects = sy.remove_target(session, _target(session, name))
        except jobs.SyncBusy as e:
            raise typer.BadParameter(str(e)) from None
    typer.echo(f"Removed target {name}: forgot {objects} synced objects, the bucket is unchanged")


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
        busy = jobs.active_job(session, target)
        if busy is not None:
            raise typer.BadParameter(str(jobs.SyncBusy(busy)))
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

        try:
            job = jobs.start(session, target, "cli")
        except jobs.SyncBusy as e:
            raise typer.BadParameter(str(e)) from None
        result = jobs.run(session, job, plan, sy.store_for(target), workers=workers,
                          on_action=progress)  # fmt: skip
        if result is None or result.stopped:
            typer.echo(job.message)
            raise typer.Exit(1)
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
    try:
        playlist = pl.find(session, lib, name)
    except pl.RuleError as e:
        raise typer.BadParameter(str(e)) from None
    if playlist is None:
        raise typer.BadParameter(f"No playlist named {name!r} in {lib.name}")
    return playlist


def _check(playlist: Playlist) -> None:
    try:
        pl.validate(playlist)
    except pl.RuleError as e:
        raise typer.BadParameter(str(e)) from None


def _describe_playlist(playlist: Playlist, shared: set[str] = frozenset()) -> str:
    if not playlist.is_smart:
        kind = f"static, {len(playlist.entries)} entries"
    else:
        kind = f"rule: {playlist.rule}"
        if playlist.group_by:
            kind += f"; one per {playlist.group_by}"
    limit = f", max {playlist.max_tracks}" if playlist.max_tracks else ""
    return f"{pl.display_name(playlist, shared)}\t{kind}; sort: {playlist.sort}{limit}"


@playlists_app.command("list")
def playlists_list(ctx: typer.Context, library: LibraryOption = None) -> None:
    with _session(ctx) as session:
        playlists = pl.library_playlists(session, _library(session, library))
        shared = pl.shared_names(playlists)
        for playlist in playlists:
            typer.echo(_describe_playlist(playlist, shared))


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

    `genre=Rock|Pop  rating>=4  year>=1990  title!~live`

    **=** equals, **!=** does not equal, **~** contains, **!~** does not contain (all
    case-insensitive; alternatives separated by |; an empty value means a missing
    field), and **>**, **>=**, **<**, **<=** for numbers. Fields are the tag fields plus
    year, path, format, duration (seconds) and bitrate (kbps).

    With --group-by, one playlist is made per value of the field, and the name must
    contain the field in braces: `aorg playlists create "Best Of {mood}" --group-by mood
    --rule 'rating>=4'`

    Smart playlists hold one copy of each song (identical audio, or the same artist and
    title) and are shuffled by default, in a new order each time they are generated.
    """
    with _session(ctx) as session:
        lib = _library(session, library)
        playlist = Playlist(
            library_id=lib.id, name=name, rule=rule, group_by=group_by,
            sort=sort or ("shuffle" if rule else "manual"), max_tracks=max_tracks or None,
        )  # fmt: skip
        _check(playlist)
        if pl.name_taken(session, lib, name, smart=playlist.is_smart):
            raise typer.BadParameter(f"There is already a {pl.kind(playlist)} playlist {name!r}")
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
            if pl.name_taken(
                session, playlist.library, rename, smart=playlist.is_smart, except_id=playlist.id
            ):
                raise typer.BadParameter(
                    f"There is already a {pl.kind(playlist)} playlist {rename!r}"
                )
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
        try:
            playlist = pl.find(session, lib, name)
        except pl.RuleError as e:
            raise typer.BadParameter(str(e)) from None
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


TargetOption = Annotated[
    str | None,
    typer.Option("--target", help="Sync target (bucket) instead of the library's disk"),
]


def _place(session: Session, library: str | None, target: str | None) -> ps.Place:
    lib = _library(session, library)
    if target is None:
        return ps.Place(lib)
    tgt = _target(session, target)
    if tgt.library_id != lib.id:
        raise typer.BadParameter(f"Target {target} syncs library {tgt.library.name}")
    return ps.Place(lib, tgt)


@playlists_app.command("write")
def playlists_write(
    ctx: typer.Context,
    names: Annotated[list[str] | None, typer.Argument(help="Playlists [default: all]")] = None,
    target: TargetOption = None,
    dry_run: Annotated[bool, typer.Option(help="Only show what would be written")] = False,
    library: LibraryOption = None,
) -> None:
    """Write playlists to the library's disk, or with --target to a bucket, in the place's
    playlist folder and formats (`aorg library set`, `aorg target set`). A file that
    someone else has changed is replaced, and its old content kept: `restore-files`."""
    with _session(ctx) as session:
        place = _place(session, library, target)
        playlists = [_playlist(session, n, library) for n in names] if names else None
        planned = ps.plan_write(session, place, pl.generate(session, place.library, playlists))
        for item in planned:
            if item.changes:
                typer.echo(f"{place.describe(item.path)} {ps.describe_state(item)}")
        if dry_run:
            return
        changed = ps.apply_write(session, place, planned)
        session.commit()
    typer.echo(f"Wrote {changed} files ({len(planned) - changed} already up to date)")


@playlists_app.command("delete-files")
def playlists_delete_files(
    ctx: typer.Context,
    names: Annotated[list[str], typer.Argument(help="Playlists")],
    target: TargetOption = None,
    dry_run: Annotated[bool, typer.Option(help="Only show what would be deleted")] = False,
    library: LibraryOption = None,
) -> None:
    """Delete the files of playlists from the library's disk, or with --target from a
    bucket. The playlists stay in the catalog, and the files' content is kept for
    `restore-files`."""
    with _session(ctx) as session:
        place = _place(session, library, target)
        planned = []
        for name in names:
            planned += ps.plan_delete(session, place, _playlist(session, name, library))
        for item in planned:
            typer.echo(place.describe(item.path))
        if dry_run:
            return
        removed = ps.apply_delete(session, place, planned)
        session.commit()
    typer.echo(f"Deleted {removed} files")


@playlists_app.command("restore-files")
def playlists_restore_files(
    ctx: typer.Context,
    backup_id: Annotated[int | None, typer.Argument(help="Backup to restore")] = None,
    target: TargetOption = None,
    library: LibraryOption = None,
) -> None:
    """Without an id, list the playlist files that were deleted or replaced and can be
    restored; with one, put that file back."""
    with _session(ctx) as session:
        place = _place(session, library, target)
        found = ps.backups(session, place)
        if backup_id is None:
            for backup in found:
                typer.echo(
                    f"{backup.id}\t{backup.reason}\t{backup.created_at:%Y-%m-%d %H:%M}"
                    f"\t{place.describe(backup.path)}"
                )
            return
        chosen = next((b for b in found if b.id == backup_id), None)
        if chosen is None:
            raise typer.BadParameter(f"No backup {backup_id} to restore at {place.label}")
        ps.restore(session, place, chosen)
        session.commit()
    typer.echo(f"Restored {place.describe(chosen.path)}")


# ---- duplicates and trash ----------------------------------------------------------------


@app.command("dupes")
def dupes(
    ctx: typer.Context,
    path: Annotated[
        str | None, typer.Option(help="Only copies under this path, e.g. an artist folder")
    ] = None,
    limit: Annotated[int, typer.Option(help="Tracks to include")] = 50,
    propose: Annotated[bool, typer.Option("--propose", help="Create a changeset for them")] = False,
    include_compilations: Annotated[
        bool,
        typer.Option(
            "--include-compilations",
            help="Also when only a compilation's copy would be kept",
        ),
    ] = False,
    library: LibraryOption = None,
) -> None:
    """Find identical copies of tracks and propose to trash the loose ones.

    A copy in an album folder is kept; copies lying loose in an artist folder, in
    "Unknown Album" or next to it as iTunes copies ("Title 1.mp3") are proposed for the
    trash, after copying any tags only they have to the kept file. Tracks with copies
    in several album folders are left for you to decide, and so, unless you add
    --include-compilations, are tracks whose only album copy is on a compilation.
    Trashed files can be restored by undoing the changeset until the trash is emptied.
    """
    with _session(ctx) as session:
        lib = _library(session, library)
        found = du.survey(session, lib, path_prefix=path, include_compilations=include_compilations)
        typer.echo(
            f"{found.groups} tracks with identical copies: {len(found.proposals)} can be "
            f"cleaned up ({found.trash_count} copies to trash). Left alone: "
            f"{found.several_albums} in several album folders, {found.no_album} in no album "
            f"folder, {found.compilations} only on a compilation besides the artist's "
            f"folder, {found.pending} with changes waiting for review."
        )
        batch = found.proposals[:limit]
        for proposal in batch[:20] if not propose else []:
            typer.echo(f"  keep  {proposal.keep.file.path}")
            for copy in proposal.trash:
                typer.echo(f"  trash {copy.file.path}")
            if proposal.fill:
                typer.echo(f"        copying tags: {', '.join(proposal.fill)}")
        if not propose:
            if len(batch) > 20:
                typer.echo(f"  ... and {len(batch) - 20} more in this batch")
            if batch:
                typer.echo(f"Run with --propose to create a changeset for {len(batch)} tracks.")
            return
        if not batch:
            return
        where = f" in {path}" if path else ""
        changeset = du.propose(session, batch, f"Trash loose copies of {len(batch)} tracks{where}")
        typer.echo(
            f"Changeset {changeset.id}: {len(changeset.changes)} changes. Review with "
            f"`aorg changes show {changeset.id}` or in the web UI."
        )


def _encoding(file: AudioFile) -> str:
    return f"{file.format} {round((file.bitrate or 0) / 1000)} kbps, {file.duration or 0:.0f} s"


@app.command("encodings")
def encodings(
    ctx: typer.Context,
    path: Annotated[
        str | None, typer.Option(help="Only copies under this path, e.g. an artist folder")
    ] = None,
    limit: Annotated[int, typer.Option(help="Tracks to include")] = 50,
    propose: Annotated[bool, typer.Option("--propose", help="Create a changeset for them")] = False,
    replace: Annotated[
        bool,
        typer.Option(
            "--replace", help="Also where a loose copy would replace the album's (listen first)"
        ),
    ] = False,
    include_compilations: Annotated[
        bool,
        typer.Option(
            "--include-compilations",
            help="Also loose copies whose album copy is only on a compilation",
        ),
    ] = False,
    library: LibraryOption = None,
) -> None:
    """Find tracks encoded more than once and propose to keep the best encoding.

    Files are one track in different encodings when they are in one album folder
    with the same title and number, or lie loose with the artist and title of a track
    in one album folder, and their durations are within two seconds. The lossless or
    highest-bitrate copy is kept (WMA last), in the place of the album's copy; the others
    are proposed for the trash. Files whose durations differ are listed as versions.
    Where a loose copy is the best, it replaces the album's copy only with --replace.
    """
    with _session(ctx) as session:
        lib = _library(session, library)
        found = rec.survey(
            session, lib, path_prefix=path, include_compilations=include_compilations
        )
        typer.echo(
            f"{found.groups} tracks in several encodings: {len(found.proposals)} can be "
            f"cleaned up ({found.trash_count} files to trash; in "
            f"{len(found.proposals) - len(found.batchable)} a loose copy would replace the "
            f"album's). Left alone: {len(found.versions)} with different durations, "
            f"{found.several_albums} loose copies matching several album folders, "
            f"{found.compilations} only on a compilation besides the artist's folder, "
            f"{found.pending} with changes waiting for review."
        )
        batch = rec.batch(found.proposals, limit, replace=replace)
        for proposal in batch[:20] if not propose else []:
            typer.echo(f"  keep  {proposal.keep.file.path} ({_encoding(proposal.keep.file)})")
            for copy in proposal.trash:
                typer.echo(f"  trash {copy.file.path} ({_encoding(copy.file)})")
            if proposal.move_to:
                typer.echo(f"        moved to {proposal.move_to}")
            if proposal.fill:
                typer.echo(f"        copying tags: {', '.join(proposal.fill)}")
        if not propose:
            if len(batch) > 20:
                typer.echo(f"  ... and {len(batch) - 20} more in this batch")
            for files in found.versions[:10]:
                typer.echo("  versions, left alone:")
                for file in files:
                    typer.echo(f"        {file.path} ({_encoding(file)})")
            if batch:
                typer.echo(f"Run with --propose to create a changeset for {len(batch)} tracks.")
            return
        if not batch:
            return
        where = f" in {path}" if path else ""
        changeset = rec.propose(
            session, batch, f"Keep the best encoding of {len(batch)} tracks{where}"
        )
        typer.echo(
            f"Changeset {changeset.id}: {len(changeset.changes)} changes. Review with "
            f"`aorg changes show {changeset.id}` or in the web UI."
        )


def _describe_merge(merge: am.Merge) -> list[str]:
    note = "" if merge.same_name else "  [different album names]"
    lines = [
        f"  {merge.source.path} ({merge.source.distinct} tracks) -> {merge.target.path} "
        f"({merge.target.distinct} tracks){note}",
        f"      {len(merge.trash)} copies to trash, {len(merge.move)} tracks to move",
    ]
    for track in merge.clashes:
        lines.append(f"      clash, left in place: {track.name}")
    return lines


@app.command("merge-albums")
def merge_albums(
    ctx: typer.Context,
    path: Annotated[str | None, typer.Option(help="Only folders under this path")] = None,
    limit: Annotated[int, typer.Option(help="Merges to propose in a batch")] = 20,
    propose: Annotated[
        bool, typer.Option("--propose", help="Create a changeset for a batch")
    ] = False,
    source: Annotated[
        str | None, typer.Option("--from", help="Merge this album folder ...")
    ] = None,
    target: Annotated[str | None, typer.Option("--into", help="... into this one")] = None,
    library: LibraryOption = None,
) -> None:
    """Find album folders that hold the same album, and merge them.

    Merging trashes the source folder's copies of tracks the target already has and
    moves its other tracks into the target. A track whose number the target already uses
    is a clash and stays where it is. Batches (--propose) only include folders with the
    same album name and no clashes; merge others one at a time with --from and --into,
    in the direction you choose.
    """
    with _session(ctx) as session:
        lib = _library(session, library)
        if source or target:
            if not (source and target):
                raise typer.BadParameter("Give both --from and --into")
            try:
                merge = am.plan_folders(session, lib, source.strip("/"), target.strip("/"))
            except ValueError as e:
                raise typer.BadParameter(str(e)) from None
            for line in _describe_merge(merge):
                typer.echo(line)
            if not merge.trash and not merge.move:
                typer.echo("Nothing to merge")
                return
            changeset = am.propose(
                session, [merge], f"Merge {merge.source.path} into {merge.target.path}"
            )
            typer.echo(f"Changeset {changeset.id}: {len(changeset.changes)} changes")
            return
        found = am.survey(session, lib, path_prefix=path)
        chosen = am.batch(found.merges, limit)
        typer.echo(
            f"{len(found.merges)} pairs of album folders share tracks: "
            f"{len(found.batchable)} can be merged in batches, "
            f"{len(found.merges) - len(found.batchable)} need a look; "
            f"{found.pending} have changes waiting for review."
        )
        if propose:
            if chosen:
                changeset = am.propose(session, chosen, f"Merge {len(chosen)} album folders")
                typer.echo(
                    f"Changeset {changeset.id}: {len(changeset.changes)} changes for "
                    f"{len(chosen)} merges. Review with `aorg changes show {changeset.id}`."
                )
            return
        typer.echo("Next batch:" if chosen else "Nothing to merge in a batch.")
        for merge in chosen:
            for line in _describe_merge(merge):
                typer.echo(line)
        others = [m for m in found.merges if not m.batchable]
        if others:
            typer.echo("Need a look (merge with --from and --into):")
            for merge in others:
                for line in _describe_merge(merge):
                    typer.echo(line)


@app.command("neighbours")
def neighbours(
    ctx: typer.Context,
    path: Annotated[
        str | None, typer.Option(help="Only loose tracks under this path, e.g. an artist folder")
    ] = None,
    limit: Annotated[int, typer.Option(help="Tracks to include (whole albums)")] = 50,
    propose: Annotated[bool, typer.Option("--propose", help="Create a changeset for them")] = False,
    library: LibraryOption = None,
) -> None:
    """Put loose tracks back into their album folders.

    A loose track (in an artist folder or "Unknown Album") joins an album folder when its
    album tag names exactly one album folder of its artist, and its track number and
    title are not there yet. It is named like the album's other tracks and gets the
    album-wide tags they agree on (album artist, year, track total, ...). Run `aorg dupes`
    and `aorg encodings` first: loose copies of tracks already in an album are theirs.
    """
    with _session(ctx) as session:
        lib = _library(session, library)
        found = nb.survey(session, lib, path_prefix=path)
        typer.echo(
            f"{found.loose} loose tracks: {found.move_count} fit into {len(found.albums)} album "
            f"folders. Left alone: {found.duplicates} with identical copies in albums, "
            f"{found.no_album_tag} without an album tag, {found.no_folder} whose album has no "
            f"folder, {found.several_folders} matching several folders, {found.taken} whose "
            f"number or title is taken, {found.no_number} without a track number, "
            f"{found.conflicts} claiming the same number, {found.pending} with changes "
            f"waiting for review."
        )
        chosen = nb.batch(found.albums, limit)
        if not propose:
            for album in chosen:
                typer.echo(f"  {album.folder} ({album.tracks} tracks)")
                for move in album.moves:
                    typer.echo(f"      + {move.copy.file.path}")
                    typer.echo(f"        -> {PurePosixPath(move.to).name}")
                    if move.fill:
                        fill = ", ".join(f"{k}={v}" for k, v in move.fill.items())
                        typer.echo(f"        tags: {fill}")
            if chosen:
                count = sum(len(a.moves) for a in chosen)
                typer.echo(f"Run with --propose to create a changeset for {count} tracks.")
            return
        if not chosen:
            return
        count = sum(len(a.moves) for a in chosen)
        where = f" in {path}" if path else ""
        changeset = nb.propose(
            session, chosen, f"Put {count} loose tracks into {len(chosen)} albums{where}"
        )
        typer.echo(
            f"Changeset {changeset.id}: {len(changeset.changes)} changes. Review with "
            f"`aorg changes show {changeset.id}` or in the web UI."
        )


@app.command("leftovers")
def leftovers(
    ctx: typer.Context,
    path: Annotated[str | None, typer.Option(help="Only folders under this path")] = None,
    propose: Annotated[
        bool, typer.Option("--propose", help="Create a changeset trashing the litter")
    ] = False,
    everything: Annotated[
        bool, typer.Option("--all", help="Also propose files that are not known litter")
    ] = False,
    library: LibraryOption = None,
) -> None:
    """Find folders without music that still hold files, and trash the litter.

    Merging albums and trashing duplicates leave Folder.jpg, desktop.ini and old
    generated playlists behind, which keeps their folders from being removed. Looks at the
    disk, not the catalog. Only files known as litter are proposed (images such as
    Folder.jpg, playlists that name no tracks, logs, system files); other files are listed,
    and proposed with --all. A folder is removed when its last file is trashed.

    An album folder with no cover.jpg or cover.png but another image (AlbumArtSmall.jpg)
    gets a proposal to rename its biggest image to cover.jpg or cover.png.
    """
    with _session(ctx) as session:
        lib = _library(session, library)
        found = lf.survey(lib, path.strip("/") if path else None)
        typer.echo(
            f"{len(found.folders)} folders without music hold {found.litter} litter files "
            f"and {found.other} other files; {len(found.renames)} album folders have an "
            f"image to rename to cover."
        )
        for folder in found.folders:
            typer.echo(f"  {folder.path}/")
            for name in folder.litter:
                typer.echo(f"    litter  {PurePosixPath(name).name}")
            for name in folder.other:
                typer.echo(f"    other   {PurePosixPath(name).name}")
        for source, target in found.renames:
            typer.echo(f"  rename {source} -> {PurePosixPath(target).name}")
        if not propose:
            return
        changeset = lf.propose(
            session, lib, found, f"Trash leftover files in {len(found.folders)} folders",
            everything=everything,
        )  # fmt: skip
        if changeset is None:
            typer.echo("Nothing to trash.")
            return
        typer.echo(
            f"Changeset {changeset.id}: {len(changeset.changes)} changes. "
            f"Review with `aorg changes show {changeset.id}`."
        )


@app.command("write-tags")
def write_tags(
    ctx: typer.Context,
    path: Annotated[str | None, typer.Option(help="Only files under this path")] = None,
    only: Annotated[
        str | None,
        typer.Option(help="Only these fields, comma-separated; e.g. replaygain, rating"),
    ] = None,
    limit: Annotated[int, typer.Option(help="Files in a batch")] = 100,
    id3_version: Annotated[
        int | None, typer.Option(help="Write ID3 tags as version 3 or 4 [default: as now]")
    ] = None,
    prefer_gnudb: Annotated[
        bool,
        typer.Option(
            "--prefer-gnudb", help="Also replace tag values that differ from gnudb's (see --only)"
        ),
    ] = False,
    propose: Annotated[
        bool, typer.Option("--propose", help="Create a changeset for a batch")
    ] = False,
    library: LibraryOption = None,
) -> None:
    """Write values that are not yet in the files: your edits, measurements (`aorg scan
    --deep`) and lookups. Proposed as changesets, in batches.

    A gnudb value ranks below the tag, so by default only what the tags lack is written.
    With --prefer-gnudb, gnudb's value replaces a different tag, for the fields chosen
    with --only (e.g. --only genre,album) and the files under --path (one album's folder,
    say); the listing shows the tag and gnudb's value, and the changeset is reviewed as
    usual before anything is written.
    """
    if id3_version not in (None, 3, 4):
        raise typer.BadParameter("--id3-version is 3 or 4")
    fields = [f.strip() for f in only.split(",") if f.strip()] if only else None
    with _session(ctx) as session:
        lib = _library(session, library)
        prefer = ("gnudb",) if prefer_gnudb else ()
        found = wr.survey(session, lib, path_prefix=path, only=fields, prefer=prefer)
        counts = ", ".join(f"{name} {n}" for name, n in found.by_field.most_common())
        typer.echo(
            f"{len(found.files)} files have values not yet in them"
            + (f" ({counts})" if counts else "")
            + f"; {found.waiting} have changes waiting for review."
        )
        batch = found.files[:limit]
        if propose:
            if batch:
                what = "Write gnudb's values to" if prefer_gnudb else "Write"
                changeset = wr.propose(
                    session, batch, f"{what} the tags of {len(batch)} files", id3_version
                )
                typer.echo(
                    f"Changeset {changeset.id}: {len(changeset.changes)} files. Review with "
                    f"`aorg changes show {changeset.id}`."
                )
            return
        for pending in batch[:20]:
            if prefer_gnudb:
                typer.echo(f"  {pending.file.path}")
                for name, value in pending.fields.items():
                    typer.echo(f"      {name}: {pending.before.get(name)!r} -> {value!r}")
            else:
                typer.echo(f"  {pending.file.path}: {', '.join(pending.fields)}")
        if len(found.files) > 20:
            typer.echo(f"  ... and {len(found.files) - 20} more")
        if batch:
            typer.echo(f"Run with --propose to create a changeset for {len(batch)} files.")


@app.command("ratings")
def ratings_command(
    ctx: typer.Context,
    set_: Annotated[
        list[str] | None,
        typer.Option("--set", help='Stars for a preference: "Good=3"; "Good=" for none'),
    ] = None,
    apply: Annotated[
        bool, typer.Option("--apply", help="Set the ratings of files from the mapping")
    ] = False,
    library: LibraryOption = None,
) -> None:
    """Star ratings from MusicMatch "preference" values (Excellent, Very Good, ...).

    Shows how each preference maps to 1-5 stars; --set changes the mapping and --apply
    sets the ratings in the catalog. A rating already in a file's tags is kept. The
    ratings are then written into the files with `aorg write-tags --only rating`.
    """
    with _session(ctx) as session:
        lib = _library(session, library)
        mapping = rt.get_map(session, lib)
        for item in set_ or []:
            name, sep, stars = item.partition("=")
            if not sep or stars.strip() not in ("", "1", "2", "3", "4", "5"):
                raise typer.BadParameter('--set takes "Preference=1..5" or "Preference="')
            mapping[name.strip()] = int(stars) if stars.strip() else None
        if set_:
            rt.set_map(session, lib, mapping)
            session.commit()
        counts = rt.preferences(session, lib)
        for name, n in counts.most_common():
            stars = mapping.get(name)
            typer.echo(f"  {name:12} {n:6} files  ->  {'★' * stars if stars else 'no rating'}")
        if apply:
            rated = rt.apply(session, lib, mapping)
            typer.echo(
                f"Rated {rated} files. Write the ratings with `aorg write-tags --only rating`."
            )
        elif counts:
            typer.echo("Run with --apply to set the ratings.")


@app.command("covers")
def covers_command(
    ctx: typer.Context,
    path: Annotated[str | None, typer.Option(help="Only folders under this path")] = None,
    limit: Annotated[int, typer.Option(help="Folders in a batch")] = 20,
    min_pixels: Annotated[
        int, typer.Option(help="Smaller covers (on a side) are thumbnails, to be replaced")
    ] = cv.MIN_PIXELS,
    save_images: Annotated[
        bool,
        typer.Option(help="Also save covers found online as the folder's cover.jpg"),
    ] = True,
    propose: Annotated[
        bool, typer.Option("--propose", help="Create a changeset for a batch")
    ] = False,
    library: LibraryOption = None,
) -> None:
    """Embed covers into tracks that have none, or only a thumbnail.

    The cover is the folder's image (cover/folder/front.jpg or .png, or the folder's only
    image), or when the folder has no image of at least --min-pixels, one that `aorg
    coverart` (or `aorg scan --with-covers`) found online. It is copied into every such
    track, so large images make every file bigger. A cover found online is also saved as
    the folder's cover.jpg when the folder holds just that album. Run `aorg scan --reread`
    once first if the catalog is older than cover detection.
    """
    with _session(ctx) as session:
        lib = _library(session, library)
        found = cv.survey(
            session, lib, path_prefix=path, min_pixels=min_pixels, save_images=save_images
        )
        files = sum(len(f.files) for f in found.folders)
        replaced = sum(len(f.replace) for f in found.folders)
        online = sum(1 for f in found.folders if f.fetched is not None)
        saved = sum(1 for f in found.folders if f.save_as)
        typer.echo(
            f"{len(found.folders)} folders have a cover for {files} tracks without one "
            f"({replaced} with a thumbnail to replace; {online} covers found online, "
            f"{saved} to save in their folder); {len(found.too_small)} more have only a "
            f"thumbnail (under {min_pixels} pixels), and {found.without_image} folders "
            "with such tracks have no image."
        )
        batch = found.folders[:limit]
        if propose:
            if batch:
                changeset = cv.propose(
                    session, lib, batch, f"Embed the covers of {len(batch)} folders"
                )
                typer.echo(
                    f"Changeset {changeset.id}: {len(changeset.changes)} changes. Review with "
                    f"`aorg changes show {changeset.id}`."
                )
            return
        for folder in batch:
            dims = folder.fetched.size if folder.fetched else folder.dimensions
            size = "×".join(map(str, dims)) if dims else "?"
            name = f"{folder.album} found online" if folder.fetched else folder.image_name
            replace = f", replacing {len(folder.replace)} thumbnails" if folder.replace else ""
            save = ""
            if folder.save_as:
                verb = "replacing" if folder.overwrite else "to be saved as"
                save = f", {verb} {PurePosixPath(folder.save_as).name}"
            typer.echo(
                f"  {folder.folder}/: {name} ({size}, {folder.image_size // 1024} KB) into "
                f"{len(folder.files)} tracks{replace}{save}"
            )
        if batch:
            typer.echo(f"Run with --propose to create a changeset for {len(batch)} folders.")


@app.command("artists")
def artists_command(
    ctx: typer.Context,
    set_: Annotated[
        list[str] | None,
        typer.Option(
            "--set", help='Sort name: "Alanis Morissette=Morissette, Alanis"; empty removes'
        ),
    ] = None,
    accept_suggestions: Annotated[
        bool, typer.Option("--accept-suggestions", help="Use all suggested sort names")
    ] = False,
    all_: Annotated[
        bool, typer.Option("--all", help="List all artists, not only those with sort names")
    ] = False,
    library: LibraryOption = None,
) -> None:
    """Artists' sort names, used by the layout fields {albumartistsort} and {artistsort}
    (e.g. "Alanis Morissette" filed as "Morissette, Alanis").

    Suggestions come from your folders: a folder "Morissette, Alanis" suggests that sort
    name for "Alanis Morissette". Setting an artist's own name ("Yello=Yello") keeps it as
    it is and stops suggestions for it.
    """
    with _session(ctx) as session:
        lib = _library(session, library)
        for item in set_ or []:
            name, sep, sort_name = item.partition("=")
            if not sep or not name.strip():
                raise typer.BadParameter('--set takes "Name=Sort name"')
            art.set_sort_name(session, lib, name.strip(), sort_name)
        if accept_suggestions:
            count = art.accept_suggestions(session, lib, art.album_artists(session, lib))
            typer.echo(f"Accepted {count} suggested sort names")
        session.commit()
        artists = art.album_artists(session, lib)
        suggested = [a for a in artists if a.suggestion]
        named = [a for a in artists if a.sort_name]
        typer.echo(
            f"{len(artists)} album artists: {len(named)} with a sort name, "
            f"{len(suggested)} with a suggested one."
        )
        for artist in artists:
            if artist.sort_name == artist.name:
                typer.echo(f"  {artist.name}  (as is)")
            elif artist.sort_name:
                typer.echo(f"  {artist.name}  ->  {artist.sort_name}")
            elif artist.suggestion:
                typer.echo(f"  {artist.name}  ->  {artist.suggestion}  (suggested)")
            elif all_:
                typer.echo(f"  {artist.name}")
        if suggested:
            typer.echo("Use --accept-suggestions to take the suggested ones, or --set to choose.")


@app.command("layout")
def layout_command(
    ctx: typer.Context,
    set_: Annotated[str | None, typer.Option("--set", help="New layout template")] = None,
    library: LibraryOption = None,
) -> None:
    """Show or set the library's folder layout, a template of file paths.

    Fields: {albumartist} (the artist when missing, "Various Artists" on compilations),
    {albumartistsort} and {artistsort} (the sort name set with `aorg artists`, e.g.
    "Morissette, Alanis", else the name), {artist}, {album}, {title}, {genre}, {year},
    {track} (two digits) and {disc} ("2-" on albums with several discs, else empty). The
    default is "{albumartist}/{album}/{disc}{track} {title}". `aorg reorganize` moves
    files into it.
    """
    with _session(ctx) as session:
        lib = _library(session, library)
        if set_ is not None:
            try:
                lo.set_layout(session, lib, set_)
            except lo.LayoutError as e:
                raise typer.BadParameter(str(e)) from None
            session.commit()
        typer.echo(lo.get_layout(session, lib))


@app.command("reorganize")
def reorganize(
    ctx: typer.Context,
    path: Annotated[str | None, typer.Option(help="Only files under this path")] = None,
    limit: Annotated[int, typer.Option(help="Albums to propose in a batch")] = 20,
    propose: Annotated[
        bool, typer.Option("--propose", help="Create a changeset for a batch")
    ] = False,
    library: LibraryOption = None,
) -> None:
    """Move files into the library's layout (see `aorg layout`), album by album.

    Run the duplicate and album folder cleanups first: copies that would get the same
    path are conflicts and stay where they are.
    """
    with _session(ctx) as session:
        lib = _library(session, library)
        template = lo.get_layout(session, lib)
        plan = lo.plan(session, lib, template, path_prefix=path)
        typer.echo(f"Layout: {template}")
        typer.echo(
            f"{plan.files} files in {len(plan.albums)} albums can move; {plan.in_place} are "
            f"in place, {plan.case_only} differ only in letter case, {plan.incomplete} lack "
            f"a field the layout needs, {len(plan.conflicts)} conflict, {plan.pending} have "
            "changes waiting for review."
        )
        batch = plan.albums[:limit]
        if propose:
            if batch:
                changeset = lo.propose(
                    session, lib, batch, f"Move {len(batch)} albums into the layout"
                )
                typer.echo(
                    f"Changeset {changeset.id}: {len(changeset.changes)} moves. Review with "
                    f"`aorg changes show {changeset.id}`."
                )
            return
        for album in batch:
            typer.echo(f"  {album.folder}/  ({len(album.moves)} files)")
            for source in album.sources:
                typer.echo(f"      from {source}/")
        if len(plan.albums) > len(batch):
            typer.echo(f"  ... and {len(plan.albums) - len(batch)} more albums")
        if batch:
            typer.echo(f"Run with --propose to create a changeset for {len(batch)} albums.")


trash_app = typer.Typer(
    no_args_is_help=False,
    rich_markup_mode="markdown",
    help="Files moved to the trash by applied changes.",
    invoke_without_command=True,
)
app.add_typer(trash_app, name="trash")


def _trash_files(lib: Library) -> list[Path]:
    trash = Path(lib.root) / ch.TRASH_DIR
    return [p for p in trash.rglob("*") if p.is_file()] if trash.is_dir() else []


@trash_app.callback()
def trash_summary(ctx: typer.Context, library: LibraryOption = None) -> None:
    """Show what is in the trash. Undoing a changeset brings its files back."""
    if ctx.invoked_subcommand is not None:
        return
    with _session(ctx) as session:
        lib = _library(session, library)
        files = _trash_files(lib)
    size = sum(p.stat().st_size for p in files)
    typer.echo(f"{len(files)} files, {size / 1e6:.0f} MB in {Path(lib.root) / ch.TRASH_DIR}")


@trash_app.command("empty")
def trash_empty(
    ctx: typer.Context,
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask for confirmation")] = False,
    library: LibraryOption = None,
) -> None:
    """Delete the files in the trash for good. Changes that trashed them can no longer be
    undone."""
    with _session(ctx) as session:
        lib = _library(session, library)
    trash = Path(lib.root) / ch.TRASH_DIR
    files = _trash_files(lib)
    if not files:
        typer.echo("The trash is empty")
        return
    size = sum(p.stat().st_size for p in files)
    if not yes and not typer.confirm(
        f"Delete {len(files)} files ({size / 1e6:.0f} MB) in {trash} for good?"
    ):
        raise typer.Exit(1)
    shutil.rmtree(trash)
    typer.echo(f"Deleted {len(files)} files")
