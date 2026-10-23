"""Best-effort outbound alerts, isolated from interactive role operations."""

import asyncio
import contextlib
import logging
import time

import aiohttp

from .logging_setup import Redactor

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, url: str | None, redact: Redactor):
        self._url, self._redact = url, redact
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=32)
        self._recent: dict[str, float] = {}
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._url and self._task is None:
            self._task = asyncio.create_task(self._worker(), name="admin-notifications")

    def notify(self, key: str, message: str) -> None:
        if not self._url:
            return
        now = time.monotonic()
        self._recent = {
            key: deadline for key, deadline in self._recent.items() if deadline > now
        }
        if key in self._recent:
            return
        self._recent[key] = now + 300
        try:
            self._queue.put_nowait(self._redact(message)[:1900])
        except asyncio.QueueFull:
            log.warning("admin_notification_queue_full")

    async def _worker(self) -> None:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10), trust_env=False
        ) as session:
            while True:
                message = await self._queue.get()
                try:
                    async with session.post(
                        self._url,
                        json={"content": message, "allowed_mentions": {"parse": []}},
                        allow_redirects=False,
                    ) as response:
                        if response.status not in {200, 204}:
                            log.warning(
                                "admin_notification_rejected status=%s", response.status
                            )
                except (aiohttp.ClientError, TimeoutError):
                    log.warning("admin_notification_unavailable")
                finally:
                    self._queue.task_done()

    async def close(self) -> None:
        if self._task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._queue.join(), timeout=2)
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
