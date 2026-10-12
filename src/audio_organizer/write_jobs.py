"""Writing many files (writing.write_gaps, write_all) as a background job of the web UI:
all measurements and derived ratings, or everything pending in a selection the user
reviewed.

After a deep scan of a large library, hundreds of thousands of files wait for their
ReplayGain values: hours of writing, far longer than a request may wait; so may a review
of thousands of edited tracks. The job runs in a thread of the server (see background), a
changeset per batch, and shows
how far it has come and which changesets it has written. It can be stopped between
batches; a job that is stopped or fails keeps the changesets written so far, each of which
can be undone, and the next run writes the files still lacking their values.
"""

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from . import writing as wr
from .background import Job, Jobs
from .models import Library

BATCH = 500  # files per changeset


class WriteGapsJob(Job):
    """A run of write_gaps, or with gaps False of write_all; total and done count the
    files looked at."""

    def __init__(
        self,
        library_id: int,
        selection: wr.Selection,
        id3_version: int | None,
        gaps: bool = True,
    ):
        super().__init__(library_id)
        self.selection = selection
        self.gaps = gaps
        self.id3_version = id3_version
        self.changesets: list[int] = []
        self.written = 0  # files written

    def state(self) -> dict[str, Any]:
        return {**super().state(), "changesets": self.changesets, "written": self.written}


class WriteGapsJobs:
    """The write_gaps runs of this server, the last one per library."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._jobs: Jobs[WriteGapsJob] = Jobs("write-gaps")

    def get(self, library: Library) -> WriteGapsJob | None:
        """The library's running job, or the last one this server ran."""
        return self._jobs.get(library.id)

    def start(
        self,
        library: Library,
        selection: wr.Selection,
        id3_version: int | None = None,
        finished: Callable[[], None] | None = None,
        gaps: bool = True,
    ) -> WriteGapsJob:
        """Start writing in a background thread: the measured and derived values filling
        empty tags (gaps), or everything pending in the selection. background.Busy if a
        run of this library is under way. finished is called when the run ends, however it
        ends (and not if this raises)."""
        job = WriteGapsJob(library.id, selection, id3_version, gaps)
        return self._jobs.start(job, self._run, finished)

    def stop(self, library: Library) -> None:
        """Ask the running job to stop; it stops once the batch being written is done."""
        self._jobs.stop(library.id)

    def _run(self, job: WriteGapsJob) -> None:
        def progress(done: int, total: int) -> None:
            job.done, job.total, job.status = done, total, "running"

        def step(what: str) -> None:
            job.current = what

        def written_batch(changeset_id: int, written: int, failures: list[str]) -> None:
            job.changesets.append(changeset_id)
            job.written += written
            for message in failures:
                job.error(f"Changeset {changeset_id}", message)

        # A session of its own: the request that started the job has ended, and write_gaps
        # expunges everything from its session after each batch
        with self._session_factory() as session:
            library = session.get(Library, job.library_id)
            try:
                write = wr.write_gaps if job.gaps else wr.write_all
                write(
                    session, library, job.selection, job.id3_version, BATCH,
                    progress=progress, written_batch=written_batch,
                    should_stop=lambda: job.stop_requested, step=step,
                )  # fmt: skip
            except Exception:
                session.rollback()  # the changesets written so far are committed
                raise
            finally:
                job.current = None
        failed = f"; {job.failed} failed" if job.failed else ""
        wrote = f"Wrote {job.written:,} files in {len(job.changesets)} changesets{failed}"
        if job.stop_requested and job.done < job.total:
            job.status = "stopped"
            job.message = f"Stopped after {job.done:,} of {job.total:,} files. {wrote}"
        else:
            job.status = "done"
            job.message = (
                f"{wrote}."
                if job.total
                else "Nothing to write: no file lacks its values."
                if job.gaps
                else "Nothing to write: no file has pending changes."
            )
