"""Online lookups of a whole library (gnudb, cover art) as background jobs of the web UI.

Looking up thousands of albums takes hours (the services are asked politely, a request
every few seconds), far longer than a request may wait, so the web UI runs
`gnudb.lookup_library` or `coverart.lookup_library` in a thread of the server and shows
its progress. One job of each kind runs per library, and while it runs the single-album
lookup of that kind waits for it, so that a service never gets two sessions from us at
once.

Jobs are kept in memory, as the sync plans are (sync_jobs.Previews): nothing is lost when
the server stops, since each album's lookup is committed as it is made and the next run
continues with the albums not looked up yet. A lookup run by `aorg gnudb` or `aorg coverart`
in another process is not seen here; don't run both at once.
"""

import threading
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from . import coverart, gnudb
from .models import Library

ERRORS_KEPT = 100
KINDS = ("gnudb", "coverart")


def _lookup_function(kind: str) -> Callable[..., Any]:
    """The kind's lookup, found when a run starts so that tests can replace it."""
    return {"gnudb": gnudb, "coverart": coverart}[kind].lookup_library


class LookupBusy(Exception):
    def __init__(self, library: Library, kind: str):
        super().__init__(f"{kind} lookups of {library.name} are running already")


class LookupJob:
    """A run of gnudb lookups, its progress updated by the thread that runs it."""

    def __init__(self, kind: str, library_id: int, limit: int | None) -> None:
        self.kind = kind
        self.library_id = library_id
        self.limit = limit
        self.status = "starting"  # starting | running | done | stopped | failed
        self.total = 0  # albums to look up in this run
        self.left = 0  # albums not looked up before this run, in all
        self.done = 0
        self.found = 0
        self.not_found = 0
        self.failed = 0
        self.errors: list[tuple[str, str]] = []  # folder, error
        self.current: str | None = None
        self.message: str | None = None
        self.stop_requested = False
        self.started = time.time()
        self.finished: float | None = None

    @property
    def active(self) -> bool:
        return self.status in ("starting", "running")

    def state(self) -> dict[str, Any]:
        return {
            "status": self.status, "active": self.active, "total": self.total,
            "left": self.left, "done": self.done, "found": self.found,
            "not_found": self.not_found, "failed": self.failed,
            "errors": [list(e) for e in self.errors], "current": self.current,
            "message": self.message, "stop_requested": self.stop_requested,
        }  # fmt: skip


class LookupJobs:
    """The lookup runs of this server, the last one of each kind per library."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._jobs: dict[tuple[str, int], LookupJob] = {}
        self._lock = threading.Lock()

    def get(self, library: Library, kind: str) -> LookupJob | None:
        """The library's running job of this kind, or the last one this server ran."""
        with self._lock:
            return self._jobs.get((kind, library.id))

    def running(self, library: Library, kind: str) -> bool:
        job = self.get(library, kind)
        return job is not None and job.active

    def start(self, library: Library, kind: str, limit: int | None = None) -> LookupJob:
        """Start looking up the library's albums not looked up yet (at most `limit` of
        them) in a background thread; LookupBusy if a run of this kind is under way."""
        with self._lock:
            job = self._jobs.get((kind, library.id))
            if job is not None and job.active:
                raise LookupBusy(library, kind)
            job = self._jobs[(kind, library.id)] = LookupJob(kind, library.id, limit)
        threading.Thread(
            target=self._run, args=(job,), name=f"{kind}-lookups-{library.id}", daemon=True
        ).start()
        return job

    def stop(self, library: Library, kind: str) -> None:
        """Ask the running job to stop; it stops once the album being looked up is done."""
        job = self.get(library, kind)
        if job is not None and job.active:
            job.stop_requested = True

    def _run(self, job: LookupJob) -> None:
        def started(total: int, left: int) -> None:
            job.total, job.left, job.status = total, left, "running"

        def progress(done: int, total: int, album: str, error: str | None) -> None:
            job.done, job.current = done, album
            if error:
                job.failed += 1
            if error and len(job.errors) < ERRORS_KEPT:
                job.errors.append((album, error))

        lookup = _lookup_function(job.kind)
        try:
            with self._session_factory() as session:
                library = session.get(Library, job.library_id)
                stats = lookup(
                    session, library, limit=job.limit, progress=progress, started=started,
                    should_stop=lambda: job.stop_requested,
                )  # fmt: skip
            job.found, job.not_found = stats.found, stats.not_found
            if job.stop_requested and stats.stopped:
                job.status = "stopped"
                job.message = f"Stopped after {job.done} of {job.total} albums"
            elif stats.stopped:
                job.status = "failed"
                job.message = f"Stopped: {stats.stopped}"
            else:
                job.status = "done"
                job.message = (
                    f"Looked up {job.done} albums: {stats.found} found, {stats.not_found} not found"
                )
        except Exception as e:
            job.status = "failed"
            job.message = f"{type(e).__name__}: {e}"
        finally:
            job.current = None
            job.finished = time.time()
