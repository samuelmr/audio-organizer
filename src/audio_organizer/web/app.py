"""The web UI: browsing the catalog, editing metadata, reviewing changesets, playlists
and sync plans. Server-rendered pages with plain HTML forms, so every page works
without JavaScript.

create_app() takes the session factory, so the same app can serve a local catalog
(`aorg web`) or, later, a hosted one.
"""

import threading
import unicodedata
from collections.abc import Iterator
from math import ceil
from pathlib import Path, PurePosixPath
from typing import Annotated
from urllib.parse import quote, urlencode, urlsplit

from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import String, func, or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import album_edit as ae
from .. import album_merge as am
from .. import artists as art
from .. import changes as ch
from .. import coverart, covers, gnudb, loudness, tags
from .. import duplicates as du
from .. import encodings as rec
from .. import layout as lo
from .. import neighbours as nb
from .. import operations as ops
from .. import playlist_files as pf
from .. import playlist_store as ps
from .. import playlists as pl
from .. import ratings as rt
from .. import sync as sy
from .. import sync_jobs as jobs
from .. import writing as wr
from ..models import (
    AudioFile,
    Change,
    Changeset,
    CoverImage,
    EffectiveMetadata,
    Library,
    Operation,
    Playlist,
    PlaylistFileBackup,
    SyncJob,
    SyncTarget,
    TagSnapshot,
)
from ..provenance import resolve
from ..tags import FIELDS
from ..tracks import ALBUM_FIELDS, album_files, album_filter, present, shared_values

HERE = Path(__file__).parent
# The track page's sections, in the order shown; every tag field is in one of them
FIELD_GROUPS = (
    ("Track", ("title", "artist", "album", "albumartist", "tracknumber", "tracktotal",
               "discnumber", "disctotal", "date", "originaldate", "genre", "compilation")),
    ("Sorting", ("albumartistsort", "artistsort")),
    ("Credits", ("composer", "conductor", "grouping", "subtitle", "language")),
    ("Mood and rating", ("mood", "rating", "tempo", "situation", "preference")),
    ("Loudness (ReplayGain)", ("replaygain_track_gain", "replaygain_track_peak",
                               "replaygain_album_gain", "replaygain_album_peak")),
    ("Identification", ("discid",)),
)  # fmt: skip
assert sorted(f for _, names in FIELD_GROUPS for f in names) == sorted(FIELDS)
FIELD_LABELS = {
    "title": "Title", "artist": "Artist", "album": "Album", "albumartist": "Album artist",
    "tracknumber": "Track", "tracktotal": "Tracks", "discnumber": "Disc", "disctotal": "Discs",
    "date": "Year or date", "originaldate": "Original year", "genre": "Genre",
    "compilation": "Compilation (1)", "albumartistsort": "Album artist sort name",
    "artistsort": "Artist sort name", "composer": "Composer", "conductor": "Conductor",
    "grouping": "Grouping", "subtitle": "Subtitle", "language": "Language (e.g. fin, eng)",
    "mood": "Mood", "rating": "Rating", "tempo": "Tempo", "situation": "Situation",
    "preference": "Preference", "replaygain_track_gain": "Track gain",
    "replaygain_track_peak": "Track peak", "replaygain_album_gain": "Album gain",
    "replaygain_album_peak": "Album peak", "discid": "gnudb disc ID",
    "cover": "Cover", "trash": "Trash",
    "cover-file": "Folder image",
}  # fmt: skip
PAGE_SIZE = 50
# An album or folder with more tracks than this is not offered for editing at once: it is
# more likely a mixed folder or a common album name ("Greatest Hits") than one album
MAX_ALBUM_EDIT = 200
# Starlette parses a posted form with at most 1000 fields; each page's batch form posts
# roughly one field per row, so a batch above this would fail to submit. Clamp every
# "limit" so a hand-edited URL can't ask for more than the form can post back.
MAX_BATCH = 1000
HISTORY_PAGE = 50
M = EffectiveMetadata
SORTS = {
    "artist": (M.artist, M.album, M.discnumber, M.tracknumber),
    "title": (M.title, M.artist),
    "album": (M.album, M.albumartist, M.discnumber, M.tracknumber),
    "year": (M.year, M.artist, M.album, M.discnumber, M.tracknumber),
    "genre": (M.genre, M.artist),
    "path": (AudioFile.path,),
    "added": (AudioFile.first_seen,),
}
FILTERS = {
    "all": None,
    "disputed": EffectiveMetadata.disputed,
    "no-album": EffectiveMetadata.album.is_(None),
    "no-title": EffectiveMetadata.title.is_(None),
}


class ActionRunning(Exception):
    """A long-running action was asked for while another one is under way."""


