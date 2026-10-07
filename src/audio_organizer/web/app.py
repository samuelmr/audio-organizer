"""The web UI: browsing the catalog, editing metadata, reviewing changesets, playlists
and sync plans. Server-rendered pages with plain HTML forms, so every page works
without JavaScript.

create_app() takes the session factory, so the same app can serve a local catalog
(`aorg web`) or, later, a hosted one.
"""

from collections.abc import Iterator
from math import ceil
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import String, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from .. import album_merge as am
from .. import changes as ch
from .. import duplicates as du
from .. import playlists as pl
from .. import sync as sy
from ..models import (
    AudioFile,
    Change,
    Changeset,
    EffectiveMetadata,
    Library,
    Playlist,
    SyncTarget,
    TagSnapshot,
)
from ..provenance import resolve
from ..tags import FIELDS
from ..tracks import present

HERE = Path(__file__).parent
PAGE_SIZE = 50
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


def create_app(session_factory: sessionmaker[Session]) -> FastAPI:
    app = FastAPI(title="audio-organizer", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["duration"] = _duration
    templates.env.globals["url"] = _url
    templates.env.globals["asset"] = _asset
    templates.env.globals["move_source"] = ch.move_source
    templates.env.globals["retag_fields"] = ch.retag_fields
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def get_session() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    SessionDep = Depends(get_session)

    def render(request: Request, template: str, status_code: int = 200, **context):
        context.setdefault("message", request.query_params.get("msg"))
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
        session: Session = SessionDep,
    ):
        """The tracks of a library: searched, filtered to one album, artist or folder,
        and sorted. Sorted by album, the tracks come in album order."""
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
        for column, value in ((M.album, album), (M.albumartist, albumartist), (M.artist, artist)):
            if value is not None:
                query = query.where(func.lower(column) == value.lower())
        if folder is not None:
            prefix = folder.strip("/") + "/"
            query = query.where(
                AudioFile.path.startswith(prefix, autoescape=True),
                ~AudioFile.path.like(_like_escape(prefix) + "%/%", escape="\\"),
            )
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
        return render(
            request, "library.html", library=lib, rows=rows, total=total, sort=sort,
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
        return render(
            request, "albums.html", library=lib, rows=rows, total=total, q=q, page=page,
            pages=pages,
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
        return render(
            request, "track.html", file=file, resolved=resolved, fields=FIELDS, extra=extra,
            manual=manual, copies=copies, snapshot=snapshot,
        )  # fmt: skip

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
        """Store the edited fields as manual values and propose writing them into the
        file. An emptied field drops its manual value, falling back to tags and path."""
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
        changeset = ch.new_changeset(session, f"Manual edit of {file.path}")
        change = ch.propose_manual_edit(session, changeset, file, values)
        if change is None:
            session.expunge(changeset)
            session.commit()
            return _redirect(f"/tracks/{file.id}", "Saved; the file's tags already match")
        session.commit()
        return _redirect(f"/changes/{changeset.id}", "Edit saved; review the change to the file")

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
        found = du.survey(session, lib, path_prefix=path or None, include_compilations=compilations)
        return render(
            request, "duplicates.html", library=lib, found=found,
            batch=found.proposals[: max(limit, 1)], path=path, limit=limit,
            compilations=compilations,
        )  # fmt: skip

    @app.post("/libraries/{name}/duplicates")
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
        changeset = du.propose(session, batch, f"Trash loose copies of {len(batch)} tracks{where}")
        return _redirect(f"/changes/{changeset.id}", "Proposed; review and approve the changes")

    @app.get("/libraries/{name}/album-merges", response_class=HTMLResponse)
    def album_merges(
        request: Request, name: str, path: str = "", limit: int = 20, session: Session = SessionDep
    ):
        lib = _library(session, name)
        found = am.survey(session, lib, path_prefix=path or None)
        return render(
            request, "album_merges.html", library=lib, found=found,
            batch=am.batch(found.merges, max(limit, 1)),
            others=[m for m in found.merges if not m.batchable], path=path, limit=limit,
        )  # fmt: skip

    @app.post("/libraries/{name}/album-merges")
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
            merges = am.batch(found.merges, max(limit, 1))
            description = f"Merge {len(merges)} album folders"
        if not any(m.trash or m.move for m in merges):
            return _redirect(f"/libraries/{lib.name}/album-merges", "Nothing to merge")
        changeset = am.propose(session, merges, description)
        return _redirect(f"/changes/{changeset.id}", "Proposed; review and approve the changes")

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

    @app.post("/changes/{changeset_id}/apply", response_class=HTMLResponse)
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

    @app.post("/changes/{changeset_id}/undo", response_class=HTMLResponse)
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
        if not error and session.scalar(
            select(Playlist).where(Playlist.library_id == lib.id, Playlist.name == playlist.name)
        ):
            error = f"There is already a playlist named {playlist.name!r}"
        if error:
            return render(
                request, "playlists.html", status_code=400, library=lib,
                playlists=pl.library_playlists(session, lib), sorts=pl.SORTS, form=form,
                error=error,
            )  # fmt: skip
        session.add(playlist)
        session.commit()
        return _redirect(f"/playlists/{playlist.id}", "Playlist created")

    @app.get("/playlists/{playlist_id}", response_class=HTMLResponse)
    def playlist(
        request: Request, playlist_id: int, group: str = "", session: Session = SessionDep
    ):
        playlist = _playlist(session, playlist_id)
        generated = pl.generate(session, playlist.library, [playlist])
        shown = generated[0] if generated else None
        if group:
            shown = next((g for g in generated if g.name == group), shown)
        return render(
            request, "playlist.html", playlist=playlist, generated=generated, shown=shown,
            sorts=pl.SORTS, error=None,
        )  # fmt: skip

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
        except pl.RuleError as e:
            session.rollback()
            playlist = _playlist(session, playlist_id)
            generated = pl.generate(session, playlist.library, [playlist])
            return render(
                request, "playlist.html", status_code=400, playlist=playlist,
                generated=generated, shown=generated[0] if generated else None,
                sorts=pl.SORTS, error=str(e),
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
        return render(request, "sync.html", targets=targets, target=None, plan=None)

    @app.get("/sync/{name}", response_class=HTMLResponse)
    def sync_plan(request: Request, name: str, session: Session = SessionDep):
        """What a sync would do. Planning looks for album covers in every folder, which
        takes a while on a network share; running the sync is left to `aorg sync`."""
        target = session.scalar(select(SyncTarget).where(SyncTarget.name == name))
        if target is None:
            raise HTTPException(404, f"No sync target named {name}")
        plan = sy.plan(session, target)
        return render(request, "sync.html", targets=None, target=target, plan=plan)

    return app


# ---- helpers ----------------------------------------------------------------------------


def _library(session: Session, name: str) -> Library:
    library = session.scalar(select(Library).where(Library.name == name))
    if library is None:
        raise HTTPException(404, f"No library named {name}")
    return library


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


def _playlist(session: Session, playlist_id: int) -> Playlist:
    playlist = session.get(Playlist, playlist_id)
    if playlist is None:
        raise HTTPException(404, f"No playlist {playlist_id}")
    return playlist


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


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _redirect(path: str, message: str | None = None) -> RedirectResponse:
    if message:
        path += ("&" if "?" in path else "?") + urlencode({"msg": message})
    return RedirectResponse(path, status_code=303)


def _url(path: str, **params) -> str:
    """A path with query parameters, leaving out empty ones."""
    query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    return f"{path}?{query}" if query else path


def _duration(seconds: float | None) -> str:
    if not seconds:
        return ""
    s = round(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02}:{s % 60:02}" if s >= 3600 else f"{s // 60}:{s % 60:02}"
