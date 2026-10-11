"""Online lookups of a whole library (gnudb, cover art) as background jobs of the web UI.

Looking up thousands of albums takes hours (the services are asked politely, a request
every few seconds), far longer than a request may wait, so the web UI runs
`gnudb.lookup_library` or `coverart.lookup_library` in a thread of the server and shows
its progress (see background). One job of each kind runs per library, and while it runs
the single-album lookup of that kind waits for it, so that a service never gets two
sessions from us at once.

Each album's lookup is committed as it is made, and the next run continues with the albums
not looked up yet. A lookup run by `aorg gnudb` or `aorg coverart` in another process is
not seen here; don't run both at once.
"""

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from . import coverart, gnudb
from .background import ERRORS_KEPT, Busy, Job, Jobs
from .models import Library

__all__ = ["ERRORS_KEPT", "KINDS", "LookupBusy", "LookupJob", "LookupJobs"]

KINDS = ("gnudb", "coverart")


def _lookup_function(kind: str) -> Callable[..., Any]:
    """The kind's lookup, found when a run starts so that tests can replace it."""
    return {"gnudb": gnudb, "coverart": coverart}[kind].lookup_library


class LookupBusy(Exception):
    def __init__(self, library: Library, kind: str):
        super().__init__(f"{kind} lookups of {library.name} are running already")


class LookupJob(Job):
    """A run of lookups of one kind; total and done count albums."""

    def __init__(self, kind: str, library_id: int, limit: int | None) -> None:
        super().__init__(library_id)
        self.kind = kind
        self.limit = limit
        self.left = 0  # albums not looked up before this run, in all
        self.found = 0
        self.not_found = 0

    def state(self) -> dict[str, Any]:
        return {**super().state(), "left": self.left, "found": self.found,
                "not_found": self.not_found}  # fmt: skip


class LookupJobs:
    """The lookup runs of this server, the last one of each kind per library."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._jobs: dict[str, Jobs[LookupJob]] = {kind: Jobs(f"{kind}-lookups") for kind in KINDS}

    def get(self, library: Library, kind: str) -> LookupJob | None:
        """The library's running job of this kind, or the last one this server ran."""
        return self._jobs[kind].get(library.id)

    def running(self, library: Library, kind: str) -> bool:
        return self._jobs[kind].running(library.id)

    def start(self, library: Library, kind: str, limit: int | None = None) -> LookupJob:
        """Start looking up the library's albums not looked up yet (at most `limit` of
        them) in a background thread; LookupBusy if a run of this kind is under way."""
        try:
            return self._jobs[kind].start(LookupJob(kind, library.id, limit), self._run)
        except Busy:
            raise LookupBusy(library, kind) from None

    def stop(self, library: Library, kind: str) -> None:
        """Ask the running job to stop; it stops once the album being looked up is done."""
        self._jobs[kind].stop(library.id)

    def _run(self, job: LookupJob) -> None:
        def started(total: int, left: int) -> None:
            job.total, job.left, job.status = total, left, "running"

        def progress(done: int, total: int, album: str, error: str | None) -> None:
            job.done, job.current = done, album
            if error:
                job.error(album, error)

        lookup = _lookup_function(job.kind)
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