def create_app(session_factory: sessionmaker[Session]) -> FastAPI:
    previews = jobs.Previews(session_factory)
    app = FastAPI(title="audio-organizer", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["duration"] = _duration
    templates.env.globals["url"] = _url
    templates.env.globals["selection_url"] = selection_url
    templates.env.globals["format_labels"] = FORMAT_LABELS
    templates.env.globals["shared_names"] = pl.shared_names
    templates.env.globals["display_name"] = pl.display_name
    templates.env.globals["asset"] = _asset
    templates.env.globals["move_source"] = ch.move_source
    templates.env.globals["move_extras"] = ch.move_extras
    templates.env.globals["is_rename_in_place"] = ch.is_rename_in_place
    templates.env.globals["retag_fields"] = ch.retag_fields
    templates.env.globals["field_labels"] = FIELD_LABELS
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def get_session() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    SessionDep = Depends(get_session)
    running = threading.Lock()

    def exclusive() -> Iterator[None]:
        """Let one long-running action (a lookup, proposal, apply or undo) run at a time,
        so that a second click, another tab or another page cannot start a parallel run
        that works on the same files and catalog. The pages disable their buttons while
        one runs (ui.js); this is the check that holds when they could not."""
        if not running.acquire(blocking=False):
            raise ActionRunning
        try:
            yield
        finally:
            running.release()

    Exclusive = Depends(exclusive)

    def render(request: Request, template: str, status_code: int = 200, **context):
        context.setdefault("message", request.query_params.get("msg"))
        href = request.query_params.get("msg_href", "")
        label = request.query_params.get("msg_link", "")
        # Only paths on this site, so a crafted link cannot send the reader elsewhere
        if label and href.startswith("/") and not href.startswith(("//", "/\\")):
            context.setdefault("message_link", (href, label))
        if context.get("library") is None:
            # Pages outside a library still link to its tracks and playlists in the menu
            # when there is only one library
            with session_factory() as session:
                libraries = session.scalars(select(Library).limit(2)).all()
            context["library"] = libraries[0] if len(libraries) == 1 else None
        return templates.TemplateResponse(request, template, context, status_code=status_code)

    @app.middleware("http")
    async def refuse_cross_site_posts(request: Request, call_next):
        """A page on another site could make the browser post a form to this one; such
        requests carry that site's Origin (or Referer) and are refused."""
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            source = request.headers.get("origin") or request.headers.get("referer")
            if source and urlsplit(source).netloc != request.headers.get("host"):
                return PlainTextResponse("Cross-site request refused", status_code=403)
        return await call_next(request)

    @app.exception_handler(ActionRunning)
    async def action_running(request: Request, exc: ActionRunning):
        return HTMLResponse(
            "<!DOCTYPE html><title>Busy</title>"
            "<p>Another long-running action is still in progress. Nothing was changed. "
            "Wait for it to finish, then go back and try again.</p>",
            status_code=409,
            headers={"Retry-After": "5"},
        )

    @app.exception_handler(OperationalError)
    async def database_busy(request: Request, exc: OperationalError):
        """Another process (a scan or sync from the command line) holds the write lock:
        say so and ask to retry, instead of a server error."""
        if "database is locked" not in str(exc):
            raise exc
        return HTMLResponse(
            "<!DOCTYPE html><title>Catalog busy</title>"
            "<p>The catalog is busy: another aorg process (a scan or sync?) is writing to it. "
            "Nothing was changed. Wait for it to finish, then go back and try again.</p>",
            status_code=503,
            headers={"Retry-After": "10"},
        )

    @app.exception_handler(StarletteHTTPException)
    async def batch_too_large(request: Request, exc: StarletteHTTPException):
        """A posted batch form has one field per row, and Starlette turns going over its
        1000-field cap into a plain HTTPException raised before any route code runs (so
        it isn't the fastapi.HTTPException subclass other handlers below raise); the
        page's own limit keeps a batch under that, but a hand-edited URL or form can
        still ask for more. Say so instead of a bare JSON error."""
        if exc.status_code != 400 or "Too many fields" not in str(exc.detail):
            return await http_exception_handler(request, exc)
        back = request.headers.get("referer", "")
        # The cross-site-post check above already confirms a present referer is same-site
        go_back = f' <a href="{back}">Go back</a> and choose a smaller batch.' if back else ""
        return HTMLResponse(
            "<!DOCTYPE html><title>Batch too large</title>"
            f"<p>This batch has too many rows to submit at once (at most {MAX_BATCH}). "
            f"Nothing was changed.{go_back}</p>",
            status_code=400,
        )

    # ---- libraries and tracks ------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, session: Session = SessionDep):
        rows = session.execute(
            select(Library, func.count(AudioFile.id))
            .outerjoin(AudioFile, present(Library.id) & (AudioFile.library_id == Library.id))
            .group_by(Library.id)
            .order_by(Library.name)
        ).all()
        if len(rows) == 1:
            return RedirectResponse(f"/libraries/{rows[0][0].name}", status_code=303)
        return render(request, "index.html", libraries=rows)

    @app.get("/libraries/{name}", response_class=HTMLResponse)
    def library(
        request: Request,
        name: str,
        q: str = "",
        sort: str = "",
        show: str = "all",
        album: str | None = None,
        albumartist: str | None = None,
        artist: str | None = None,
        folder: str | None = None,
        page: int = 1,
        edit: bool = False,
        session: Session = SessionDep,
    ):
        """The tracks of a library: searched, filtered to one album, artist or folder,
        and sorted. Sorted by album, the tracks come in album order. With edit, the form
        editing all of an album's tracks is open."""
        lib = _library(session, name)
        query = select(AudioFile, M).join(M, M.file_id == AudioFile.id).where(present(lib.id))
        for word in q.split():
            pattern = f"%{word}%"
            query = query.where(
                or_(
                    M.artist.ilike(pattern),
                    M.album.ilike(pattern),
                    M.title.ilike(pattern),
                    AudioFile.path.ilike(pattern),
                )
            )
        if FILTERS.get(show) is not None:
            query = query.where(FILTERS[show])
        query = query.where(*album_filter(album, albumartist, folder))
        if artist is not None:
            query = query.where(func.lower(M.artist) == artist.lower())
        sort = sort if sort in SORTS else ("album" if album or folder else "artist")
        total = session.scalar(select(func.count()).select_from(query.subquery()))
        pages = max(1, ceil(total / PAGE_SIZE))
        page = min(max(page, 1), pages)
        rows = session.execute(
            query.order_by(*_order(SORTS[sort]), AudioFile.path)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        ).all()
        params = {"q": q, "show": show, "album": album, "albumartist": albumartist,
                  "artist": artist, "folder": folder}  # fmt: skip
        # Where the shown album, artist or folder lives on disk (all pages of it)
        folder_links = (
            _folder_links(lib, session.scalars(query.with_only_columns(AudioFile.path)).all())
            if album is not None or artist is not None or folder is not None
            else []
        )
        # The folder gnudb can be asked about: the one shown, or the one holding the album
        folders = {str(PurePosixPath(file.path).parent) for file, _ in rows}
        album_folder = (
            folder.strip("/") if folder is not None
            else folders.pop() if album is not None and total == len(rows) and len(folders) == 1
            else None
        )  # fmt: skip
        # An album or folder shown whole can be edited at once: its shared values
        album_edit = (
            shared_values(session, album_files(session, lib.id, album, albumartist, folder),
                          ALBUM_FIELDS)
            if (album is not None or folder is not None) and artist is None and not q
            and show == "all" and 0 < total <= MAX_ALBUM_EDIT
            else None
        )  # fmt: skip
        return render(
            request, "library.html", library=lib, rows=rows, total=total, sort=sort,
            album_folder=album_folder, folder_links=folder_links, album_edit=album_edit,
            edit=edit,
            page=page, pages=pages, sorts=list(SORTS), filters=list(FILTERS), params=params,
            **params,
        )  # fmt: skip

    @app.get("/libraries/{name}/albums", response_class=HTMLResponse)
    def albums(
        request: Request, name: str, q: str = "", page: int = 1, session: Session = SessionDep
    ):
        """The library's albums by their (effective) album and album artist tags."""
        lib = _library(session, name)
        query = (
            select(
                func.min(M.album).label("album"),
                func.min(M.albumartist).label("albumartist"),
                func.count().label("tracks"),
                func.count(func.distinct(M.artist)).label("artists"),
                func.min(M.artist).label("artist"),
                func.min(M.year).label("year"),
                func.min(AudioFile.id).label("file_id"),  # for the cover
            )
            .join(AudioFile, M.file_id == AudioFile.id)
            .where(present(lib.id), M.album.is_not(None))
            .group_by(func.lower(M.album), func.lower(M.albumartist))
        )
        for word in q.split():
            pattern = f"%{word}%"
            query = query.where(
                or_(M.album.ilike(pattern), M.albumartist.ilike(pattern), M.artist.ilike(pattern))
            )
        total = session.scalar(select(func.count()).select_from(query.subquery()))
        pages = max(1, ceil(total / PAGE_SIZE))
        page = min(max(page, 1), pages)
        rows = session.execute(
            query.order_by(func.lower(func.min(M.album)), func.lower(func.min(M.albumartist)))
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        ).all()
        # The folders of each album shown, to tell copies of one album apart
        keys = {(r.album.lower(), (r.albumartist or "").lower()) for r in rows}
        paths: dict[tuple[str, str], list[str]] = {}
        for album, albumartist, path in session.execute(
            select(
                func.lower(M.album), func.lower(func.coalesce(M.albumartist, "")), AudioFile.path
            )
            .join(AudioFile, M.file_id == AudioFile.id)
            .where(present(lib.id), func.lower(M.album).in_({k[0] for k in keys}))
        ):
            if (album, albumartist) in keys:
                paths.setdefault((album, albumartist), []).append(path)
        folders = {
            (r.album, r.albumartist): _folder_links(
                lib, paths.get((r.album.lower(), (r.albumartist or "").lower()), [])
            )
            for r in rows
        }
        return render(
            request, "albums.html", library=lib, rows=rows, total=total, q=q, page=page,
            pages=pages, folders=folders, max_album_edit=MAX_ALBUM_EDIT,
        )  # fmt: skip

    @app.get("/tracks/{file_id}", response_class=HTMLResponse)
    def track(request: Request, file_id: int, session: Session = SessionDep):
        file = _file(session, file_id)
        resolved = resolve(file.values)
        copies = []
        if file.audio_hash:
            copies = session.scalars(
                select(AudioFile)
                .where(present(file.library_id), AudioFile.audio_hash == file.audio_hash)
                .where(AudioFile.id != file.id)
                .order_by(AudioFile.path)
            ).all()
        snapshot = session.scalar(
            select(TagSnapshot)
            .where(TagSnapshot.file_id == file.id)
            .order_by(TagSnapshot.taken_at)
            .limit(1)
        )
        manual = {fv.field: fv.value for fv in file.values if fv.source == "manual"}
        extra = sorted(set(resolved) - set(FIELDS))
        path = Path(file.library.root) / file.path
        on_disk = path.is_file()
        measure_error = next(
            (v.value for v in file.values if v.source == "analysis" and v.field == loudness.FAILED),
            None,
        )
        folder_cover = sy.find_cover(path.parent) if on_disk else None
        return render(
            request, "track.html", file=file, resolved=resolved, groups=FIELD_GROUPS,
            extra=extra, manual=manual, copies=copies, snapshot=snapshot,
            unwritten=wr.unwritten(file.values), on_disk=on_disk, measure_error=measure_error,
            has_cover=on_disk and covers.embedded(path) is not None,
            folder_cover=folder_cover.name if folder_cover else None,
            id3_version=tags.id3_version(path) if on_disk else None,
        )  # fmt: skip

    @app.post("/libraries/{name}/gnudb", dependencies=[Exclusive])
    async def gnudb_lookup(request: Request, name: str, session: Session = SessionDep):
        """Look up one album folder in gnudb. It waits for the answer, a few seconds."""
        lib = _library(session, name)
        form = await request.form()
        folder = str(form.get("folder", "")).strip("/")
        back = _url(f"/libraries/{lib.name}", folder=folder or None)
        if not gnudb.has_email():
            return _redirect(back, "Set AORG_GNUDB_EMAIL to your email address first")
        try:
            results = gnudb.lookup_folder(session, lib, folder)
        except gnudb.GnudbError as e:
            return _redirect(back, f"gnudb: {e}")
        if not results:
            return _redirect(
                back,
                "Not an album folder gnudb can be asked about: it needs all the tracks of a "
                "single disc, numbered 1..n, with an artist and album title",
            )
        found = [album for album, entry in results if entry]
        if not found:
            return _redirect(back, "gnudb has no matching entry for this album")
        return _redirect(
            back,
            f"Found in gnudb: {', '.join(a.album for a in found)}; not yet in the files.",
            link=(
                _url(f"/libraries/{lib.name}/pending", path=folder + "/", prefer_gnudb="true"),
                "Review what gnudb found",
            ),
        )

    @app.post("/libraries/{name}/coverart", dependencies=[Exclusive])
    async def coverart_lookup(request: Request, name: str, session: Session = SessionDep):
        """Look up the missing cover of one album folder online. It waits for the answer."""
        lib = _library(session, name)
        form = await request.form()
        folder = str(form.get("folder", "")).strip("/")
        back = _url(f"/libraries/{lib.name}", folder=folder or None)
        try:
            results = coverart.lookup_folder(session, lib, folder)
        except coverart.CoverArtError as e:
            return _redirect(back, f"Cover Art Archive: {e}")
        if not results:
            return _redirect(
                back,
                "No album here needs a cover: it has one (embedded, in the folder or found "
                "online), or its tracks lack artist and album",
            )
        found = [album for album, cover in results if cover]
        if not found:
            return _redirect(back, "No cover found online for this album")
        return _redirect(
            back,
            f"Found a cover for {', '.join(a.album for a in found)}.",
            link=(
                _url(f"/libraries/{lib.name}/covers", path=folder + "/"),
                "Embed it on the Covers page",
            ),
        )

    @app.post("/tracks/{file_id}/replaygain", dependencies=[Exclusive])
    def measure_replaygain(file_id: int, session: Session = SessionDep):
        file = _file(session, file_id)
        try:
            values = loudness.measure_file(session, file)
        except loudness.LoudnessError as e:
            return _redirect(f"/tracks/{file.id}", str(e))
        session.commit()
        return _redirect(
            f"/tracks/{file.id}",
            f"Measured: gain {values['replaygain_track_gain']}, peak "
            f"{values['replaygain_track_peak']}; not yet in the file",
        )

    @app.get("/tracks/{file_id}/cover")
    def track_cover(file_id: int, folder: bool = False, session: Session = SessionDep):
        """The file's embedded cover, or with folder=1 the cover image in its folder."""
        file = _file(session, file_id)
        path = Path(file.library.root) / file.path
        if folder:
            image = sy.find_cover(path.parent)
            if image is None:
                raise HTTPException(404, "No cover image in the folder")
            return FileResponse(image, media_type=covers.IMAGE_TYPES[image.suffix.lower()])
        found = covers.embedded(path) if path.is_file() else None
        if found is None:
            raise HTTPException(404, "No embedded cover")
        mime, data = found
        return Response(data, media_type=mime)

    @app.get("/cover-images/{sha1}")
    def cover_image(sha1: str, session: Session = SessionDep):
        """A cover kept in the catalog: found online, or replaced by a change."""
        image = session.get(CoverImage, sha1)
        if image is None:
            raise HTTPException(404, "No such cover image")
        return Response(
            image.data, media_type=image.mime, headers={"Cache-Control": "max-age=31536000"}
        )

    @app.get("/tracks/{file_id}/art")
    def track_art(request: Request, file_id: int, session: Session = SessionDep):
        """The cover to show beside the track: the embedded one, else its folder's. Lists
        show one per album, so the browser revalidates by an ETag of the audio file and the
        folder image instead of reading the covers again."""
        file = _file(session, file_id)
        path = Path(file.library.root) / file.path
        if not path.is_file():
            raise HTTPException(404, "The file is not in the library")
        image = sy.find_cover(path.parent)
        stats = [path.stat()] + ([image.stat()] if image else [])
        etag = '"' + "-".join(f"{s.st_mtime_ns:x}.{s.st_size:x}" for s in stats) + '"'
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        found = covers.embedded(path)
        if found is not None:
            mime, data = found
            return Response(data, media_type=mime, headers=headers)
        if image is not None:
            return FileResponse(
                image, media_type=covers.IMAGE_TYPES[image.suffix.lower()], headers=headers
            )
        raise HTTPException(404, "No cover")

    @app.post("/tracks/{file_id}/cover", dependencies=[Exclusive])
    def embed_cover(file_id: int, session: Session = SessionDep):
        """Decide to embed the folder's cover image into the file."""
        file = _file(session, file_id)
        path = Path(file.library.root) / file.path
        image = sy.find_cover(path.parent)
        if image is None:
            return _redirect(f"/tracks/{file.id}", "There is no cover image in the folder")
        relative = image.relative_to(file.library.root).as_posix()
        ops.record(
            session, file.library_id, "covers", f"Embed the cover of {file.path}", [],
            desires=[ops.Desire(file.id, "embed", {"image": relative})],
        )  # fmt: skip
        session.commit()
        pending = _url(f"/libraries/{file.library.name}/pending", file=file.id)
        return _redirect(pending, "Decided; review the cover and write it into the file")

    @app.get("/tracks/{file_id}/audio")
    def track_audio(file_id: int, session: Session = SessionDep):
        """The track's audio, for listening in the browser (with range requests, so
        seeking works). A trashed file is played from the trash. Only files in the
        catalog can be served, by id, so no other file can be reached through this."""
        file = _file(session, file_id)
        root = Path(file.library.root)
        path = root / file.path
        if not path.is_file():
            trashed = session.scalars(
                select(Change)
                .where(Change.file_id == file.id, Change.kind == "trash")
                .where(Change.status == "applied")
                .order_by(Change.id.desc())
            ).first()
            path = root / trashed.undo["trash"] if trashed and trashed.undo else path
        if not path.is_file():
            raise HTTPException(404, "The file is not on disk")
        media_type = sy.CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        return FileResponse(path, media_type=media_type)

    @app.post("/tracks/{file_id}/edit")
    async def edit_track(request: Request, file_id: int, session: Session = SessionDep):
        """Store the edited fields as manual values, to be written into the file with its
        other pending changes. An emptied field drops its manual value, falling back to
        tags and path."""
        file = _file(session, file_id)
        form = await request.form()
        resolved = resolve(file.values)
        manual = {fv.field: fv.value for fv in file.values if fv.source == "manual"}
        values: dict[str, str | None] = {}
        for name in FIELDS:
            submitted = str(form.get(name, "")).strip()
            current = resolved[name].value if name in resolved else ""
            if submitted and submitted != current:
                values[name] = submitted
            elif not submitted and name in manual:
                values[name] = None
        if not values:
            return _redirect(f"/tracks/{file.id}", "Nothing changed")
        if not wr.store_edit(session, [file], values, f"Edit {file.path}"):
            return _redirect(f"/tracks/{file.id}", "Saved; the file's tags already match")
        pending = _url(f"/libraries/{file.library.name}/pending", file=file.id)
        return _redirect(
            f"/tracks/{file.id}", "Saved; not yet in the file.", link=(pending, "Review and write")
        )

    @app.post("/libraries/{name}/edit-album")
    async def edit_album(request: Request, name: str, session: Session = SessionDep):
        """Edit all tracks of an album or folder at once: a field filled in with a new
        value is saved as a manual value of every track, to be written with the files'
        other pending changes. An empty field (or one left as it was) is not changed."""
        lib = _library(session, name)
        form = await request.form()
        chosen = {
            key: (str(form[f"of_{key}"]) if f"of_{key}" in form else None)
            for key in ("album", "albumartist", "folder")
        }
        back = _url(f"/libraries/{lib.name}", **chosen)
        if chosen["album"] is None and chosen["folder"] is None:
            raise HTTPException(400, "Choose an album or a folder")
        files = album_files(session, lib.id, **chosen)
        if not files:
            return _redirect(back, "No tracks found")
        if len(files) > MAX_ALBUM_EDIT:
            return _redirect(back, f"Too many tracks to edit at once (over {MAX_ALBUM_EDIT})")
        shared = shared_values(session, files, ALBUM_FIELDS)
        values: dict[str, str | None] = {}
        for field in ALBUM_FIELDS:
            submitted = str(form.get(field, "")).strip()
            if submitted and submitted != shared[field]:
                values[field] = submitted
        if not values:
            return _redirect(back, "Nothing changed")
        folder = chosen["folder"].strip("/") if chosen["folder"] is not None else None
        edit = ae.edit(
            session, lib, files, values, ae.description(chosen["album"], folder),
            move_files=form.get("move_files") == "true",
        )  # fmt: skip
        pending = None
        if edit.pending or edit.moves:
            artist, album = wr.group_of((edit.pending or [edit.moves[0].file])[0])
            pending = selection_url(f"/libraries/{lib.name}/pending", artist=artist, album=album)
        if pending is None:
            return _redirect(back, "Saved; the files' tags already match")
        message = f"Saved; {len(edit.pending)} files are not yet written"
        if edit.moves:
            message += f", and {len(edit.moves)} are to move to renamed folders"
        elif edit.plan is not None:
            message += "; no folders to rename"
        left = len(edit.plan.conflicts) + edit.plan.case_only if edit.plan else 0
        if left:
            message += (
                f"; {left} files stay where they are: their new path is taken, or "
                "differs only in letter case"
            )
        return _redirect(pending, message + ". Review and write them here.")

    # ---- duplicates ----------------------------------------------------------------------

    @app.get("/libraries/{name}/duplicates", response_class=HTMLResponse)
    def duplicates(
        request: Request,
        name: str,
        path: str = "",
        limit: int = 50,
        compilations: bool = False,
        session: Session = SessionDep,
    ):
        lib = _library(session, name)
        limit = _clamp_batch(limit)
        found = du.survey(session, lib, path_prefix=path or None, include_compilations=compilations)
        return render(
            request, "duplicates.html", library=lib, found=found,
            batch=found.proposals[:limit], path=path, limit=limit,
            compilations=compilations,
        )  # fmt: skip

    @app.post("/libraries/{name}/duplicates", dependencies=[Exclusive])
    async def propose_duplicates(request: Request, name: str, session: Session = SessionDep):
        """Propose the groups shown on the page (one "keep-<audio hash>" field each, the
        id of the copy to keep), so the batch is what the user saw and chose."""
        lib = _library(session, name)
        form = await request.form()
        path = str(form.get("path", ""))
        keep_ids = {
            key.removeprefix("keep-"): int(value)
            for key, value in form.multi_items()
            if key.startswith("keep-") and str(value).isdigit()
        }
        found = du.survey(
            session, lib, path_prefix=path or None,
            include_compilations=form.get("compilations") == "true",
        )  # fmt: skip
        shown = [p for p in found.proposals if p.keep.file.audio_hash in keep_ids]
        batch = du.choose(shown, keep_ids)
        if not batch:
            return _redirect(f"/libraries/{lib.name}/duplicates", "Nothing to propose")
        where = f" in {path}" if path else ""
        du.decide(session, lib, batch, f"Trash loose copies of {len(batch)} tracks{where}")
        return _redirect(
            _url(f"/libraries/{lib.name}/pending", path=path or None),
            "Decided; review and write the trashing here",
            link=(f"/libraries/{lib.name}/history", "Undo on the History page"),
        )

    # ---- encodings ------------------------------------------------------------------

    @app.get("/libraries/{name}/encodings", response_class=HTMLResponse)
    def encodings(
        request: Request,
        name: str,
        path: str = "",
        limit: int = 50,
        replace: bool = False,
        compilations: bool = False,
        session: Session = SessionDep,
    ):
        lib = _library(session, name)
        limit = _clamp_batch(limit)
        found = rec.survey(
            session, lib, path_prefix=path or None, include_compilations=compilations
        )
        return render(
            request, "encodings.html", library=lib, found=found,
            batch=rec.batch(found.proposals, limit, replace=replace), path=path,
            limit=limit, replace=replace, compilations=compilations,
        )  # fmt: skip

    @app.post("/libraries/{name}/encodings", dependencies=[Exclusive])
    async def propose_encodings(request: Request, name: str, session: Session = SessionDep):
        """Propose the tracks shown on the page (one "keep-<id kept by default>" field
        each, the id of the file to keep), so the batch is what the user saw and chose."""
        lib = _library(session, name)
        form = await request.form()
        path = str(form.get("path", ""))
        keep_ids = {
            key.removeprefix("keep-"): int(value)
            for key, value in form.multi_items()
            if key.startswith("keep-") and str(value).isdigit()
        }
        found = rec.survey(
            session, lib, path_prefix=path or None,
            include_compilations=form.get("compilations") == "true",
        )  # fmt: skip
        shown = [p for p in found.proposals if str(p.keep.file.id) in keep_ids]
        batch = rec.choose(shown, keep_ids)
        if not batch:
            return _redirect(f"/libraries/{lib.name}/encodings", "Nothing to propose")
        where = f" in {path}" if path else ""
        changeset = rec.propose(
            session, batch, f"Keep the best encoding of {len(batch)} tracks{where}"
        )
        return _redirect(f"/changes/{changeset.id}", "Proposed; review and approve the changes")

    # ---- loose tracks into their albums ---------------------------------------------------

    @app.get("/libraries/{name}/neighbours", response_class=HTMLResponse)
    def neighbours(
        request: Request, name: str, path: str = "", limit: int = 50, session: Session = SessionDep
    ):
        lib = _library(session, name)
        limit = _clamp_batch(limit)
        found = nb.survey(session, lib, path_prefix=path or None)
        return render(
            request, "neighbours.html", library=lib, found=found,
            batch=nb.batch(found.albums, limit), path=path, limit=limit,
        )  # fmt: skip

    @app.post("/libraries/{name}/neighbours", dependencies=[Exclusive])
    async def propose_neighbours(request: Request, name: str, session: Session = SessionDep):
        """Propose the checked albums of the page (one "folder" field each)."""
        lib = _library(session, name)
        form = await request.form()
        path = str(form.get("path", ""))
        folders = set(form.getlist("folder"))
        found = nb.survey(session, lib, path_prefix=path or None)
        chosen = [a for a in found.albums if a.folder in folders]
        if not chosen:
            return _redirect(f"/libraries/{lib.name}/neighbours", "Nothing to propose")
        count = sum(len(a.moves) for a in chosen)
        changeset = nb.propose(
            session, chosen, f"Put {count} loose tracks into {len(chosen)} albums"
        )
        return _redirect(f"/changes/{changeset.id}", "Proposed; review and approve the changes")

    @app.get("/libraries/{name}/album-merges", response_class=HTMLResponse)
    def album_merges(
        request: Request, name: str, path: str = "", limit: int = 20, session: Session = SessionDep
    ):
        lib = _library(session, name)
        limit = _clamp_batch(limit)
        found = am.survey(session, lib, path_prefix=path or None)
        return render(
            request, "album_merges.html", library=lib, found=found,
            batch=am.batch(found.merges, limit),
            others=[m for m in found.merges if not m.batchable], path=path, limit=limit,
        )  # fmt: skip

    @app.post("/libraries/{name}/album-merges", dependencies=[Exclusive])
    def propose_album_merges(
        name: str,
        path: Annotated[str, Form()] = "",
        limit: Annotated[int, Form()] = 20,
        source: Annotated[str, Form()] = "",
        target: Annotated[str, Form()] = "",
        session: Session = SessionDep,
    ):
        """A batch of merges, or with source and target, one merge in that direction."""
        lib = _library(session, name)
        if source and target:
            try:
                merges = [am.plan_folders(session, lib, source, target)]
            except ValueError as e:
                raise HTTPException(400, str(e)) from None
            description = f"Merge {source} into {target}"
        else:
            found = am.survey(session, lib, path_prefix=path or None)
            merges = am.batch(found.merges, _clamp_batch(limit))
            description = f"Merge {len(merges)} album folders"
        if not any(m.trash or m.move for m in merges):
            return _redirect(f"/libraries/{lib.name}/album-merges", "Nothing to merge")
        changeset = am.propose(session, merges, description)
        return _redirect(f"/changes/{changeset.id}", "Proposed; review and approve the changes")

    # ---- artists -------------------------------------------------------------------------

    def _artists(session: Session, lib: Library, show: str, q: str) -> list:
        listed = art.album_artists(session, lib)
        if show == "suggested":
            listed = [a for a in listed if a.suggestion]
        elif show == "named":
            listed = [a for a in listed if a.sort_name]
        words = [_fold_text(w) for w in q.split()]
        if words:
            listed = [
                a for a in listed
                if all(w in _fold_text(f"{a.name} {a.sort_name or ''}") for w in words)
            ]  # fmt: skip
        return listed

    @app.get("/libraries/{name}/artists", response_class=HTMLResponse)
    def artists_page(
        request: Request,
        name: str,
        show: str = "suggested",
        q: str = "",
        page: int = 1,
        session: Session = SessionDep,
    ):
        lib = _library(session, name)
        listed = _artists(session, lib, show, q)
        pages = max(1, ceil(len(listed) / 100))
        page = min(max(page, 1), pages)
        everyone = art.album_artists(session, lib)
        return render(
            request, "artists.html", library=lib, artists=listed[(page - 1) * 100 : page * 100],
            total=len(listed), show=show, q=q, page=page, pages=pages,
            named=sum(1 for a in everyone if a.sort_name),
            suggested=sum(1 for a in everyone if a.suggestion), count=len(everyone),
        )  # fmt: skip

    @app.post("/libraries/{name}/artists", dependencies=[Exclusive])
    async def set_artist_sort_names(request: Request, name: str, session: Session = SessionDep):
        """Set one artist's sort name (artist, sort_name), or with accept=all, use the
        suggestions of the artists listed with the given filter."""
        lib = _library(session, name)
        form = await request.form()
        show, q = str(form.get("show", "suggested")), str(form.get("q", ""))
        if form.get("accept") == "all":
            count = art.accept_suggestions(session, lib, _artists(session, lib, show, q))
            message = f"Accepted {count} suggested sort names"
        else:
            artist = str(form.get("artist", "")).strip()
            if not artist:
                raise HTTPException(400, "No artist given")
            art.set_sort_name(session, lib, artist, str(form.get("sort_name", "")))
            message = f"Saved the sort name of {artist}"
        session.commit()
        return _redirect(_url(f"/libraries/{lib.name}/artists", show=show, q=q), message)

    # ---- writing tags --------------------------------------------------------------------

    def _only(value: str) -> list[str] | None:
        return [f.strip() for f in value.split(",") if f.strip()] or None

    @app.get("/libraries/{name}/write-tags")
    def write_tags_moved(request: Request, name: str):
        """The Write tags page became the Pending page; old links still work."""
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"/libraries/{name}/pending{query}", status_code=301)

    def _selection(
        path: str, artist: str | None, album: str | None, file_ids: list[int] | None,
        only: str, prefer_gnudb: bool,
    ) -> wr.Selection:  # fmt: skip
        return wr.Selection(
            path=path or None, artist=artist, album=album, file_ids=file_ids,
            only=_only(only), prefer=("gnudb",) if prefer_gnudb else (),
        )  # fmt: skip

    @app.get("/libraries/{name}/pending", response_class=HTMLResponse)
    def pending_page(
        request: Request,
        name: str,
        path: str = "",
        artist: str | None = None,
        album: str | None = None,
        file: int | None = None,
        only: str = "",
        limit: int = 100,
        prefer_gnudb: bool = False,
        session: Session = SessionDep,
    ):
        """What is waiting to be written into the files: the albums that have pending
        changes, and for the chosen ones what would change in each file."""
        lib = _library(session, name)
        limit = _clamp_batch(limit)
        selection = _selection(path, artist, album, [file] if file else None, only, prefer_gnudb)
        found = wr.survey(session, lib, selection, limit)
        # The list of albums is for choosing; once one is chosen it is in the heading
        chosen = artist is not None or album is not None or file is not None
        if chosen:
            groups = []
        else:
            groups = found.groups if prefer_gnudb else wr.groups(session, lib, selection)
        unmeasured = len(loudness.unmeasured(session, lib))
        unmeasurable = loudness.failed(session, lib)
        return render(
            request, "pending.html", library=lib, found=found, groups=groups,
            path=path, artist=artist, album=album, file=file, only=only, limit=limit,
            prefer_gnudb=prefer_gnudb, unmeasured=unmeasured, unmeasurable=unmeasurable,
        )  # fmt: skip

    @app.post("/libraries/{name}/pending", response_class=HTMLResponse, dependencies=[Exclusive])
    def write_pending(
        request: Request,
        name: str,
        action: Annotated[str, Form()],
        file_ids: Annotated[list[int] | None, Form()] = None,
        path: Annotated[str, Form()] = "",
        artist: Annotated[list[str] | None, Form()] = None,
        album: Annotated[list[str] | None, Form()] = None,
        only: Annotated[str, Form()] = "",
        prefer_gnudb: Annotated[bool, Form()] = False,
        id3_version: Annotated[str, Form()] = "",
        session: Session = SessionDep,
    ):
        """Write the ticked files' pending changes (each file once, with all of them), drop
        the user's edits among them, or (with prefer_gnudb) accept gnudb's values in them
        as the user's edits. The changes are worked out again here, so a file is written
        as it is pending now.

        The artist and album are lists because FastAPI reads an empty form field as missing,
        while artist="" chooses the tracks without an artist; a list keeps the empty value."""
        lib = _library(session, name)
        artist, album = (values[0] if values else None for values in (artist, album))
        if action not in ("write", "discard", "accept"):
            raise HTTPException(400, f"Unknown action {action}")
        if (action == "accept") != prefer_gnudb and action != "discard":
            raise HTTPException(400, "gnudb's values are accepted first, then written")
        selection = _selection(path, artist, album, file_ids or [], only, prefer_gnudb)
        found = wr.survey(session, lib, selection, MAX_BATCH)
        back = selection_url(
            f"/libraries/{lib.name}/pending", path=path or None, artist=artist, album=album,
            only=only or None, prefer_gnudb="true" if prefer_gnudb else None,
        )  # fmt: skip
        if not found.files:
            return _redirect(back, "Nothing to write: no ticked file has pending changes")
        history = (f"/libraries/{lib.name}/history", "Undo on the History page")
        if action == "discard":
            dropped, kept = wr.discard(session, found.files)
            kept_note = f"; kept {kept} measured or looked-up values" if kept else ""
            return _redirect(back, f"Dropped {dropped} edits{kept_note}.", link=history)
        if action == "accept":
            description = wr.description(selection, len(found.files), accept="gnudb")
            taken = wr.accept(session, found.files, "gnudb", description)
            pending = selection_url(
                f"/libraries/{lib.name}/pending", path=path or None, artist=artist, album=album,
                only=only or None,
            )  # fmt: skip
            return _redirect(
                pending, f"Accepted {taken} of gnudb's values; write them here.", link=history
            )
        version = int(id3_version) if id3_version in ("3", "4") else None
        description = wr.description(selection, len(found.files))
        changeset, outcomes = wr.write(session, found.files, description, version)
        return render(
            request, "changeset.html", changeset=changeset, describe=ch.describe,
            outcomes=outcomes, outcome_title="Written",
        )  # fmt: skip

    # ---- decisions -----------------------------------------------------------------------

    @app.get("/libraries/{name}/history", response_class=HTMLResponse)
    def history_page(request: Request, name: str, start: int = 0, session: Session = SessionDep):
        """The user's decisions in the catalog, newest first, each of which can be undone."""
        lib = _library(session, name)
        entries = ops.history(session, lib.id, HISTORY_PAGE + 1, max(start, 0))
        return render(
            request, "history.html", library=lib, entries=entries[:HISTORY_PAGE],
            start=max(start, 0), more=len(entries) > HISTORY_PAGE, page=HISTORY_PAGE,
        )  # fmt: skip

    @app.get("/libraries/{name}/history/{operation_id}", response_class=HTMLResponse)
    def operation_page(
        request: Request, name: str, operation_id: int, session: Session = SessionDep
    ):
        lib = _library(session, name)
        operation = _operation(session, lib, operation_id)
        rows = ops.changed_values(session, operation, MAX_BATCH)
        return render(
            request, "operation.html", library=lib, operation=operation, rows=rows,
            total=len(operation.values),
        )  # fmt: skip

    @app.post("/libraries/{name}/history/{operation_id}/undo", dependencies=[Exclusive])
    def undo_operation(name: str, operation_id: int, session: Session = SessionDep):
        """Set the decision's values back where nothing has changed them since."""
        lib = _library(session, name)
        operation = _operation(session, lib, operation_id)
        back = f"/libraries/{lib.name}/history"
        try:
            undone = ops.undo(session, operation)
        except ValueError as e:
            return _redirect(back, str(e))
        session.commit()
        kept = f"; {undone.kept} values changed since were kept" if undone.kept else ""
        if undone.operation is None:
            return _redirect(back, f"Nothing to undo{kept}")
        return _redirect(
            back, f"Set {undone.reverted} values back{kept}.",
            link=(f"/libraries/{lib.name}/pending", "See what is pending"),
        )  # fmt: skip

    # ---- ratings and covers --------------------------------------------------------------

    @app.get("/libraries/{name}/ratings", response_class=HTMLResponse)
    def ratings_page(request: Request, name: str, session: Session = SessionDep):
        lib = _library(session, name)
        return render(
            request, "ratings.html", library=lib, counts=rt.preferences(session, lib),
            mapping=rt.get_map(session, lib),
        )  # fmt: skip

    @app.post("/libraries/{name}/ratings", dependencies=[Exclusive])
    async def apply_ratings(request: Request, name: str, session: Session = SessionDep):
        """Save the mapping from preferences to stars (fields "stars-<n>" with "pref-<n>")
        and set the ratings."""
        lib = _library(session, name)
        form = await request.form()
        mapping = rt.get_map(session, lib)
        for key, preference in form.multi_items():
            if key.startswith("pref-"):
                stars = str(form.get("stars-" + key.removeprefix("pref-"), ""))
                valid = stars in ("1", "2", "3", "4", "5")
                mapping[str(preference)] = int(stars) if valid else None
        rt.set_map(session, lib, mapping)
        session.commit()
        rated = rt.apply(session, lib, mapping)
        return _redirect(
            _url(f"/libraries/{lib.name}/pending", only="rating"),
            f"Rated {rated} files in the catalog; write the ratings into the files here",
        )

    @app.get("/libraries/{name}/covers", response_class=HTMLResponse)
    def covers_page(
        request: Request,
        name: str,
        path: str = "",
        limit: int = 20,
        min_pixels: int = covers.MIN_PIXELS,
        save_images: bool = True,
        refresh: bool = False,
        session: Session = SessionDep,
    ):
        lib = _library(session, name)
        limit = _clamp_batch(limit)
        found = covers.survey(
            session, lib, path_prefix=path or None, enough=limit,
            min_pixels=min_pixels, save_images=save_images, cached=not refresh,
        )  # fmt: skip
        return render(
            request, "covers.html", library=lib, found=found, path=path, limit=limit,
            min_pixels=min_pixels, save_images=save_images,
        )  # fmt: skip

    @app.post("/libraries/{name}/covers", dependencies=[Exclusive])
    def propose_covers(
        name: str,
        path: Annotated[str, Form()] = "",
        limit: Annotated[int, Form()] = 20,
        min_pixels: Annotated[int, Form()] = covers.MIN_PIXELS,
        save_images: Annotated[bool, Form()] = True,
        folder: Annotated[str, Form()] = "",
        picking: Annotated[bool, Form()] = False,
        pick: Annotated[list[str] | None, Form()] = None,
        session: Session = SessionDep,
    ):
        """The folders ticked on the page (picking says the page had ticks, so that none
        ticked proposes nothing, not the whole batch), a batch of folders, or with folder,
        just that one. The folders are looked at afresh, as the page may be out of date."""
        lib = _library(session, name)
        if folder:
            found = covers.survey(
                session, lib, path_prefix=folder + "/", min_pixels=min_pixels,
                save_images=save_images,
            )  # fmt: skip
            chosen = [f for f in found.folders if f.folder == folder]
        else:
            found = covers.survey(
                session, lib, path_prefix=path or None, enough=_clamp_batch(limit),
                min_pixels=min_pixels, save_images=save_images,
            )  # fmt: skip
            chosen = found.folders
            if picking:
                chosen = [f for f in chosen if f.folder in set(pick or [])]
        if not chosen:
            return _redirect(
                f"/libraries/{lib.name}/covers",
                "No folder selected" if picking and not pick else "Nothing to embed",
            )
        operation = covers.decide(
            session, lib, chosen,
            f"Embed the cover of {chosen[0].folder}" if folder
            else f"Embed the covers of {len(chosen)} folders",
        )  # fmt: skip
        # The covers just chosen are the ones to review: those under the folder, or the batch
        where = folder + "/" if folder else path
        return _redirect(
            _url(f"/libraries/{lib.name}/pending", path=where or None, only="cover"),
            "Decided on the covers; write them into the files here"
            if operation
            else "These covers were decided already",
            link=(f"/libraries/{lib.name}/history", "Undo on the History page"),
        )

    # ---- layout --------------------------------------------------------------------------

    @app.get("/libraries/{name}/layout", response_class=HTMLResponse)
    def layout_page(
        request: Request, name: str, path: str = "", limit: int = 20, session: Session = SessionDep
    ):
        lib = _library(session, name)
        limit = _clamp_batch(limit)
        template = lo.get_layout(session, lib)
        plan = lo.plan(session, lib, template, path_prefix=path or None)
        return render(
            request, "layout.html", library=lib, layout_template=template, plan=plan,
            batch=plan.albums[:limit], path=path, limit=limit, error=None,
            fields=lo.TEMPLATE_FIELDS, default=lo.DEFAULT_LAYOUT,
        )  # fmt: skip

    @app.post("/libraries/{name}/layout", response_class=HTMLResponse)
    def set_layout(
        request: Request,
        name: str,
        template: Annotated[str, Form()],
        session: Session = SessionDep,
    ):
        lib = _library(session, name)
        try:
            lo.set_layout(session, lib, template)
        except lo.LayoutError as e:
            current = lo.get_layout(session, lib)
            plan = lo.plan(session, lib, current)
            return render(
                request, "layout.html", status_code=400, library=lib, layout_template=template,
                plan=plan, batch=plan.albums[:20], path="", limit=20, error=str(e),
                fields=lo.TEMPLATE_FIELDS, default=lo.DEFAULT_LAYOUT,
            )  # fmt: skip
        session.commit()
        return _redirect(f"/libraries/{lib.name}/layout", "Layout saved")

    @app.post("/libraries/{name}/layout/decide", dependencies=[Exclusive])
    def decide_layout(
        name: str,
        path: Annotated[str, Form()] = "",
        limit: Annotated[int, Form()] = 20,
        folder: Annotated[str, Form()] = "",
        session: Session = SessionDep,
    ):
        """Decide on a batch of albums, or with folder, the one album going into that folder."""
        lib = _library(session, name)
        plan = lo.plan(session, lib, lo.get_layout(session, lib), path_prefix=path or None)
        if folder:
            albums = [a for a in plan.albums if a.folder == folder]
        else:
            albums = plan.albums[: _clamp_batch(limit)]
        if not albums:
            return _redirect(f"/libraries/{lib.name}/layout", "Nothing to move")
        description = (
            f"Move {albums[0].folder} into place" if folder
            else f"Move {len(albums)} albums into the layout"
        )  # fmt: skip
        lo.decide(session, lib, albums, description)
        return _redirect(
            _url(f"/libraries/{lib.name}/pending", path=path or None),
            "Decided; review and write the moves here",
            link=(f"/libraries/{lib.name}/history", "Undo on the History page"),
        )

    # ---- changesets ----------------------------------------------------------------------

    @app.get("/changes", response_class=HTMLResponse)
    def changesets(request: Request, all: bool = False, session: Session = SessionDep):
        rows = []
        for changeset in session.scalars(select(Changeset).order_by(Changeset.id.desc())):
            counts: dict[str, int] = {}
            for change in changeset.changes:
                counts[change.status] = counts.get(change.status, 0) + 1
            if all or {"proposed", "approved", "failed"} & counts.keys():
                rows.append((changeset, counts))
        return render(request, "changes.html", rows=rows, all=all)

    @app.get("/changes/{changeset_id}", response_class=HTMLResponse)
    def changeset(request: Request, changeset_id: int, session: Session = SessionDep):
        changeset = _changeset(session, changeset_id)
        return render(
            request, "changeset.html", changeset=changeset, describe=ch.describe, outcomes=None
        )

    @app.post("/changes/{changeset_id}/status")
    def changeset_status(
        changeset_id: int,
        action: str = Form(...),
        change_ids: Annotated[list[int] | None, Form()] = None,
        session: Session = SessionDep,
    ):
        changeset = _changeset(session, changeset_id)
        statuses = {"approve": "approved", "reject": "rejected", "reset": "proposed"}
        if action.removesuffix("-all") not in statuses:
            raise HTTPException(400, f"Unknown action {action}")
        selected = (
            list(changeset.changes)
            if action.endswith("-all")
            else [c for c in changeset.changes if c.id in (change_ids or [])]
        )
        count = ch.set_status(selected, statuses[action.removesuffix("-all")])
        session.commit()
        return _redirect(f"/changes/{changeset.id}", f"{count} changes updated")

    @app.post(
        "/changes/{changeset_id}/apply", response_class=HTMLResponse, dependencies=[Exclusive]
    )
    def changeset_apply(
        request: Request,
        changeset_id: int,
        dry_run: bool = Form(False),
        session: Session = SessionDep,
    ):
        changeset = _changeset(session, changeset_id)
        outcomes = ch.apply(session, changeset, dry_run=dry_run)
        return render(
            request, "changeset.html", changeset=changeset, describe=ch.describe,
            outcomes=outcomes, outcome_title="Dry run" if dry_run else "Applied",
        )  # fmt: skip

    @app.post(
        "/changes/{changeset_id}/retry", response_class=HTMLResponse, dependencies=[Exclusive]
    )
    def changeset_retry(request: Request, changeset_id: int, session: Session = SessionDep):
        changeset = _changeset(session, changeset_id)
        ch.retry_failed(changeset)
        outcomes = ch.apply(session, changeset)
        return render(
            request, "changeset.html", changeset=changeset, describe=ch.describe,
            outcomes=outcomes, outcome_title="Retried",
        )  # fmt: skip

    @app.post("/changes/{changeset_id}/delete", dependencies=[Exclusive])
    def changeset_delete(changeset_id: int, session: Session = SessionDep):
        changeset = _changeset(session, changeset_id)
        try:
            ch.delete_changeset(session, changeset)
        except ch.ChangeError as e:
            return _redirect(f"/changes/{changeset_id}", str(e))
        return _redirect("/changes", f"Changeset {changeset_id} deleted")

    @app.post("/changes/{changeset_id}/undo", response_class=HTMLResponse, dependencies=[Exclusive])
    def changeset_undo(request: Request, changeset_id: int, session: Session = SessionDep):
        changeset = _changeset(session, changeset_id)
        outcomes = ch.undo(session, changeset)
        return render(
            request, "changeset.html", changeset=changeset, describe=ch.describe,
            outcomes=outcomes, outcome_title="Undone",
        )  # fmt: skip

    # ---- playlists -----------------------------------------------------------------------

    @app.get("/libraries/{name}/playlists", response_class=HTMLResponse)
    def playlists(request: Request, name: str, session: Session = SessionDep):
        lib = _library(session, name)
        return render(
            request, "playlists.html", library=lib,
            playlists=pl.library_playlists(session, lib), sorts=pl.SORTS, form={}, error=None,
            places=_places(session, lib), all_formats=list(FORMAT_LABELS),
        )  # fmt: skip

    @app.post("/libraries/{name}/playlists", response_class=HTMLResponse)
    def create_playlist(
        request: Request,
        name: str,
        playlist_name: str = Form(...),
        rule: str = Form(""),
        group_by: str = Form(""),
        sort: str = Form(""),
        max_tracks: str = Form(""),
        session: Session = SessionDep,
    ):
        lib = _library(session, name)
        form = {"playlist_name": playlist_name, "rule": rule, "group_by": group_by,
                "sort": sort, "max_tracks": max_tracks}  # fmt: skip
        playlist = Playlist(
            library_id=lib.id, name=playlist_name.strip(), rule=rule.strip() or None,
            group_by=group_by.strip() or None,
            sort=sort or ("shuffle" if rule.strip() else "manual"),
            max_tracks=int(max_tracks) if max_tracks.strip().isdigit() else None,
        )  # fmt: skip
        error = None
        try:
            pl.validate(playlist)
        except pl.RuleError as e:
            error = str(e)
        if not error and pl.name_taken(session, lib, playlist.name, smart=playlist.is_smart):
            error = f"There is already a {pl.kind(playlist)} playlist named {playlist.name!r}"
        if error:
            return render(
                request, "playlists.html", status_code=400, library=lib,
                playlists=pl.library_playlists(session, lib), sorts=pl.SORTS, form=form,
                error=error, places=_places(session, lib), all_formats=list(FORMAT_LABELS),
            )  # fmt: skip
        session.add(playlist)
        session.commit()
        return _redirect(f"/playlists/{playlist.id}", "Playlist created")

    @app.get("/playlists/{playlist_id}", response_class=HTMLResponse)
    def playlist(
        request: Request,
        playlist_id: int,
        group: str = "",
        place: str = "",
        folder: str | None = None,
        formats: Annotated[list[str], Query()] = [],  # noqa: B006
        check: str = "",
        session: Session = SessionDep,
    ):
        playlist = _playlist(session, playlist_id)
        generated = pl.generate(session, playlist.library, [playlist])
        shown = generated[0] if generated else None
        if group:
            shown = next((g for g in generated if g.name == group), shown)
        tried = {place: (folder, formats)} if place and folder is not None else {}
        return render(
            request, "playlist.html", playlist=playlist, generated=generated, shown=shown,
            sorts=pl.SORTS, error=None, group=group,
            places=_file_places(session, playlist, generated, tried, check),
            all_formats=list(FORMAT_LABELS),
            folder_links=_folder_links(playlist.library, [t.path for t in shown.tracks])
            if shown else [],
        )  # fmt: skip

    @app.post("/playlists/{playlist_id}/files", dependencies=[Exclusive])
    def playlist_files(
        playlist_id: int,
        place: str = Form(...),
        folder: str = Form(""),
        formats: Annotated[list[str], Form()] = [],  # noqa: B006
        do: str = Form(...),
        session: Session = SessionDep,
    ):
        """Write the playlist's files to a place, or delete them, with the folder and
        formats shown on the page (not necessarily the saved ones)."""
        playlist = _playlist(session, playlist_id)
        back = f"/playlists/{playlist.id}"
        try:
            where = _place_for(session, playlist.library, place, folder, formats)
            if do == "write":
                planned = ps.plan_playlists(session, where, [playlist])
                changed = ps.apply_write(session, where, planned)
                kept = sum(1 for p in planned if p.state == "modified")
                message = (
                    f"Wrote {changed} of {len(planned)} files to {where.folder_description()}"
                    + (
                        f"; {kept} changed elsewhere, their old content is kept below"
                        if kept
                        else ""
                    )
                )
            else:
                removed = ps.apply_delete(session, where, ps.plan_delete(session, where, playlist))
                message = f"Deleted {removed} files from {where.folder_description()}; kept below"
            session.commit()
        except (ValueError, OSError, ClientError) as e:
            session.rollback()
            message = f"Could not {do} the files: {e}"
        return _redirect(back, message)

    @app.post("/playlists/{playlist_id}/files/{backup_id}/restore", dependencies=[Exclusive])
    def restore_playlist_file(playlist_id: int, backup_id: int, session: Session = SessionDep):
        playlist = _playlist(session, playlist_id)
        backup = session.get(PlaylistFileBackup, backup_id)
        if backup is None or backup.library_id != playlist.library_id:
            raise HTTPException(404, f"No backup {backup_id}")
        target = session.get(SyncTarget, backup.target_id) if backup.target_id else None
        where = ps.Place(playlist.library, target)
        try:
            ps.restore(session, where, backup)
            session.commit()
            message = f"Restored {where.describe(backup.path)}"
        except (ValueError, OSError, ClientError) as e:
            session.rollback()
            message = f"Could not restore the file: {e}"
        return _redirect(f"/playlists/{playlist.id}", message)

    @app.post("/libraries/{name}/playlists/files-settings")
    def playlist_files_settings(
        name: str,
        place: Annotated[list[str], Form()],
        folder: Annotated[list[str], Form()],
        formats: Annotated[list[str], Form()] = [],  # noqa: B006
        session: Session = SessionDep,
    ):
        """Save the folder and formats of every place at once. The form sends the places
        and their folders in the same order; a chosen format is "<place>:<format>"."""
        lib = _library(session, name)
        back = f"/libraries/{name}/playlists"
        if len(place) != len(folder):
            raise HTTPException(400, "Each place needs a folder")
        try:
            places = [
                _place_for(
                    session, lib, key, path,
                    [f.split(":", 1)[1] for f in formats if f.split(":", 1)[0] == key],
                )
                for key, path in zip(place, folder, strict=True)
            ]  # fmt: skip
        except ValueError as e:
            return _redirect(back, f"Nothing saved: {e}")
        for where in places:
            source = where.target or lib
            source.playlist_folder = where.folder
            source.playlist_formats = ",".join(where.formats)
        session.commit()
        return _redirect(back, "Settings saved; no files were written")

    @app.post("/playlists/{playlist_id}/edit", response_class=HTMLResponse)
    def edit_playlist(
        request: Request,
        playlist_id: int,
        playlist_name: str = Form(...),
        rule: str = Form(""),
        group_by: str = Form(""),
        sort: str = Form(...),
        max_tracks: str = Form(""),
        session: Session = SessionDep,
    ):
        playlist = _playlist(session, playlist_id)
        playlist.name = playlist_name.strip()
        if playlist.is_smart:
            playlist.rule = rule.strip()
            playlist.group_by = group_by.strip() or None
        playlist.sort = sort
        playlist.max_tracks = int(max_tracks) if max_tracks.strip().isdigit() else None
        try:
            pl.validate(playlist)
            with session.no_autoflush:  # the renamed playlist would clash with itself
                taken = pl.name_taken(
                    session, playlist.library, playlist.name,
                    smart=playlist.is_smart, except_id=playlist.id,
                )  # fmt: skip
            if taken:
                raise pl.RuleError(
                    f"There is already a {pl.kind(playlist)} playlist named {playlist.name!r}"
                )
        except pl.RuleError as e:
            session.rollback()
            playlist = _playlist(session, playlist_id)
            generated = pl.generate(session, playlist.library, [playlist])
            return render(
                request, "playlist.html", status_code=400, playlist=playlist,
                generated=generated, shown=generated[0] if generated else None,
                sorts=pl.SORTS, error=str(e), group="", all_formats=list(FORMAT_LABELS),
                places=_file_places(session, playlist, generated, {}, ""),
            )  # fmt: skip
        session.commit()
        return _redirect(f"/playlists/{playlist.id}", "Playlist saved")

    @app.post("/playlists/{playlist_id}/delete")
    def delete_playlist(playlist_id: int, session: Session = SessionDep):
        playlist = _playlist(session, playlist_id)
        library_name = playlist.library.name
        session.delete(playlist)
        session.commit()
        return _redirect(f"/libraries/{library_name}/playlists", "Playlist deleted")

    # ---- sync ----------------------------------------------------------------------------

    @app.get("/sync", response_class=HTMLResponse)
    def sync_targets(request: Request, session: Session = SessionDep):
        targets = session.scalars(select(SyncTarget).order_by(SyncTarget.name)).all()
        summaries = {t.id: sy.remote_summary(session, t) for t in targets}
        active = {t.id: jobs.active_job(session, t) for t in targets}
        return render(
            request, "sync.html", targets=targets, target=None, plan=None, summaries=summaries,
            active=active,
        )  # fmt: skip

    @app.get("/sync/targets/new", response_class=HTMLResponse)
    def new_sync_target(request: Request, session: Session = SessionDep):
        libraries = session.scalars(select(Library).order_by(Library.name)).all()
        return render(
            request, "sync_target.html", target=None, libraries=libraries, layout=[], values={}
        )

    @app.get("/sync/targets/{target_id:int}", response_class=HTMLResponse)
    def edit_sync_target(request: Request, target_id: int, session: Session = SessionDep):
        target = _sync_target(session, target_id)
        libraries = session.scalars(select(Library).order_by(Library.name)).all()
        return render(
            request, "sync_target.html", target=target, libraries=libraries,
            layout=sy.bucket_layout(target), values={},
        )  # fmt: skip

    @app.post("/sync/targets")
    @app.post("/sync/targets/{target_id:int}")
    async def save_sync_target(
        request: Request, target_id: int | None = None, session: Session = SessionDep
    ):
        """Add a target, or change one. Credentials are not stored: only the name of an
        AWS profile, if one is wanted."""
        form = await request.form()
        values = {k: str(form.get(k, "")).strip() for k in (
            "name", "library", "bucket", "prefix", "endpoint_url", "region", "profile"
        )}  # fmt: skip
        target = _sync_target(session, target_id) if target_id else None
        libraries = session.scalars(select(Library).order_by(Library.name)).all()
        library = next((lib for lib in libraries if lib.name == values["library"]), None)
        try:
            sy.check_target_name(values["name"])
            if library is None:
                raise ValueError("Choose a library")
            if not values["bucket"]:
                raise ValueError("A bucket name is needed")
            other = session.scalar(select(SyncTarget).where(SyncTarget.name == values["name"]))
            if other is not None and other is not target:
                raise ValueError(f"There is already a target named {values['name']}")
        except ValueError as e:
            return render(
                request, "sync_target.html", status_code=400, target=target,
                libraries=libraries, layout=[], values=values, error=str(e),
            )  # fmt: skip
        if target is None:
            target = SyncTarget(name=values["name"], library=library, bucket=values["bucket"])
            session.add(target)
        target.name, target.library, target.bucket = values["name"], library, values["bucket"]
        target.prefix = values["prefix"].strip("/")
        target.endpoint_url = values["endpoint_url"] or None
        target.region = values["region"] or None
        target.profile = values["profile"] or None
        session.commit()
        return _redirect("/sync", f"Saved target {target.name}")

    @app.post("/sync/targets/{target_id:int}/remove")
    async def remove_sync_target(request: Request, target_id: int, session: Session = SessionDep):
        """Forget a target; the bucket is left alone. The form's checkbox is the
        confirmation, as the pages work without JavaScript."""
        target = _sync_target(session, target_id)
        if not (await request.form()).get("confirm"):
            return _redirect(f"/sync/targets/{target_id}", "Tick the box to remove the target")
        name = target.name
        try:
            objects = sy.remove_target(session, target)
        except jobs.SyncBusy as e:
            return _redirect(f"/sync/targets/{target_id}", str(e))
        return _redirect(
            "/sync", f"Removed target {name}: forgot {objects} synced objects, bucket unchanged"
        )

    @app.post("/sync/jobs")
    async def start_sync(request: Request, session: Session = SessionDep):
        """Start syncing a target in the background, if the plan is still the one shown
        (fingerprint "expected"), and go to its progress page."""
        form = await request.form()
        name = str(form.get("target", ""))
        target = session.scalar(select(SyncTarget).where(SyncTarget.name == name))
        if target is None:
            raise HTTPException(404, f"No sync target named {name}")
        expected = str(form.get("expected", "")) or None
        try:
            job = jobs.start_in_background(
                session_factory, session, target, expected,
                plan=previews.ready_plan(target, expected),
            )  # fmt: skip
        except jobs.SyncBusy as e:
            return _redirect(f"/sync/jobs/{e.job.id}", str(e))
        previews.forget(target)  # the sync changes what the next plan shows
        return _redirect(f"/sync/jobs/{job.id}")

    @app.post("/sync/plans", dependencies=[Exclusive])
    async def replan_sync(request: Request, session: Session = SessionDep):
        """Make the plan of a target again, as the files may have changed."""
        name = str((await request.form()).get("target", ""))
        target = session.scalar(select(SyncTarget).where(SyncTarget.name == name))
        if target is None:
            raise HTTPException(404, f"No sync target named {name}")
        previews.get(target, again=True)
        return _redirect(f"/sync/{quote(target.name)}")

    @app.get("/sync/jobs/{job_id:int}")
    def sync_job(request: Request, job_id: int, session: Session = SessionDep):
        """A sync's progress: a page, or JSON for its script to update the page with."""
        job = jobs.refresh(session, _sync_job(session, job_id))
        state = _job_state(job)
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(state)
        return render(request, "sync_job.html", job=job, state=state, max_errors=jobs.ERRORS_KEPT)

    @app.post("/sync/jobs/{job_id:int}/stop")
    def stop_sync(job_id: int, session: Session = SessionDep):
        job = jobs.refresh(session, _sync_job(session, job_id))
        jobs.request_stop(session, job)
        return _redirect(f"/sync/jobs/{job_id}", "Stopping after the transfers under way")

    @app.get("/sync/{name:path}", response_class=HTMLResponse)
    def sync_plan(request: Request, name: str, session: Session = SessionDep):
        """What a sync would do, with a button to start it. Planning looks for album
        covers in every folder, which takes a while on a network share, so it runs in the
        background: the page shows its progress (JSON for the page's script) and then the
        plan."""
        target = session.scalar(select(SyncTarget).where(SyncTarget.name == name))
        if target is None:
            raise HTTPException(404, f"No sync target named {name}")
        active = jobs.active_job(session, target)
        preview = previews.get(target) if active is None else None
        if preview is not None and "application/json" in request.headers.get("accept", ""):
            return JSONResponse(_preview_state(preview))
        return render(
            request, "sync.html", targets=None, target=target, active=active,
            preview=preview, plan=preview.plan if preview else None,
            state=_preview_state(preview) if preview else None,
            recent=jobs.recent_jobs(session, target),
        )  # fmt: skip

    return app


