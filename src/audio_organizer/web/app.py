"""The web UI: browsing the catalog, editing metadata, reviewing changesets, playlists
and sync plans. Server-rendered pages with plain HTML forms, so every page works
without JavaScript.

create_app() takes the session factory, so the same app can serve a local catalog
(`aorg web`) or, later, a hosted one.
"""

import unicodedata
from collections.abc import Iterator
from math import ceil
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import String, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from .. import album_merge as am
from .. import artists as art
from .. import changes as ch
from .. import covers, loudness, tags
from .. import duplicates as du
from .. import layout as lo
from .. import playlists as pl
from .. import ratings as rt
from .. import recordings as rec
from .. import sync as sy
from .. import writing as wr
from ..models import (
    AudioFile,
    Change,
    Changeset,
    CoverImage,
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
        path = Path(file.library.root) / file.path
        on_disk = path.is_file()
        folder_cover = sy.find_cover(path.parent) if on_disk else None
        return render(
            request, "track.html", file=file, resolved=resolved, groups=FIELD_GROUPS,
            extra=extra, manual=manual, copies=copies, snapshot=snapshot,
            unwritten=ch.unwritten(file), on_disk=on_disk,
            has_cover=on_disk and covers.embedded(path) is not None,
            folder_cover=folder_cover.name if folder_cover else None,
            id3_version=tags.id3_version(path) if on_disk else None,
        )  # fmt: skip

    def _id3_version(form) -> int | None:
        value = str(form.get("id3_version", ""))
        return int(value) if value in ("3", "4") else None

    @app.post("/tracks/{file_id}/write")
    async def write_track(request: Request, file_id: int, session: Session = SessionDep):
        """Propose writing the values not yet in the file (edits, measurements, lookups)."""
        file = _file(session, file_id)
        form = await request.form()
        changeset = ch.new_changeset(session, f"Write tags of {file.path}", "writing")
        change = ch.propose_tag_sync(changeset, file, _id3_version(form))
        if change is None:
            session.expunge(changeset)
            return _redirect(f"/tracks/{file.id}", "The file already has these values")
        session.commit()
        return _redirect(f"/changes/{changeset.id}", "Review the change to the file")

    @app.post("/tracks/{file_id}/replaygain")
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

    @app.post("/tracks/{file_id}/cover")
    def embed_cover(file_id: int, session: Session = SessionDep):
        """Propose embedding the folder's cover image into the file."""
        file = _file(session, file_id)
        path = Path(file.library.root) / file.path
        image = sy.find_cover(path.parent)
        if image is None:
            return _redirect(f"/tracks/{file.id}", "There is no cover image in the folder")
        changeset = ch.new_changeset(session, f"Embed the cover of {file.path}", "covers")
        relative = image.relative_to(file.library.root).as_posix()
        ch.propose_embed_cover(changeset, file, relative)
        session.commit()
        return _redirect(f"/changes/{changeset.id}", "Review the change to the file")

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
        changeset = ch.new_changeset(session, f"Manual edit of {file.path}", "manual")
        change = ch.propose_manual_edit(session, changeset, file, values, _id3_version(form))
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

    # ---- recordings in several encodings ------------------------------------------------

    @app.get("/libraries/{name}/recordings", response_class=HTMLResponse)
    def recordings(
        request: Request,
        name: str,
        path: str = "",
        limit: int = 50,
        replace: bool = False,
        compilations: bool = False,
        session: Session = SessionDep,
    ):
        lib = _library(session, name)
        found = rec.survey(
            session, lib, path_prefix=path or None, include_compilations=compilations
        )
        return render(
            request, "recordings.html", library=lib, found=found,
            batch=rec.batch(found.proposals, max(limit, 1), replace=replace), path=path,
            limit=limit, replace=replace, compilations=compilations,
        )  # fmt: skip

    @app.post("/libraries/{name}/recordings")
    async def propose_recordings(request: Request, name: str, session: Session = SessionDep):
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
            return _redirect(f"/libraries/{lib.name}/recordings", "Nothing to propose")
        where = f" in {path}" if path else ""
        changeset = rec.propose(
            session, batch, f"Keep the best encoding of {len(batch)} tracks{where}"
        )
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

    @app.post("/libraries/{name}/artists")
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

    @app.get("/libraries/{name}/write-tags", response_class=HTMLResponse)
    def write_tags_page(
        request: Request,
        name: str,
        path: str = "",
        only: str = "",
        limit: int = 100,
        session: Session = SessionDep,
    ):
        lib = _library(session, name)
        found = wr.survey(session, lib, path_prefix=path or None, only=_only(only))
        unmeasured = len(loudness.unmeasured(session, lib))
        return render(
            request, "write_tags.html", library=lib, found=found,
            batch=found.files[: max(limit, 1)], path=path, only=only, limit=limit,
            unmeasured=unmeasured,
        )  # fmt: skip

    @app.post("/libraries/{name}/write-tags")
    def propose_write_tags(
        name: str,
        path: Annotated[str, Form()] = "",
        only: Annotated[str, Form()] = "",
        limit: Annotated[int, Form()] = 100,
        id3_version: Annotated[str, Form()] = "",
        session: Session = SessionDep,
    ):
        lib = _library(session, name)
        found = wr.survey(session, lib, path_prefix=path or None, only=_only(only))
        batch = found.files[: max(limit, 1)]
        if not batch:
            return _redirect(f"/libraries/{lib.name}/write-tags", "Nothing to write")
        version = int(id3_version) if id3_version in ("3", "4") else None
        changeset = wr.propose(session, batch, f"Write tags of {len(batch)} files", version)
        return _redirect(f"/changes/{changeset.id}", "Review the changes to the files")

    # ---- ratings and covers --------------------------------------------------------------

    @app.get("/libraries/{name}/ratings", response_class=HTMLResponse)
    def ratings_page(request: Request, name: str, session: Session = SessionDep):
        lib = _library(session, name)
        return render(
            request, "ratings.html", library=lib, counts=rt.preferences(session, lib),
            mapping=rt.get_map(session, lib),
        )  # fmt: skip

    @app.post("/libraries/{name}/ratings")
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
            _url(f"/libraries/{lib.name}/write-tags", only="rating"),
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
        session: Session = SessionDep,
    ):
        lib = _library(session, name)
        found = covers.survey(
            session, lib, path_prefix=path or None, enough=max(limit, 1),
            min_pixels=min_pixels, save_images=save_images,
        )  # fmt: skip
        return render(
            request, "covers.html", library=lib, found=found, path=path, limit=limit,
            min_pixels=min_pixels, save_images=save_images,
        )  # fmt: skip

    @app.post("/libraries/{name}/covers")
    def propose_covers(
        name: str,
        path: Annotated[str, Form()] = "",
        limit: Annotated[int, Form()] = 20,
        min_pixels: Annotated[int, Form()] = covers.MIN_PIXELS,
        save_images: Annotated[bool, Form()] = True,
        folder: Annotated[str, Form()] = "",
        session: Session = SessionDep,
    ):
        """A batch of folders, or with folder, just that one."""
        lib = _library(session, name)
        if folder:
            found = covers.survey(
                session, lib, path_prefix=folder + "/", min_pixels=min_pixels,
                save_images=save_images,
            )  # fmt: skip
            chosen = [f for f in found.folders if f.folder == folder]
        else:
            found = covers.survey(
                session, lib, path_prefix=path or None, enough=max(limit, 1),
                min_pixels=min_pixels, save_images=save_images,
            )  # fmt: skip
            chosen = found.folders
        if not chosen:
            return _redirect(f"/libraries/{lib.name}/covers", "Nothing to embed")
        changeset = covers.propose(
            session, lib, chosen,
            f"Embed the cover of {chosen[0].folder}" if folder
            else f"Embed the covers of {len(chosen)} folders",
        )  # fmt: skip
        return _redirect(f"/changes/{changeset.id}", "Review the changes to the files")

    # ---- layout --------------------------------------------------------------------------

    @app.get("/libraries/{name}/layout", response_class=HTMLResponse)
    def layout_page(
        request: Request, name: str, path: str = "", limit: int = 20, session: Session = SessionDep
    ):
        lib = _library(session, name)
        template = lo.get_layout(session, lib)
        plan = lo.plan(session, lib, template, path_prefix=path or None)
        return render(
            request, "layout.html", library=lib, layout_template=template, plan=plan,
            batch=plan.albums[: max(limit, 1)], path=path, limit=limit, error=None,
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

    @app.post("/libraries/{name}/layout/propose")
    def propose_layout(
        name: str,
        path: Annotated[str, Form()] = "",
        limit: Annotated[int, Form()] = 20,
        folder: Annotated[str, Form()] = "",
        session: Session = SessionDep,
    ):
        """A batch of albums, or with folder, the one album going into that folder."""
        lib = _library(session, name)
        plan = lo.plan(session, lib, lo.get_layout(session, lib), path_prefix=path or None)
        if folder:
            albums = [a for a in plan.albums if a.folder == folder]
        else:
            albums = plan.albums[: max(limit, 1)]
        if not albums:
            return _redirect(f"/libraries/{lib.name}/layout", "Nothing to move")
        description = (
            f"Move {albums[0].folder} into place" if folder
            else f"Move {len(albums)} albums into the layout"
        )  # fmt: skip
        changeset = lo.propose(session, lib, albums, description)
        return _redirect(f"/changes/{changeset.id}", "Proposed; review and approve the moves")

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
        summaries = {t.id: sy.remote_summary(session, t) for t in targets}
        return render(
            request, "sync.html", targets=targets, target=None, plan=None, summaries=summaries
        )

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
        objects = sy.remove_target(session, target)
        return _redirect(
            "/sync", f"Removed target {name}: forgot {objects} synced objects, bucket unchanged"
        )

    @app.get("/sync/{name:path}", response_class=HTMLResponse)
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


def _fold_text(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _redirect(path: str, message: str | None = None) -> RedirectResponse:
    if message:
        path += ("&" if "?" in path else "?") + urlencode({"msg": message})
    return RedirectResponse(path, status_code=303)


def _url(base: str, /, **params) -> str:
    """A path with query parameters (which may include "path"), leaving out empty ones."""
    query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    return f"{base}?{query}" if query else base


def _duration(seconds: float | None) -> str:
    if not seconds:
        return ""
    s = round(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02}:{s % 60:02}" if s >= 3600 else f"{s // 60}:{s % 60:02}"
