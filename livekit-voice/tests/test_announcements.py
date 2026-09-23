"""Announcement queueing, playback outcomes, delivery reports, cursor and backoff."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from cocoon_voice import contract as c
from cocoon_voice.announcements import AnnouncementPump
from cocoon_voice.backend_client import BackendUnavailable

from .conftest import FakeClock

NOW = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)


def event(seq: int, *, expires_in: float | None = 60, deliveries=()) -> c.Announcement:
    return c.Announcement(
        event_id=f"ev{seq}", sequence=seq, type="alert_started", priority="high", speech=f"announcement {seq}",
        created_at=NOW, expires_at=NOW + timedelta(seconds=expires_in) if expires_in is not None else None,
        deliveries=[c.DeliveryRecord(event_id=f"ev{seq}", consumer_id="old-worker", status=s, recorded_at=NOW)
                    for s in deliveries],
    )


class FakeClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.afters: list[int] = []
        self.reports: list[tuple[str, str]] = []

    async def list_events(self, session_id, after, limit=20):
        self.afters.append(after)
        item = self.pages.pop(0) if self.pages else []
        if isinstance(item, Exception):
            raise item
        events = [e for e in item if e.sequence > after]
        return c.EventsPage(session_id=session_id, events=events, next_cursor=events[-1].sequence if events else after,
                            has_more=False)

    async def report_delivery(self, session_id, event_id, consumer_id, status, detail=None):
        self.reports.append((event_id, status))


class Handle:
    def __init__(self, interrupted=False, error=None):
        self.interrupted = interrupted
        self._error = error

    async def wait_for_playout(self):
        await asyncio.sleep(0)
        if self._error:
            raise self._error


class FakeSpeaker:
    def __init__(self, handles=None):
        self.spoken: list[str] = []
        self.handles = list(handles or [])
        self.quiet_waits = 0

    async def wait_until_quiet(self, max_wait):
        self.quiet_waits += 1

    def say(self, text):
        self.spoken.append(text)
        return self.handles.pop(0) if self.handles else Handle()


def pump(client, speaker, clock=None) -> AnnouncementPump:
    clock = clock or FakeClock()
    return AnnouncementPump(client, lambda: "ses_1", speaker, "worker-1", interval=1.0, max_backoff=8.0,
                            now=lambda: NOW, sleep=clock.sleep)


async def test_plays_each_announcement_once_across_polls():
    same = [event(1)]
    client, speaker = FakeClient([same, same, same + [event(2)]]), FakeSpeaker()
    p = pump(client, speaker)
    for _ in range(3):
        await p.poll_once()
    assert speaker.spoken == ["announcement 1", "announcement 2"]
    assert client.reports == [("ev1", "played"), ("ev2", "played")]
    assert client.afters == [0, 1, 1] and p.cursor == 2
    assert speaker.quiet_waits == 2  # waits for a quiet moment instead of talking over speech


async def test_reconnect_skips_already_delivered_and_reports_expired():
    events = [event(1, deliveries=["played"]), event(2, deliveries=["interrupted"]), event(3, expires_in=-1),
              event(4, deliveries=["failed"])]
    client, speaker = FakeClient([events]), FakeSpeaker()
    await pump(client, speaker).poll_once()
    assert speaker.spoken == ["announcement 4"]  # a previously failed playback is retried
    assert client.reports == [("ev3", "expired"), ("ev4", "played")]


async def test_interruption_and_failure_are_reported_explicitly():
    client = FakeClient([[event(1), event(2)]])
    speaker = FakeSpeaker([Handle(interrupted=True), Handle(error=RuntimeError("tts down"))])
    await pump(client, speaker).poll_once()
    assert client.reports == [("ev1", "interrupted"), ("ev2", "failed")]


async def test_backend_outage_backs_off_boundedly_then_recovers():
    clock = FakeClock()
    outage = [BackendUnavailable("down")] * 6
    client, speaker = FakeClient(outage + [[event(1)]]), FakeSpeaker()
    p = pump(client, speaker, clock)
    task = asyncio.create_task(p.run())
    while not client.reports:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    backoffs = clock.sleeps[:6]
    assert backoffs[0] >= 1.0 * 2 * 0.8 and backoffs[-1] >= 8.0 * 0.8  # grows from the base interval
    assert all(d <= 8.0 * 1.2 for d in backoffs)  # capped: never a busy loop, never unbounded
    assert client.reports == [("ev1", "played")]


async def test_stop_cancels_the_polling_task():
    client, speaker = FakeClient([]), FakeSpeaker()
    p = AnnouncementPump(client, lambda: "ses_1", speaker, "w", interval=0.01, now=lambda: NOW)
    p.start()
    await asyncio.sleep(0.05)
    await p.stop()
    polls = len(client.afters)
    await asyncio.sleep(0.05)
    assert len(client.afters) == polls and polls >= 1