# ---- helpers ----------------------------------------------------------------------------


def _clamp_batch(limit: int) -> int:
    """Keep a requested batch size between 1 and MAX_BATCH, however it was asked for
    (a hand-edited URL or form field isn't bound by the page's own min/max)."""
    return min(max(limit, 1), MAX_BATCH)


def _library(session: Session, name: str) -> Library:
    library = session.scalar(select(Library).where(Library.name == name))
    if library is None:
        raise HTTPException(404, f"No library named {name}")
    return library


JOB_LABELS = {
    "planning": "Planning",
    "running": "Syncing",
    "done": "Done",
    "failed": "Failed",
    "stopped": "Stopped",
    "changed": "Not started",
    "interrupted": "Interrupted",
}


def _preview_state(preview: jobs.Preview) -> dict:
    """A plan being made, as the page and its script show it."""
    planning = preview.status == "planning"
    counts = ""
    if planning:
        counts = (
            f"Checked {preview.done:,} of {preview.total:,} tracks and their album covers"
            if preview.total
            else "Reading the catalog"
        )
    return {
        "status": preview.status, "active": planning, "done": preview.done,
        "total": preview.total, "counts": counts, "error": preview.error,
    }  # fmt: skip


def _job_state(job: SyncJob) -> dict:
    """The job's state with the wording the page shows, so that the page's script and
    the server-rendered page say the same."""
    state = jobs.state(job)
    label = JOB_LABELS.get(job.status, job.status)
    if job.status == "running" and job.stop_requested:
        label = "Stopping"
    state["label"] = label
    if job.status == "planning":
        state["counts"] = (
            f"Checked {job.done:,} of {job.total:,} tracks and their album covers"
            if job.total
            else "Reading the catalog"
        )
    else:
        failed = f", {job.failed} failed" if job.failed else ""
        state["counts"] = (
            f"{job.done} of {job.total} done{failed}; "
            f"{job.bytes_done / 1e6:.0f} of {job.bytes_total / 1e6:.0f} MB uploaded"
        )
    return state


