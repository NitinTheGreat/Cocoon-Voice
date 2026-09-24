"""Offline command reconciliation (D4) on top of the shared command service.

`incident.submit_draft` uploads a draft captured while the device could not reach the backend:
- Identity is operator-wide: (operator_id, client_draft_id). A transport retry or an upload from a replacement
  session finds the stored draft; the same ID with other content or another binding is refused (the device keeps its
  local copy and edits the stored draft with incident.edit instead).
- The draft is attached to the ORIGINAL session named by `original_binding`, after checking it against the server's
  own session record (same operator, machine, site and shift). It is never rebound to the current session.
- Relative times ("ten minutes ago") are interpreted against `captured_at`, not the reconnect time. Upload is not
  confirmation: missing facts stay in the draft until the operator confirms it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from typing import Any

from .api import schemas as s
from .incident_time import interpret
from .store import OccurrenceTime, Store, _draft, iso, session_offset, utcnow


class DraftConflict(Exception):
    def __init__(self, draft_id: str, reason: str):
        super().__init__(f"client_draft_id was already uploaded as {draft_id} with a different {reason}; edit that "
                         "draft instead")
        self.draft_id, self.reason = draft_id, reason


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def submit_draft_mutation(original: s.Session, req: s.SessionCommand) -> Callable[[sqlite3.Connection], dict[str, Any]]:
    p = req.payload
    content = {"description": p.description, "severity": p.severity, "severity_unknown": p.severity_unknown,
               "location_text": p.location_text, "occurred_expression": p.occurred_expression,
               "occurred_at": iso(p.occurred_at) if p.occurred_at else None, "captured_at": iso(req.captured_at)}
    binding = req.original_binding.model_dump(mode="json")

    def mutate(c: sqlite3.Connection) -> dict[str, Any]:
        row = c.execute("SELECT * FROM offline_drafts WHERE operator_id = ? AND client_draft_id = ?",
                        (original.operator_id, req.client_draft_id)).fetchone()
        if row is not None:
            if row["binding_sha256"] != _sha(binding):
                raise DraftConflict(row["draft_id"], "binding")
            if row["content_sha256"] != _sha(content):
                raise DraftConflict(row["draft_id"], "content")
            draft = _draft(c.execute("SELECT * FROM incident_drafts WHERE draft_id = ?", (row["draft_id"],)).fetchone())
            return {"record_type": "incident_draft", "record_id": draft.draft_id, "duplicate_draft": True,
                    "record_session_id": row["session_id"], "draft": draft.model_dump(mode="json"),
                    "summary": f"Draft {draft.draft_number} was already uploaded; nothing new was stored."}
        reference = req.captured_at
        if p.occurred_at is not None:
            when = OccurrenceTime(p.occurred_at, "operator_clock_time", None, reference)
        else:
            got = interpret(p.occurred_expression, reference, session_offset(c, original))
            if got.status == "none":
                when = OccurrenceTime.of_report(reference)
            elif got.status == "resolved":
                when = OccurrenceTime(got.occurred_at, got.basis, got.expression, reference)
            else:
                when = OccurrenceTime(None, "unresolved", got.expression, reference)
        severity_basis = "stated_unknown" if p.severity_unknown else ("reported" if p.severity else None)
        out = Store.operator_draft(original, f"offline:{req.client_draft_id}", p.description, p.severity,
                                   severity_basis, p.location_text, when, notify_supervisor=False)(c)
        draft_id = out["record_id"]
        c.execute("UPDATE incident_drafts SET client_draft_id = ?, captured_at = ?, capture_mode = 'offline_sync'"
                  " WHERE draft_id = ?", (req.client_draft_id, iso(req.captured_at), draft_id))
        c.execute("INSERT INTO offline_drafts(operator_id, client_draft_id, content_sha256, binding_sha256,"
                  " binding_json, draft_id, session_id, command_id, captured_at, received_at) VALUES (?, ?, ?, ?, ?, ?,"
                  " ?, ?, ?, ?)", (original.operator_id, req.client_draft_id, _sha(content), _sha(binding),
                                   json.dumps(binding), draft_id, original.session_id, req.command_id,
                                   iso(req.captured_at), iso(utcnow())))
        draft = _draft(c.execute("SELECT * FROM incident_drafts WHERE draft_id = ?", (draft_id,)).fetchone())
        return {"record_type": "incident_draft", "record_id": draft_id, "duplicate_draft": False,
                "record_session_id": original.session_id, "draft": draft.model_dump(mode="json"),
                "summary": f"Stored offline report as draft {draft.draft_number} on the original shift "
                           f"(missing: {', '.join(draft.missing) or 'nothing'}); not confirmed."}

    return mutate
