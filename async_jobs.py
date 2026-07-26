from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from rql_client import RqlClient, RqlClientError, RqlResult


RESULT_RETENTION_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class PendingJob:
    job_id: str
    user_id: int
    submitted_at: float
    next_attempt_at: float
    attempts: int


class AsyncJobStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_jobs (
                job_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                submitted_at REAL NOT NULL,
                next_attempt_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._db.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @classmethod
    def from_env(cls) -> AsyncJobStore:
        return cls(os.environ.get("RQL_STATE_PATH", ".state/querybot.sqlite3"))

    def add(self, job_id: str, user_id: int, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        self._db.execute(
            """
            INSERT OR REPLACE INTO pending_jobs
                (job_id, user_id, submitted_at, next_attempt_at, attempts)
            VALUES (?, ?, ?, ?, 0)
            """,
            (job_id, user_id, timestamp, timestamp),
        )
        self._db.commit()

    def due(self, *, now: float | None = None) -> list[PendingJob]:
        timestamp = time.time() if now is None else now
        rows = self._db.execute(
            """
            SELECT job_id, user_id, submitted_at, next_attempt_at, attempts
            FROM pending_jobs
            WHERE next_attempt_at <= ?
            ORDER BY submitted_at, job_id
            """,
            (timestamp,),
        ).fetchall()
        return [PendingJob(**dict(row)) for row in rows]

    def reschedule(self, job_id: str, delay_seconds: float, *, delivery_failure: bool) -> None:
        increment = 1 if delivery_failure else 0
        self._db.execute(
            """
            UPDATE pending_jobs
            SET next_attempt_at = ?, attempts = attempts + ?
            WHERE job_id = ?
            """,
            (time.time() + delay_seconds, increment, job_id),
        )
        self._db.commit()

    def delete(self, job_id: str) -> None:
        self._db.execute("DELETE FROM pending_jobs WHERE job_id = ?", (job_id,))
        self._db.commit()

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM pending_jobs").fetchone()[0])

    def close(self) -> None:
        self._db.close()


DeliverResult = Callable[[int, str, RqlResult | None, bool], Awaitable[None]]


class AsyncJobSupervisor:
    def __init__(
        self,
        api: RqlClient,
        store: AsyncJobStore,
        deliver: DeliverResult,
        *,
        poll_seconds: float = 2.0,
    ) -> None:
        self.api = api
        self.store = store
        self.deliver = deliver
        self.poll_seconds = max(poll_seconds, 0.1)

    async def run(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(self.poll_seconds)

    async def tick(self, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        for job in self.store.due(now=timestamp):
            if timestamp - job.submitted_at >= RESULT_RETENTION_SECONDS:
                self.store.delete(job.job_id)
                continue
            await self._process(job)

    async def _process(self, job: PendingJob) -> None:
        try:
            status = await self.api.job_status(job.job_id)
        except RqlClientError:
            self.store.reschedule(job.job_id, self._delivery_backoff(job.attempts), delivery_failure=True)
            return

        state = status.get("status")
        if state in {"queued", "running"}:
            self.store.reschedule(job.job_id, self.poll_seconds, delivery_failure=False)
            return
        if state not in {"completed", "failed"}:
            self.store.reschedule(job.job_id, self.poll_seconds, delivery_failure=False)
            return

        try:
            if state == "completed":
                result = await self.api.job_result(job.job_id)
                await self.deliver(job.user_id, job.job_id, result, False)
            else:
                await self.deliver(job.user_id, job.job_id, None, True)
        except Exception:
            self.store.reschedule(job.job_id, self._delivery_backoff(job.attempts), delivery_failure=True)
            return

        self.store.delete(job.job_id)

    @staticmethod
    def _delivery_backoff(attempts: int) -> float:
        return min(60.0 * (2 ** min(attempts, 6)), 3600.0)