def _sync_job(session: Session, job_id: int) -> SyncJob:
    job = session.get(SyncJob, job_id)
    if job is None:
        raise HTTPException(404, "No such sync")
    return job


def _sync_target(session: Session, target_id: int) -> SyncTarget:
    target = session.get(SyncTarget, target_id)
    if target is None:
        raise HTTPException(404, "No such sync target")
    return target


def _file(session: Session, file_id: int) -> AudioFile:
    file = session.get(AudioFile, file_id)
    if file is None:
        raise HTTPException(404, f"No track {file_id}")
    return file


def _changeset(session: Session, changeset_id: int) -> Changeset:
    changeset = session.get(Changeset, changeset_id)
    if changeset is None:
        raise HTTPException(404, f"No changeset {changeset_id}")
    return changeset


def _operation(session: Session, library: Library, operation_id: int) -> Operation:
    operation = session.get(Operation, operation_id)
    if operation is None or operation.library_id != library.id:
        raise HTTPException(404, f"No decision {operation_id} in {library.name}")
    return operation


def _playlist(session: Session, playlist_id: int) -> Playlist:
    playlist = session.get(Playlist, playlist_id)
    if playlist is None:
        raise HTTPException(404, f"No playlist {playlist_id}")
    return playlist


FORMAT_LABELS = {"m3u": ".m3u", "json": ".json (schema.org)", "html": ".html"}


