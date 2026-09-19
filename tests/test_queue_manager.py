"""The queue is what turns Ollama's serialised inference into an honest
student-facing wait, rather than a stalled spinner (Q2). No pytest-asyncio
dependency — each test drives its own event loop with asyncio.run()."""

import asyncio

import pytest

from app.queue.manager import JobStatus, QueueFull, QueueManager


def run(coro):
    return asyncio.run(coro)


def test_a_single_job_runs_and_completes():
    async def scenario():
        qm = QueueManager(concurrency=2, max_queue_depth=5)
        qm.start()
        job = qm.submit(lambda: _sleep_and_return(0.01, "ok"))
        await asyncio.sleep(0.05)
        assert job.status == JobStatus.done
        assert job.result == "ok"
        await qm.stop()

    run(scenario())


def test_position_counts_unfinished_jobs_ahead_by_submission_order():
    async def scenario():
        qm = QueueManager(concurrency=1, max_queue_depth=10)
        qm.start()
        j1 = qm.submit(lambda: _sleep_and_return(0.05, "a"))
        j2 = qm.submit(lambda: _sleep_and_return(0.05, "b"))
        j3 = qm.submit(lambda: _sleep_and_return(0.05, "c"))
        await asyncio.sleep(0.01)  # let j1 claim the single worker
        assert qm.position(j1.id) == 1
        assert qm.position(j2.id) == 2
        assert qm.position(j3.id) == 3
        await qm.stop()

    run(scenario())


def test_position_is_none_once_a_job_finishes():
    async def scenario():
        qm = QueueManager(concurrency=1, max_queue_depth=5)
        qm.start()
        job = qm.submit(lambda: _sleep_and_return(0.01, "done"))
        await asyncio.sleep(0.05)
        assert qm.position(job.id) is None
        await qm.stop()

    run(scenario())


def test_queue_full_is_raised_past_the_depth_limit():
    async def scenario():
        qm = QueueManager(concurrency=1, max_queue_depth=2)
        qm.start()
        qm.submit(lambda: _sleep_and_return(1, "a"))
        qm.submit(lambda: _sleep_and_return(1, "b"))
        with pytest.raises(QueueFull):
            qm.submit(lambda: _sleep_and_return(1, "c"))
        await qm.stop()

    run(scenario())


def test_a_failing_job_does_not_break_the_worker():
    async def scenario():
        qm = QueueManager(concurrency=1, max_queue_depth=5)
        qm.start()

        async def boom():
            raise ValueError("model backend unreachable")

        j1 = qm.submit(boom)
        j2 = qm.submit(lambda: _sleep_and_return(0.01, "still works"))
        await asyncio.sleep(0.05)
        assert j1.status == JobStatus.failed
        assert "unreachable" in j1.error
        assert j2.status == JobStatus.done
        assert j2.result == "still works"
        await qm.stop()

    run(scenario())


def test_two_workers_clear_two_jobs_in_one_batch():
    async def scenario():
        qm = QueueManager(concurrency=2, max_queue_depth=10)
        qm.start()
        j1 = qm.submit(lambda: _sleep_and_return(0.05, "a"))
        j2 = qm.submit(lambda: _sleep_and_return(0.05, "b"))
        j3 = qm.submit(lambda: _sleep_and_return(0.05, "c"))
        await asyncio.sleep(0.01)
        # third job waits for one of the first two batches to clear
        assert qm.position(j3.id) == 3
        await asyncio.sleep(0.1)
        assert j1.status == JobStatus.done
        assert j2.status == JobStatus.done
        assert j3.status == JobStatus.done
        await qm.stop()

    run(scenario())


def test_estimated_wait_grows_with_position_and_shrinks_with_concurrency():
    async def scenario():
        qm_slow = QueueManager(concurrency=1, max_queue_depth=10, min_wait_seconds=10, max_wait_seconds=30)
        qm_slow.start()
        for _ in range(3):
            qm_slow.submit(lambda: _sleep_and_return(5, "x"))
        await asyncio.sleep(0.01)
        last_job = list(qm_slow._jobs.values())[-1]
        low, high = qm_slow.estimated_wait_seconds(last_job.id)
        assert low > 0
        assert high > low
        await qm_slow.stop()

    run(scenario())


def test_estimated_wait_is_none_after_completion():
    async def scenario():
        qm = QueueManager(concurrency=1, max_queue_depth=5)
        qm.start()
        job = qm.submit(lambda: _sleep_and_return(0.01, "x"))
        await asyncio.sleep(0.05)
        assert qm.estimated_wait_seconds(job.id) is None
        await qm.stop()

    run(scenario())


async def _sleep_and_return(delay: float, value):
    await asyncio.sleep(delay)
    return value
