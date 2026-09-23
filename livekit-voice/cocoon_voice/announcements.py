"""Proactive announcements: poll the backend's retained events and speak them in order.

Delivery is at-least-once-ish, not exactly-once: progress is persisted in the backend
as delivery reports, so reconnects skip events already played/interrupted/expired,
but a crash between playout and the report can replay one announcement.
"played" only means the audio finished; it is not operator acknowledgement.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, Protocol

from . import contract as c
from .backend_client import BackendClient, BackendError

log = logging.getLogger("cocoon_voice.announcements")

TERMINAL = {"played", "interrupted", "expired"}  # a later worker will retry only "failed" (if not expired)


class Speaker(Protocol):
    async def wait_until_quiet(self, max_wait: float) -> None: ...

    def say(self, text: str) -> Any: ...  # returns a SpeechHandle-like object


class AnnouncementPump:
    def __init__(
        self,
        client: BackendClient,
        session_id: Callable[[], str | None],
        speaker: Speaker,
        consumer_id: str,
        *,
        interval: float = 1.0,
        max_backoff: float = 30.0,
        quiet_wait: float = 4.0,
        playout_timeout: float = 30.0,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ):
        self._client = client
        self._session_id = session_id
        self._speaker = speaker
        self._consumer_id = consumer_id
        self._interval = interval
        self._max_backoff = max_backoff
        self._quiet_wait = quiet_wait
        self._playout_timeout = playout_timeout
        self._now = now
        self._sleep = sleep
        self._cursor = 0
        self._handled: set[str] = set()
        self._task: asyncio.Task | None = None
        self.outcomes: list[tuple[str, str]] = []  # (event_id, status) for logs/tests

    @property
    def cursor(self) -> int:
        return self._cursor

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="cocoon-announcements")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run(self) -> None:
        failures = 0
        while True:
            try:
                has_more = await self.poll_once()
                failures = 0
            except asyncio.CancelledError:
                raise
            except BackendError as exc:
                failures += 1
                delay = min(self._max_backoff, self._interval * 2 ** failures) * random.uniform(0.8, 1.2)
                log.warning("event poll failed (%s); retry in %.1fs", exc, delay)
                await self._sleep(delay)
                continue
            except Exception:
                failures += 1
                log.exception("unexpected error in announcement loop")
                await self._sleep(min(self._max_backoff, self._interval * 2 ** failures))
                continue
            if not has_more:
                await self._sleep(self._interval)

    async def poll_once(self) -> bool:
        session_id = self._session_id()
        if session_id is None:  # not bound to a backend session yet
            return False
        page = await self._client.list_events(session_id, after=self._cursor)
        for event in page.events:
            if event.event_id not in self._handled:
                await self._handle(event)
                self._handled.add(event.event_id)
            self._cursor = max(self._cursor, event.sequence)
        return page.has_more

    async def _handle(self, event: c.Announcement) -> None:
        prior = {d.status for d in event.deliveries}
        if prior & TERMINAL:
            log.info("skip announcement %s: already %s", event.event_id, sorted(prior & TERMINAL))
            return
        status, detail = await self._play(event)
        self.outcomes.append((event.event_id, status))
        try:
            await self._client.report_delivery(self._session_id() or "", event.event_id, self._consumer_id, status,
                                               detail)
        except BackendError as exc:
            # Keep it handled locally so routine polling does not replay it; a restart may replay it once.
            log.warning("could not record delivery for %s (%s): %s", event.event_id, status, exc)

    def _expired(self, event: c.Announcement) -> bool:
        return event.expires_at is not None and self._now() >= event.expires_at

    async def _play(self, event: c.Announcement) -> tuple[str, str | None]:
        if self._expired(event):
            return "expired", "expired before playback"
        await self._speaker.wait_until_quiet(self._quiet_wait)
        if self._expired(event):
            return "expired", "expired while waiting for a quiet moment"
        try:
            handle = self._speaker.say(event.speech)
            await asyncio.wait_for(handle.wait_for_playout(), timeout=self._playout_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("announcement %s playback failed: %s", event.event_id, exc)
            return "failed", f"{type(exc).__name__}: {exc}"[:200]
        if handle.interrupted:
            return "interrupted", "operator spoke over the announcement"
        produced = getattr(self._speaker, "produced_audio", None)
        if callable(produced) and produced() is False:
            # playout "finished" without any synthesized audio (e.g. a TTS provider error): not played
            return "failed", "no audio was synthesized"
        log.info("announcement %s played seq=%d priority=%s", event.event_id, event.sequence, event.priority)
        return "played", None