def _place_key(place: ps.Place) -> str:
    return "disk" if place.target is None else f"t{place.target.id}"


def _place_for(
    session: Session, library: Library, key: str, folder: str, formats: list[str]
) -> ps.Place:
    """The place named by a form's key ("disk" or "t<target id>") with the folder and
    formats it submitted. ValueError for values that are not allowed."""
    target = None
    if key != "disk":
        target = session.get(SyncTarget, int(key[1:])) if key[1:].isdigit() else None
        if target is None or target.library_id != library.id:
            raise ValueError(f"Unknown place {key!r}")
    return ps.Place(
        library, target, folder=pf.clean_folder(folder), formats=pf.parse_formats(formats)
    )


def _places(session: Session, library: Library) -> list[ps.Place]:
    """The library's disk and each of its sync targets, with their saved settings."""
    targets = session.scalars(
        select(SyncTarget).where(SyncTarget.library_id == library.id).order_by(SyncTarget.name)
    )
    return [ps.Place(library), *(ps.Place(library, t) for t in targets)]


def _file_places(
    session: Session,
    playlist: Playlist,
    generated: list[pl.GeneratedPlaylist],
    tried: dict[str, tuple[str | None, list[str]]],
    check: str,
) -> list[dict]:
    """What the playlist page shows per place (the library disk and each target): the
    folder and formats (the saved ones, or those the user is trying), the files that
    writing would create with their full paths, and backups that can be restored. The disk
    is read to say what would happen to each file; a bucket only when asked (`check`)."""
    views = []
    stems = {pl.safe_filename(g.name).casefold() for g in generated}
    everyone = pl.library_playlists(session, playlist.library)
    shared = pl.shared_names(everyone)
    clashes = [  # playlists whose files have the same names, so that writing one replaces the other
        pl.display_name(other, shared | {other.name})
        for other in everyone
        if other.id != playlist.id and pl.safe_filename(other.name).casefold() in stems
    ]
    for saved in _places(session, playlist.library):
        key = _place_key(saved)
        place, error = saved, None
        if key in tried:
            try:
                place = _place_for(session, playlist.library, key, *tried[key][:1], tried[key][1])
            except ValueError as e:
                error = str(e)
        states: dict[str, str] = {}
        if key == "disk" or key == check:
            try:
                planned = ps.plan_write(session, place, generated)
                states = {p.path: ps.describe_state(p) for p in planned}
            except (OSError, ClientError) as e:
                error = f"Could not read the files: {e}"
        paths = [
            pf.file_path(place.folder, g.name, fmt) for g in generated for fmt in place.formats
        ]
        views.append({
            "key": key,
            "label": "the library disk" if key == "disk" else f"bucket {saved.target.name}",
            "place": place,
            "saved": saved,
            "changed": (place.folder, place.formats) != (saved.folder, saved.formats),
            "root": place.describe("").rstrip("/") if place.target else str(place.library.root),
            "names": [pl.safe_filename(g.name) for g in generated],
            "files": [
                {"display": place.describe(path), "state": states.get(path, "")} for path in paths
            ],
            "checked": key == "disk" or key == check,
            "error": error,
            "clashes": clashes,
            "backups": [
                {
                    "id": b.id,
                    "reason": b.reason,
                    "when": b.created_at,
                    "path": place.describe(b.path),
                }
                for b in ps.backups(session, place)
                if pl.safe_filename(playlist.name) in b.path or playlist.group_by
            ],
        })  # fmt: skip
    return views


def _order(columns) -> list:
    """ORDER BY terms: missing values last, text case-insensitively, numbers as numbers."""
    terms = []
    for column in columns:
        terms.append(column.is_(None))
        terms.append(func.lower(column) if isinstance(column.type, String) else column)
    return terms


def _asset(name: str) -> str:
    """A static file's URL with its modification time, so that browsers fetch it again
    after an update instead of using a cached copy."""
    return f"/static/{name}?v={int((HERE / 'static' / name).stat().st_mtime)}"


def _fold_text(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _folder_links(library: Library, paths) -> list[tuple[str, str]]:
    """The distinct folders of library-relative file paths, each with the file:// URI of
    the folder on this machine, to find the files in a file manager (a browser may refuse
    to open it from a web page)."""
    folders = sorted({str(PurePosixPath(p).parent) for p in paths})
    return [
        (
            f,
            PurePosixPath(library.root, f).as_uri()
            if f != "."
            else PurePosixPath(library.root).as_uri(),
        )
        for f in folders
    ]


def _redirect(
    path: str, message: str | None = None, link: tuple[str, str] | None = None
) -> RedirectResponse:
    """Redirect with a message, which may be followed by a link (path, label)."""
    if message:
        extra = {"msg": message}
        if link:
            extra.update(msg_href=link[0], msg_link=link[1])
        path += ("&" if "?" in path else "?") + urlencode(extra)
    return RedirectResponse(path, status_code=303)


def _url(base: str, /, **params) -> str:
    """A path with query parameters (which may include "path"), leaving out empty ones."""
    query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    return f"{base}?{query}" if query else base


def selection_url(base: str, /, **params) -> str:
    """Like _url, but keeping empty values: on the Pending page, artist="" chooses the
    tracks without an artist, while leaving artist out chooses every artist."""
    query = urlencode({k: v for k, v in params.items() if v is not None})
    return f"{base}?{query}" if query else base


def _duration(seconds: float | None) -> str:
    if not seconds:
        return ""
    s = round(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02}:{s % 60:02}" if s >= 3600 else f"{s // 60}:{s % 60:02}"
