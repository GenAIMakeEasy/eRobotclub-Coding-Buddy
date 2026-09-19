"""A bounded FIFO queue in front of the model backend.

Ollama's `OLLAMA_NUM_PARALLEL` defaults to 1 and this project runs it at 2
(see design/spec.md and the grilling session, round on load) — inference is
effectively serialised on a 16GB machine. Left alone, requests would pile up
invisibly against Ollama's own `OLLAMA_MAX_QUEUE` (512 deep, then a bare 503).
This queue sits in front of that: bounded to a much smaller, adjustable
depth, with an honest visible position and wait estimate for every caller
(Q2), and a "busy" response once that depth is exceeded rather than an
indefinitely growing line.

A job's position counts every not-yet-finished job ahead of it, running or
still queued — that is what "you are 3rd" means to a student, regardless of
which of those 3 already occupies an execution slot.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


class QueueFull(Exception):
    """Raised by submit() when the queue is at its adjustable depth limit.
    The caller should show the student a busy message, not a position."""


@dataclass
class Job:
    id: int
    status: JobStatus = JobStatus.queued
    submitted_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    finished_at: float | None = None
    result: Any = None
    error: str | None = None


class QueueManager:
    def __init__(
        self,
        concurrency: int = 2,
        max_queue_depth: int = 20,
        min_wait_seconds: float = 10.0,
        max_wait_seconds: float = 30.0,
        job_retention_seconds: float = 3600.0,
    ) -> None:
        self.concurrency = concurrency
        self.max_queue_depth = max_queue_depth
        self.min_wait_seconds = min_wait_seconds
        self.max_wait_seconds = max_wait_seconds
        self.job_retention_seconds = job_retention_seconds

        self._jobs: dict[int, Job] = {}
        self._order: list[int] = []  # submission order, for position ranking
        self._ids = itertools.count(1)
        self._work: asyncio.Queue[tuple[int, Callable[[], Awaitable[Any]]]] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []

    def start(self) -> None:
        if self._workers:
            return
        self._workers = [asyncio.create_task(self._worker_loop()) for _ in range(self.concurrency)]

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        for w in self._workers:
            try:
                await w
            except asyncio.CancelledError:
                pass
        self._workers = []

    def _unfinished_count(self) -> int:
        return sum(1 for j in self._jobs.values() if j.status in (JobStatus.queued, JobStatus.running))

    def active_job_count(self) -> int:
        """Public wrapper over `_unfinished_count` -- used by
        app/api/main.py's /admin/restart to warn an admin how many student
        requests are still in flight before it drops them."""
        return self._unfinished_count()

    def submit(self, handler: Callable[[], Awaitable[Any]]) -> Job:
        self._purge_old_jobs()
        if self._unfinished_count() >= self.max_queue_depth:
            raise QueueFull(f"queue is at its limit of {self.max_queue_depth}")

        job = Job(id=next(self._ids))
        self._jobs[job.id] = job
        self._order.append(job.id)
        self._work.put_nowait((job.id, handler))
        return job

    def get_job(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)

    def position(self, job_id: int) -> int | None:
        """1-based rank among not-yet-finished jobs, by submission order.
        None once the job has finished (there is nothing left to wait for)."""
        job = self._jobs.get(job_id)
        if job is None or job.status in (JobStatus.done, JobStatus.failed):
            return None
        rank = 0
        for jid in self._order:
            j = self._jobs[jid]
            if j.status in (JobStatus.queued, JobStatus.running):
                rank += 1
                if jid == job_id:
                    return rank
        return None

    def estimated_wait_seconds(self, job_id: int) -> tuple[float, float] | None:
        """An honest range, not a promise — screen 3 shows "~24 seconds", not
        "24.000 seconds". Jobs ahead are cleared `concurrency` at a time."""
        pos = self.position(job_id)
        if pos is None:
            return None
        jobs_ahead = pos - 1
        batches_ahead_low = jobs_ahead // self.concurrency
        batches_ahead_high = jobs_ahead // self.concurrency + (1 if jobs_ahead % self.concurrency else 0)
        low = batches_ahead_low * self.min_wait_seconds
        high = (batches_ahead_high + 1) * self.max_wait_seconds
        return (low, high)

    def _purge_old_jobs(self) -> None:
        cutoff = time.monotonic() - self.job_retention_seconds
        stale = [
            jid for jid, j in self._jobs.items()
            if j.status in (JobStatus.done, JobStatus.failed) and (j.finished_at or 0) < cutoff
        ]
        for jid in stale:
            del self._jobs[jid]
            self._order.remove(jid)

    async def _worker_loop(self) -> None:
        while True:
            job_id, handler = await self._work.get()
            job = self._jobs.get(job_id)
            if job is None:
                continue
            job.status = JobStatus.running
            job.started_at = time.monotonic()
            try:
                job.result = await handler()
                job.status = JobStatus.done
            except Exception as exc:  # the job fails; the queue keeps running
                job.error = str(exc)
                job.status = JobStatus.failed
            finally:
                job.finished_at = time.monotonic()
