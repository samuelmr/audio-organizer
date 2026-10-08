"""Sync runs as jobs: progress that any page can show, and a lock per target.

A sync started from the web UI runs in a background thread of the server; one started
with `aorg sync` runs in that process. Both record a SyncJob, so the web UI shows the
progress of either, and neither starts while the other is syncing the same target.

A job's row is updated about once a second, on the session that records the synced
objects, so that SQLite never holds a write transaction for long. A job whose process
is gone (the server was stopped, the CLI was killed) counts as interrupted when it is
next looked at; nothing is lost, as the catalog records each object as it is synced and
the next sync carries on from there.

Planning is slow on a network share (it looks for every folder's cover), so the web UI
plans in the background too: a `Previews` cache per server holds the plan shown on a
target's page, with its progress while it is made. Starting a sync reuses that plan, as
what runs is then exactly what the user reviewed.
"""

import os
import socket
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from . import sync as sy
from .models import SyncJob, SyncTarget, utcnow
from .objectstore import ObjectStore

ACTIVE = ("planning", "running")
ERRORS_KEPT = 100
HEARTBEAT = 1.0  # seconds between progress updates
# A job of a process on another machine (a shared database) counts as interrupted when
# it has not reported for this long; planning on a slow share reports nothing for minutes.
STALE = timedelta(minutes=15)
PREVIEW_AGE = 10 * 60  # seconds a shown plan can be started from; then it is made again

_HOST = socket.gethostname()
_running: set[int] = set()  # jobs run by this process
_lock = threading.Lock()


class SyncBusy(Exception):
    def __init__(self, job: SyncJob):
        super().__init__(
            f"Target {job.target.name} is already being synced (started from the "
            f"{job.origin} at {job.started_at:%Y-%m-%d %H:%M})"
        )
        self.job = job


