from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from async_jobs import AsyncJobStore, AsyncJobSupervisor, RESULT_RETENTION_SECONDS
from rql_client import RqlResult


class FakeApi:
    def __init__(self, state: str = "completed") -> None:
        self.state = state

    async def job_status(self, job_id: str):
        return {"id": job_id, "status": self.state}

    async def job_result(self, _job_id: str):
        return RqlResult(output="done\n", attachment=None, count=1)


class AsyncJobTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "state" / "jobs.sqlite3")

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_store_recovers_pending_jobs_after_restart(self) -> None:
        first = AsyncJobStore(self.path)
        first.add("job-1", 42, now=100)
        first.close()

        second = AsyncJobStore(self.path)
        try:
            jobs = second.due(now=100)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].job_id, "job-1")
            self.assertEqual(jobs[0].user_id, 42)
            self.assertEqual(second.count(), 1)
            columns = {row[1] for row in second._db.execute("PRAGMA table_info(pending_jobs)").fetchall()}
            self.assertEqual(
                columns,
                {
                    "job_id",
                    "user_id",
                    "submitted_at",
                    "next_attempt_at",
                    "attempts",
                },
            )
            self.assertNotIn("query", columns)
        finally:
            second.close()

    async def test_completed_job_is_delivered_then_deleted(self) -> None:
        store = AsyncJobStore(self.path)
        store.add("job-1", 42)
        delivered = []

        async def deliver(user_id, job_id, result, failed):
            delivered.append((user_id, job_id, result.output, failed))

        supervisor = AsyncJobSupervisor(FakeApi(), store, deliver, poll_seconds=0.1)
        await supervisor.tick()
        self.assertEqual(delivered, [(42, "job-1", "done\n", False)])
        self.assertEqual(store.count(), 0)
        store.close()

    async def test_dm_failure_keeps_job_for_retry(self) -> None:
        store = AsyncJobStore(self.path)
        store.add("job-1", 42)

        async def fail_delivery(*_args):
            raise RuntimeError("DMs disabled")

        supervisor = AsyncJobSupervisor(FakeApi(), store, fail_delivery, poll_seconds=0.1)
        await supervisor.tick()
        self.assertEqual(store.count(), 1)
        store.close()

    async def test_expired_delivery_is_removed(self) -> None:
        store = AsyncJobStore(self.path)
        submitted = time.time() - RESULT_RETENTION_SECONDS - 1
        store.add("job-1", 42, now=submitted)

        async def should_not_deliver(*_args):
            self.fail("expired result should not be delivered")

        supervisor = AsyncJobSupervisor(FakeApi(), store, should_not_deliver)
        await supervisor.tick(now=time.time())
        self.assertEqual(store.count(), 0)
        store.close()


if __name__ == "__main__":
    unittest.main()
