"""Consumer presence and non-voice presentation receipts (D3/D4).

Presence: one row per (session, consumer). A report is applied only when its `sequence` is higher than the stored one
(reordered or stale reports are recorded as ignored). Liveness is server receipt time + ttl, so a future client clock
cannot keep a device "online"; one consumer's disconnect never touches another's row. Presence is last-known
application contact, not proof that the phone is offline or that the operator is (or is not) conscious.

Presentation: screen or vibration outcome for one announcement, keyed by presentation_id, stored apart from audio
`deliveries`. A screen `presented` report of an SOS check-in is an eligible contact offer; vibration alone is not.
Presentation never acknowledges an alert or completes a lesson.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta

from .api import schemas as s
from .store import Conflict, NotFound, Store, iso, parse_dt, utcnow


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def presence_view(r: sqlite3.Row, now: datetime) -> s.ConsumerPresence:
    return s.ConsumerPresence(consumer_id=r["consumer_id"], sequence=r["sequence"], connection=r["connection"],
                              voice_available=bool(r["voice_available"]), screen_available=bool(r["screen_available"]),
                              reported_at=parse_dt(r["reported_at"]), received_at=parse_dt(r["received_at"]),
                              expires_at=parse_dt(r["expires_at"]), live=parse_dt(r["expires_at"]) > now)


def presentation_view(r: sqlite3.Row) -> s.PresentationView:
    return s.PresentationView(presentation_id=r["presentation_id"], event_id=r["event_id"],
                              consumer_id=r["consumer_id"], channel=r["channel"], status=r["status"],
                              presented_at=parse_dt(r["presented_at"]), received_at=parse_dt(r["received_at"]))


class Channels:
    def __init__(self, store: Store, sos=None, clock: Callable[[], datetime] = utcnow):
        self.store, self.sos, self.clock = store, sos, clock

    def report_presence(self, session: s.Session, req: s.ConsumerPresenceReport) -> s.PresenceResult:
        now = self.clock()
        digest = _hash(req.model_dump(mode="json"))
        with self.store._tx() as c:
            saved = c.execute("SELECT * FROM presence_reports WHERE session_id = ? AND report_id = ?",
                              (session.session_id, req.report_id)).fetchone()
            if saved is not None:
                if saved["request_hash"] != digest:
                    raise Conflict("report_id was already used with a different report")
                current = c.execute("SELECT * FROM presence WHERE session_id = ? AND consumer_id = ?",
                                    (session.session_id, req.consumer_id)).fetchone()
                result = s.PresenceResult.model_validate_json(saved["result_json"])
                return result.model_copy(update={"duplicate": True, "applied": False,
                                                 "presence": presence_view(current, now)})
            row = c.execute("SELECT * FROM presence WHERE session_id = ? AND consumer_id = ?",
                            (session.session_id, req.consumer_id)).fetchone()
            ignored = None
            if row is not None and req.sequence <= row["sequence"]:
                ignored = "older_sequence"
            else:
                c.execute("INSERT INTO presence(session_id, consumer_id, sequence, connection, voice_available,"
                          " screen_available, reported_at, received_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                          " ON CONFLICT(session_id, consumer_id) DO UPDATE SET sequence = excluded.sequence,"
                          " connection = excluded.connection, voice_available = excluded.voice_available,"
                          " screen_available = excluded.screen_available, reported_at = excluded.reported_at,"
                          " received_at = excluded.received_at, expires_at = excluded.expires_at",
                          (session.session_id, req.consumer_id, req.sequence, req.connection, int(req.voice_available),
                           int(req.screen_available), iso(req.reported_at), iso(now),
                           iso(now + timedelta(seconds=req.ttl_seconds))))
            current = c.execute("SELECT * FROM presence WHERE session_id = ? AND consumer_id = ?",
                                (session.session_id, req.consumer_id)).fetchone()
            result = s.PresenceResult(report_id=req.report_id, applied=ignored is None, duplicate=False,
                                      ignored_reason=ignored, presence=presence_view(current, now))
            c.execute("INSERT INTO presence_reports(session_id, report_id, request_hash, result_json, created_at)"
                      " VALUES (?, ?, ?, ?, ?)", (session.session_id, req.report_id, digest, result.model_dump_json(),
                                                  iso(now)))
            return result

    def presence(self, session_id: str) -> list[s.ConsumerPresence]:
        now = self.clock()
        return [presence_view(r, now) for r in self.store._all(
            "SELECT * FROM presence WHERE session_id = ? ORDER BY consumer_id", (session_id,))]

    def report_presentation(self, session: s.Session, event_id: str,
                            req: s.PresentationReceipt) -> s.PresentationView:
        now = self.clock()
        digest = _hash(req.model_dump(mode="json"))
        with self.store._tx() as c:
            if c.execute("SELECT 1 FROM announcements WHERE session_id = ? AND event_id = ?",
                         (session.session_id, event_id)).fetchone() is None:
                raise NotFound("announcement not found in this session")
            saved = c.execute("SELECT * FROM presentations WHERE session_id = ? AND presentation_id = ?",
                              (session.session_id, req.presentation_id)).fetchone()
            if saved is not None:
                if saved["request_hash"] != digest or saved["event_id"] != event_id:
                    raise Conflict("presentation_id was already used with a different report")
                return presentation_view(saved)
            c.execute("INSERT INTO presentations(session_id, presentation_id, event_id, consumer_id, channel, status,"
                      " presented_at, received_at, request_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (session.session_id, req.presentation_id, event_id, req.consumer_id, req.channel, req.status,
                       iso(req.presented_at), iso(now), digest))
            if self.sos is not None and req.channel == "screen" and req.status == "presented":
                self.sos.offer(c, event_id, "screen", f"presentation:{req.presentation_id}", now)
            return presentation_view(c.execute("SELECT * FROM presentations WHERE session_id = ? AND"
                                               " presentation_id = ?", (session.session_id,
                                                                        req.presentation_id)).fetchone())