def _alive(job: SyncJob) -> bool:
    if job.host != _HOST:
        heartbeat = job.heartbeat_at
        if heartbeat.tzinfo is None:  # SQLite returns naive datetimes
            heartbeat = heartbeat.replace(tzinfo=utcnow().tzinfo)
        return utcnow() - heartbeat < STALE
    if job.pid == os.getpid():
        with _lock:
            return job.id in _running
    try:
        os.kill(job.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # someone else's process with that number
    return True


def is_active(job: SyncJob) -> bool:
    return job.status in ACTIVE


def active_job(session: Session, target: SyncTarget) -> SyncJob | None:
    """The job syncing the target now, if any. Jobs whose process is gone are marked
    interrupted on the way."""
    found = None
    for job in session.scalars(
        select(SyncJob).where(SyncJob.target_id == target.id, SyncJob.status.in_(ACTIVE))
    ):
        if _alive(job):
            found = job
        else:
            _close(job, "interrupted", "The process running the sync stopped; sync again to resume")
    session.commit()
    return found


def refresh(session: Session, job: SyncJob) -> SyncJob:
    """The job as stored now, marked interrupted if its process is gone."""
    session.refresh(job)
    if is_active(job) and not _alive(job):
        _close(job, "interrupted", "The process running the sync stopped; sync again to resume")
        session.commit()
    return job


def recent_jobs(session: Session, target: SyncTarget, limit: int = 5) -> list[SyncJob]:
    active_job(session, target)  # mark interrupted ones first
    return list(session.scalars(
        select(SyncJob).where(SyncJob.target_id == target.id)
        .order_by(SyncJob.id.desc()).limit(limit)
    ))  # fmt: skip


def _throttled(report: Callable[[int, int], None]) -> Callable[[int, int], None]:
    """`report`, called at most once per HEARTBEAT (and for the last item)."""
    last = 0.0

    def call(done: int, total: int) -> None:
        nonlocal last
        now = time.monotonic()
        if done >= total or now - last >= HEARTBEAT:
            last = now
            report(done, total)

    return call


class Preview:
    """A plan being made for a target's page, or ready to be shown and started."""

    def __init__(self) -> None:
        self.status = "planning"  # planning | ready | failed
        self.done = 0  # tracks checked
        self.total = 0
        self.plan: sy.Plan | None = None
        self.error: str | None = None
        self.started = time.monotonic()
        self.finished: float | None = None

    def fresh(self) -> bool:
        return self.status == "planning" or (
            self.status == "ready"
            and self.finished is not None
            and time.monotonic() - self.finished < PREVIEW_AGE
        )


class Previews:
    """The plans shown on the Sync page, made in background threads of this process, one
    per target. Kept in memory: a plan is cheap to make again after a restart."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._by_target: dict[int, Preview] = {}
        self._lock = threading.Lock()

    def get(self, target: SyncTarget, *, again: bool = False) -> Preview:
        """The target's plan, started in the background if there is none, it is stale or
        `again` is set. A plan being made is never started twice."""
        with self._lock:
            preview = self._by_target.get(target.id)
            if preview is not None and (
                preview.status == "planning" or (preview.fresh() and not again)
            ):
                return preview
            preview = self._by_target[target.id] = Preview()
        threading.Thread(
            target=self._make, args=(target.id, preview), name=f"sync-plan-{target.id}",
            daemon=True,
        ).start()  # fmt: skip
        return preview

    def ready_plan(self, target: SyncTarget, expected: str | None) -> sy.Plan | None:
        """The plan shown for the target, if it is still fresh and is the expected one."""
        with self._lock:
            preview = self._by_target.get(target.id)
        if preview is None or preview.status != "ready" or not preview.fresh():
            return None
        if expected is None or preview.plan is None or preview.plan.fingerprint() != expected:
            return None
        return preview.plan

    def forget(self, target: SyncTarget) -> None:
        with self._lock:
            self._by_target.pop(target.id, None)

    def _make(self, target_id: int, preview: Preview) -> None:
        def progress(done: int, total: int) -> None:
            preview.done, preview.total = done, total

        try:
            with self._session_factory() as session:
                target = session.get(SyncTarget, target_id)
                preview.plan = sy.plan(session, target, progress=progress)
            preview.status = "ready"
        except Exception as e:
            preview.error = f"{type(e).__name__}: {e}"
            preview.status = "failed"
        finally:
            preview.finished = time.monotonic()


def start(session: Session, target: SyncTarget, origin: str) -> SyncJob:
    """Claim the target for a sync by this process; SyncBusy if one is running."""
    busy = active_job(session, target)
    if busy is not None:
        raise SyncBusy(busy)
    job = SyncJob(target=target, origin=origin, status="planning", host=_HOST, pid=os.getpid())
    session.add(job)
    session.commit()
    with _lock:
        _running.add(job.id)
    return job


def request_stop(session: Session, job: SyncJob) -> None:
    """Ask a job to stop after the transfers under way; it is picked up within a second."""
    if is_active(job):
        job.stop_requested = True
        session.commit()


def run(
    session: Session,
    job: SyncJob,
    plan: sy.Plan,
    store: ObjectStore,
    *,
    workers: int = 4,
    on_action: Callable[[sy.Action, str | None], None] | None = None,
) -> sy.SyncResult | None:
    """Carry out a plan for a started job, keeping the job's progress up to date.
    Returns None when the job was asked to stop before it began."""
    try:
        session.refresh(job, ["stop_requested"])
        if job.stop_requested:
            _close(job, "stopped", "Stopped before anything was synced")
            return None
        # total and done counted tracks while planning; now they count actions
        job.status, job.total, job.bytes_total = "running", len(plan.actions), plan.upload_bytes
        job.done = 0
        session.commit()
        last = time.monotonic()

        def progress(action: sy.Action, error: str | None) -> None:
            nonlocal last
            job.done += 1
            job.current = action.describe()
            if error:
                job.failed += 1
                if len(job.errors) < ERRORS_KEPT:
                    job.errors = [*job.errors, [action.describe(), error]]
            elif action.kind == "upload":
                job.bytes_done += action.size
            if on_action:
                on_action(action, error)
            if time.monotonic() - last >= HEARTBEAT:
                last = time.monotonic()
                job.heartbeat_at = utcnow()
                session.commit()  # also commits the objects recorded so far
                session.refresh(job, ["stop_requested"])

        result = sy.execute(session, job.target, store, plan.actions, workers=workers,
                            progress=progress, should_stop=lambda: job.stop_requested)  # fmt: skip
        if result.stopped:
            _close(job, "stopped", f"Stopped after {job.done} of {job.total}; sync again to resume")
        else:
            errors = f"; {job.failed} failed" if job.failed else ""
            _close(job, "done", f"Synced {job.done - job.failed} of {job.total}{errors}")
        return result
    except BaseException as e:
        session.rollback()
        if isinstance(e, KeyboardInterrupt):
            _close(job, "stopped", "Interrupted; sync again to resume")
        else:
            _close(job, "failed", f"{type(e).__name__}: {e}")
        raise
    finally:
        session.commit()
        with _lock:
            _running.discard(job.id)


def fail(session: Session, job: SyncJob, status: str, message: str) -> None:
    """End a job that did not get to run its plan (planning failed, or gave another plan)."""
    _close(job, status, message)
    session.commit()
    with _lock:
        _running.discard(job.id)


def _close(job: SyncJob, status: str, message: str) -> None:
    job.status, job.message = status, message
    job.finished_at = job.heartbeat_at = utcnow()
    job.current = None


def start_in_background(
    session_factory: sessionmaker[Session],
    session: Session,
    target: SyncTarget,
    expected: str | None,
    *,
    plan: sy.Plan | None = None,
    workers: int = 4,
    store_for: Callable[[SyncTarget], ObjectStore] = sy.store_for,
) -> SyncJob:
    """Start a sync of the target in a thread of this process and return its job at
    once. With the plan the user reviewed, that plan runs. Without it, the thread plans
    again (showing its progress) and only runs a plan with the expected fingerprint, so
    that what runs is what the user reviewed. SyncBusy if the target is being synced
    already."""
    job = start(session, target, "web")
    job_id = job.id

    def work() -> None:
        with session_factory() as worker:
            job = worker.get(SyncJob, job_id)
            try:
                to_run = plan
                if to_run is None:
                    to_run = sy.plan(worker, job.target, progress=_throttled(
                        lambda done, total: _planned(worker, job, done, total)
                    ))  # fmt: skip
                    if expected is not None and to_run.fingerprint() != expected:
                        fail(worker, job, "changed",
                             "The plan changed since it was shown (files were scanned or "
                             "edited); review it again")  # fmt: skip
                        return
                run(worker, job, to_run, store_for(job.target), workers=workers)
            except Exception as e:
                if is_active(job):
                    worker.rollback()
                    fail(worker, job, "failed", f"{type(e).__name__}: {e}")

    threading.Thread(target=work, name=f"sync-job-{job_id}", daemon=True).start()
    return job


def _planned(session: Session, job: SyncJob, done: int, total: int) -> None:
    """Record how far planning has come, which also shows that the job is alive."""
    job.done, job.total, job.heartbeat_at = done, total, utcnow()
    session.commit()


def state(job: SyncJob) -> dict[str, Any]:
    """The job as JSON, for the progress page's script."""
    return {
        "id": job.id,
        "target": job.target.name,
        "origin": job.origin,
        "status": job.status,
        "active": is_active(job),
        "stop_requested": job.stop_requested,
        "total": job.total,
        "done": job.done,
        "failed": job.failed,
        "bytes_total": job.bytes_total,
        "bytes_done": job.bytes_done,
        "current": job.current,
        "message": job.message,
        "errors": job.errors,
        "started_at": job.started_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }
