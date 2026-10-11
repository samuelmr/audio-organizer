"""Long actions of the web UI run as background jobs in a thread of the server.

A gnudb lookup of a whole library takes hours, and writing measurements into hundreds of
thousands of files takes about as long: far longer than a request may wait. The web UI
starts such an action in a thread and shows its progress, which the thread updates on a
job object (`Job`). One job runs per library and kind; the last one is kept to show how
it ended.

Jobs are kept in memory, as the sync plans are (sync_jobs.Previews): what a job does is
committed as it goes, so nothing is lost when the server stops, and the next run carries
on from the catalog. Syncs keep their jobs in the catalog instead (sync_jobs), as `aorg
sync` runs them in other processes too.
"""

import threading
import time
from collections.abc import Callable
from typing import Any

ERRORS_KEPT = 100


class Busy(Exception):
    """A job of this kind is running for the library already."""


class Job:
    """A run of a long action, its progress updated by the thread that runs it.
    Subclasses add the counts their action reports."""

    def __init__(self, library_id: int) -> None:
        self.library_id = library_id
        self.status = "starting"  # starting | running | done | stopped | failed
        self.total = 0
        self.done = 0
        self.failed = 0
        self.errors: list[tuple[str, str]] = []  # what, error
        self.current: str | None = None
        self.message: str | None = None
        self.stop_requested = False
        self.started = time.time()
        self.finished: float | None = None

    @property
    def active(self) -> bool:
        return self.status in ("starting", "running")

    def error(self, what: str, error: str) -> None:
        """Count a failure, and keep the first ERRORS_KEPT of them to show."""
        self.failed += 1
        if len(self.errors) < ERRORS_KEPT:
            self.errors.append((what, error))

    def state(self) -> dict[str, Any]:
        """The job as JSON, for the progress page's script (sync.js)."""
        return {
            "status": self.status, "active": self.active, "total": self.total,
            "done": self.done, "failed": self.failed,
            "errors": [list(e) for e in self.errors], "current": self.current,
            "message": self.message, "stop_requested": self.stop_requested,
        }  # fmt: skip


class Jobs[J: Job]:
    """The jobs of one kind run by this server, the last one per library."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._by_library: dict[int, J] = {}
        self._lock = threading.Lock()

    def get(self, library_id: int) -> J | None:
        """The library's running job, or the last one this server ran."""
        with self._lock:
            return self._by_library.get(library_id)

    def running(self, library_id: int) -> bool:
        job = self.get(library_id)
        return job is not None and job.active

    def stop(self, library_id: int) -> None:
        """Ask the running job to stop; it stops at the next point where it can."""
        job = self.get(library_id)
        if job is not None and job.active:
            job.stop_requested = True

    def start(
        self, job: J, work: Callable[[J], None], finished: Callable[[], None] | None = None
    ) -> J:
        """Run work(job) in a background thread; Busy if a job of this library is running.
        work sets the job's status and message as it ends; an exception it raises fails
        the job. finished is called when the thread ends, however it ends; if start raises,
        there is no thread and finished is not called."""
        with self._lock:
            running = self._by_library.get(job.library_id)
            if running is not None and running.active:
                raise Busy
            self._by_library[job.library_id] = job

        def run() -> None:
            try:
                work(job)
            except Exception as e:
                job.status = "failed"
                job.message = f"{type(e).__name__}: {e}"
            finally:
                job.current = None
                job.finished = time.time()
                if job.active:  # work returned without saying how it ended
                    job.status = "done"
                if finished:
                    finished()

        try:
            threading.Thread(target=run, name=f"{self._name}-{job.library_id}", daemon=True).start()
        except BaseException:
            job.status, job.finished = "failed", time.time()
            raise
        return job
