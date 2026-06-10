"""In-process async task store for long gateway runs (M1.1).

Deliberately not Redis/Arq yet: locally a process-lifetime registry of asyncio tasks
satisfies the contract (submit → task id → poll/stream status). The interface is the
seam — a Redis-backed implementation slots in at deploy time (P4) without touching
callers.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

TaskState = Literal["queued", "running", "done", "error"]


@dataclass
class TaskRecord:
    id: str
    state: TaskState = "queued"
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    finished_at: dt.datetime | None = None
    result: Any | None = None
    error: str | None = None
    # progress events (recorder lines) for status streaming
    events: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)


class TaskStore:
    """Submit coroutines, poll status, drain progress events."""

    def __init__(self, max_tasks: int = 1000) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._handles: dict[str, asyncio.Task[Any]] = {}
        self._max = max_tasks

    def submit(self, factory: Callable[[TaskRecord], Awaitable[Any]]) -> TaskRecord:
        if len(self._tasks) >= self._max:
            self._evict_finished()
        record = TaskRecord(id=uuid.uuid4().hex)
        self._tasks[record.id] = record

        async def runner() -> None:
            record.state = "running"
            try:
                record.result = await factory(record)
                record.state = "done"
            except Exception as e:  # surfaced via status, never lost
                record.state = "error"
                record.error = f"{type(e).__name__}: {e}"
            finally:
                record.finished_at = dt.datetime.now(dt.UTC)
                await record.events.put({"event": "end", "state": record.state})

        self._handles[record.id] = asyncio.create_task(runner())
        return record

    def get(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def _evict_finished(self) -> None:
        for tid, rec in list(self._tasks.items()):
            if rec.state in ("done", "error"):
                self._tasks.pop(tid, None)
                self._handles.pop(tid, None)

    async def aclose(self) -> None:
        for handle in self._handles.values():
            handle.cancel()
