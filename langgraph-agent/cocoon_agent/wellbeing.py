"""Purpose-specific consent and contextual wellbeing advice (D1).

Consent: two operator-owned purposes (`vitals_processing`, `risk_sharing_supervisor`), each granted or revoked
against the current notice with a stable change_id and the expected consent version. Nothing is granted implicitly:
`not_set` behaves exactly like revoked. Sharing requires processing; revoking processing also revokes sharing.

Private inputs: heart rate (bpm) and skin temperature (degC) arrive on their own route. The processing grant is read
in the SAME transaction that retains the sample, so a concurrent revocation (also a BEGIN IMMEDIATE transaction) is
either fully before or fully after it. Without the grant nothing is retained and no value is echoed. Raw samples are
kept for `retention_hours` (server receipt time) and deleted by `purge_expired` or on revocation of processing.

Advice: one deterministic, versioned policy with three independent factors, each evaluated only from its own
eligible inputs (an unavailable factor is `unknown`, never normal):
  heat_index     NWS heat index from the site's air temperature and relative humidity (C's weather cache)
  break_due      minutes since the shift start or the last recorded break end (explicit break commands only)
  vitals_strain  mean heart rate / skin temperature over a recent window, only under the processing grant
One advice episode per operator opens when any factor reaches advisory, rises once (announced once), and clears only
when every factor that contributed is observed clear again. Missing data or a revocation never reads as recovery:
revoking processing withdraws an episode that relied on vitals (end_reason consent_revoked).
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .api import schemas as s
from .store import Conflict, InvalidInput, InvalidTransition, NotFound, Store, VersionConflict, iso, parse_dt, utcnow

POLICY_DIR = Path(__file__).resolve().parent.parent / "policies"
PURPOSES = ("vitals_processing", "risk_sharing_supervisor")
REQUIRES = {"risk_sharing_supervisor": ["vitals_processing"]}
ADVICE_RULE_ID = "demo.wellbeing.contextual.v1"
_RANK = {"clear": 0, "unknown": 0, "advisory": 1, "high": 2}


# ---------------------------------------------------------------------- policy


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HeatRule(_Strict):
    rule_id: str
    advisory_c: float
    high_c: float
    clear_margin_c: float
    basis: str


class BreakRule(_Strict):
    rule_id: str
    advisory_minutes: int
    basis: str


class VitalsRule(_Strict):
    rule_id: str
    window_seconds: int
    min_samples: int
    stale_seconds: int
    advisory_mean_hr_bpm: float
    high_mean_hr_bpm: float
    high_with_heat_mean_hr_bpm: float
    skin_temp_high_c: float
    clear_mean_hr_bpm: float
    min_quality: list[str]
    basis: str


class WellbeingRules(_Strict):
    heat_index: HeatRule
    break_due: BreakRule
    vitals_strain: VitalsRule


class WellbeingPolicy(_Strict):
    schema_id: str = Field(alias="schema")
    policy_version: str
    note: str
    sources: list[dict[str, str]]
    retention_hours: int
    rules: WellbeingRules


def load_wellbeing_policy(path: Path | None = None) -> WellbeingPolicy:
    doc = json.loads((path or POLICY_DIR / "wellbeing_v1.json").read_text(encoding="utf-8"))
    if doc.get("schema") != "cocoon.wellbeing-policy.v1":
        raise ValueError("not a cocoon.wellbeing-policy.v1 file")
    return WellbeingPolicy.model_validate(doc)


def load_consent_notices(path: Path | None = None) -> dict[str, dict[str, str]]:
    doc = json.loads((path or POLICY_DIR / "consent_notices_v1.json").read_text(encoding="utf-8"))
    if doc.get("schema") != "cocoon.consent-notices.v1" or set(doc["notices"]) != set(PURPOSES):
        raise ValueError("not a cocoon.consent-notices.v1 file covering both purposes")
    return doc["notices"]


# ---------------------------------------------------------------------- NWS heat index


def heat_index_c(temperature_c: float, relative_humidity_pct: float) -> float:
    """NOAA/NWS heat index (https://www.weather.gov/tbw/heatindex), computed in degF and converted back.

    Steel's simple formula first; when its average with the temperature is 80 degF or more, the Rothfusz regression
    with the low-humidity (RH < 13 %, 80-112 degF) and high-humidity (RH > 85 %, 80-87 degF) adjustments. Inputs are
    air temperature and relative humidity only: this is not apparent temperature, WBGT, skin or core temperature."""
    if not (math.isfinite(temperature_c) and math.isfinite(relative_humidity_pct)):
        raise ValueError("heat index needs finite inputs")
    if not 0 <= relative_humidity_pct <= 100:
        raise ValueError("relative humidity must be 0-100 %")
    t = temperature_c * 9 / 5 + 32
    rh = relative_humidity_pct
    hi = 0.5 * (t + 61.0 + (t - 68.0) * 1.2 + rh * 0.094)
    if (hi + t) / 2 >= 80:
        hi = (-42.379 + 2.04901523 * t + 10.14333127 * rh - 0.22475541 * t * rh - 0.00683783 * t * t
              - 0.05481717 * rh * rh + 0.00122874 * t * t * rh + 0.00085282 * t * rh * rh
              - 0.00000199 * t * t * rh * rh)
        if rh < 13 and 80 <= t <= 112:
            hi -= ((13 - rh) / 4) * math.sqrt((17 - abs(t - 95.0)) / 17)
        elif rh > 85 and 80 <= t <= 87:
            hi += ((rh - 85) / 10) * ((87 - t) / 5)
    return round((hi - 32) * 5 / 9, 1)


def heat_band(hi_c: float) -> str:
    """NWS chart bands (degF 80 / 90 / 103 / 125)."""
    f = hi_c * 9 / 5 + 32
    if f >= 125:
        return "extreme_danger"
    if f >= 103:
        return "danger"
    if f >= 90:
        return "extreme_caution"
    if f >= 80:
        return "caution"
    return "none"


# ---------------------------------------------------------------------- consent


def consent_rows(c: sqlite3.Connection, operator_id: str) -> dict[str, sqlite3.Row]:
    return {r["purpose"]: r for r in c.execute("SELECT * FROM consent_current WHERE operator_id = ?", (operator_id,))}


def consent_status(c: sqlite3.Connection, operator_id: str, purpose: str) -> str:
    row = c.execute("SELECT status FROM consent_current WHERE operator_id = ? AND purpose = ?",
                    (operator_id, purpose)).fetchone()
    return row["status"] if row else "not_set"


def operator_sites(c: sqlite3.Connection, operator_id: str) -> list[str]:
    return [r[0] for r in c.execute("SELECT DISTINCT site_id FROM sessions WHERE operator_id = ? AND binding_status ="
                                    " 'catalog_verified' AND site_id IS NOT NULL ORDER BY site_id", (operator_id,))]


def _payload_hash(obj: dict[str, Any]) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Wellbeing:
    def __init__(self, store: Store, policy: WellbeingPolicy, notices: dict[str, dict[str, str]], weather=None,
                 clock: Callable[[], datetime] = utcnow):
        self.store, self.policy, self.notices, self.weather, self.clock = store, policy, notices, weather, clock

    # ------------------------------------------------------------------ consent

    def consent_state(self, operator_id: str) -> s.OperatorConsentState:
        with self.store._lock:
            return self._state(self.store._conn, operator_id)

    def _state(self, c: sqlite3.Connection, operator_id: str) -> s.OperatorConsentState:
        row = c.execute("SELECT version FROM consent_state WHERE operator_id = ?", (operator_id,)).fetchone()
        rows = consent_rows(c, operator_id)
        consents = []
        for purpose in PURPOSES:
            r = rows.get(purpose)
            notice = self.notices[purpose]
            consents.append(s.OperatorConsent(
                purpose=purpose, status=r["status"] if r else "not_set",
                notice_version=r["notice_version"] if r else None, current_notice_version=notice["notice_version"],
                notice_summary=notice["summary"], effective_at=parse_dt(r["effective_at"]) if r else None,
                revoked_at=parse_dt(r["revoked_at"]) if r else None,
                is_synthetic_demo_record=bool(r["is_synthetic"]) if r else False, requires=REQUIRES.get(purpose, [])))
        return s.OperatorConsentState(operator_id=operator_id, version=row["version"] if row else 0, consents=consents)

    def change_consent(self, operator_id: str, principal_id: str, req: s.OperatorConsentChange,
                       ) -> s.OperatorConsentChangeResult:
        """One grant/revoke. Identical retry → saved change, not re-applied (it can never override a later
        revocation); same change_id with another body → Conflict; stale expected_version → VersionConflict."""
        digest = _payload_hash(req.model_dump(mode="json"))
        now = self.clock()
        with self.store._tx() as c:
            saved = c.execute("SELECT * FROM consent_changes WHERE operator_id = ? AND change_id = ?",
                              (operator_id, req.change_id)).fetchone()
            if saved is not None:
                if saved["request_hash"] != digest:
                    raise Conflict("change_id was already used with a different consent change")
                return s.OperatorConsentChangeResult(
                    change_id=req.change_id, applied=False, duplicate=True,
                    cascaded=[p for p in saved["cascaded"].split(",") if p], state=self._state(c, operator_id))
            state = self._state(c, operator_id)
            if req.expected_version != state.version:
                raise VersionConflict(state.version)
            current_notice = self.notices[req.purpose]["notice_version"]
            if req.notice_version != current_notice:
                raise InvalidInput("notice_version", f"the current notice is {current_notice}")
            provenance = "synthetic_demo_actor" if req.is_synthetic_demo_record else "operator_actor_token"
            cascaded: list[str] = []
            if req.action == "grant":
                if req.purpose == "risk_sharing_supervisor" and \
                        consent_status(c, operator_id, "vitals_processing") != "granted":
                    raise InvalidTransition(consent_status(c, operator_id, "vitals_processing"),
                                            "sharing a wellbeing risk needs vitals_processing granted first")
                self._set(c, operator_id, req.purpose, "granted", req.notice_version, now, req.is_synthetic_demo_record,
                          provenance)
            else:
                self._set(c, operator_id, req.purpose, "revoked", req.notice_version, now, req.is_synthetic_demo_record,
                          provenance)
                if req.purpose == "vitals_processing":
                    if consent_status(c, operator_id, "risk_sharing_supervisor") == "granted":
                        self._set(c, operator_id, "risk_sharing_supervisor", "revoked",
                                  self.notices["risk_sharing_supervisor"]["notice_version"], now,
                                  req.is_synthetic_demo_record, provenance + ":cascade")
                        cascaded.append("risk_sharing_supervisor")
                    self._forget_vitals(c, operator_id, now)
            if req.purpose == "risk_sharing_supervisor" or cascaded:
                for site_id in operator_sites(c, operator_id):  # connected supervisors re-project (remove or add)
                    Store.feed_event(c, site_id, "risk.changed", "operator", operator_id, now)
            version = state.version + 1
            c.execute("INSERT INTO consent_state(operator_id, version) VALUES (?, ?) ON CONFLICT(operator_id)"
                      " DO UPDATE SET version = excluded.version", (operator_id, version))
            c.execute("INSERT INTO consent_changes(operator_id, change_id, request_hash, purpose, action, notice_version,"
                      " expected_version, resulting_version, cascaded, provenance, principal_id, created_at)"
                      " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (operator_id, req.change_id, digest, req.purpose, req.action, req.notice_version,
                       req.expected_version, version, ",".join(cascaded), provenance, principal_id, iso(now)))
            return s.OperatorConsentChangeResult(change_id=req.change_id, applied=True, duplicate=False,
                                                 cascaded=cascaded, state=self._state(c, operator_id))

    @staticmethod
    def _set(c: sqlite3.Connection, operator_id: str, purpose: str, status: str, notice: str, now: datetime,
             synthetic: bool, provenance: str) -> None:
        granted = status == "granted"
        c.execute("INSERT INTO consent_current(operator_id, purpose, status, notice_version, effective_at, revoked_at,"
                  " is_synthetic, provenance) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(operator_id, purpose) DO UPDATE"
                  " SET status = excluded.status, notice_version = excluded.notice_version,"
                  " effective_at = CASE WHEN excluded.status = 'granted' THEN excluded.effective_at"
                  " ELSE consent_current.effective_at END, revoked_at = excluded.revoked_at,"
                  " is_synthetic = excluded.is_synthetic, provenance = excluded.provenance",
                  (operator_id, purpose, status, notice, iso(now) if granted else None, None if granted else iso(now),
                   int(synthetic), provenance))

    @staticmethod
    def _forget_vitals(c: sqlite3.Connection, operator_id: str, now: datetime) -> None:
        """Processing revoked: delete retained raw samples and withdraw an episode that relied on vitals. Derived,
        operator-private evidence already saved with past advice stays as a private record."""
        c.execute("UPDATE wellbeing_sample_outcomes SET request_hash = 'purged' WHERE operator_id = ? AND sample_id IN"
                  " (SELECT sample_id FROM wellbeing_samples WHERE operator_id = ?)", (operator_id, operator_id))
        c.execute("DELETE FROM wellbeing_samples WHERE operator_id = ?", (operator_id,))
        for row in c.execute("SELECT advice_id, evidence_json FROM wellbeing_advice WHERE operator_id = ? AND"
                             " status = 'active'", (operator_id,)).fetchall():
            if "vitals_strain" in json.loads(row["evidence_json"]).get("contributors", []):
                c.execute("UPDATE wellbeing_advice SET status = 'withdrawn', ended_at = ?, updated_at = ?,"
                          " end_reason = 'consent_revoked' WHERE advice_id = ?", (iso(now), iso(now), row["advice_id"]))

    def purge_expired(self, now: datetime | None = None) -> int:
        now = now or self.clock()
        with self.store._tx() as c:
            c.execute("UPDATE wellbeing_sample_outcomes SET request_hash = 'purged' WHERE (operator_id, sample_id) IN"
                      " (SELECT operator_id, sample_id FROM wellbeing_samples WHERE expires_at <= ?)", (iso(now),))
            return c.execute("DELETE FROM wellbeing_samples WHERE expires_at <= ?", (iso(now),)).rowcount

    # ------------------------------------------------------------------ factors

    def _heat(self, session: s.Session, at: datetime) -> s.WellbeingFactor:
        """Threshold level only; the hysteresis band is applied in _factors against the active episode."""
        rule = self.policy.rules.heat_index
        if self.weather is None:
            return s.WellbeingFactor(factor="heat_index", level="unknown")
        snap, coverage, _ = self.weather.lookup(self.store.get_site(session.site_id), at, self.clock())
        if snap is None or coverage not in ("fresh", "stale") or snap.temperature_c is None \
                or snap.relative_humidity_pct is None:
            return s.WellbeingFactor(factor="heat_index", level="unknown")
        hi = heat_index_c(snap.temperature_c, snap.relative_humidity_pct)
        level = "high" if hi >= rule.high_c else ("advisory" if hi >= rule.advisory_c else "clear")
        return s.WellbeingFactor(factor="heat_index", level=level, heat_index_c=hi, heat_index_band=heat_band(hi),
                                 weather_provider=snap.provider)

    def _break(self, c: sqlite3.Connection, session: s.Session, at: datetime) -> s.WellbeingFactor:
        rule = self.policy.rules.break_due
        if c.execute("SELECT 1 FROM break_records WHERE operator_id = ? AND ended_at IS NULL",
                     (session.operator_id,)).fetchone():
            return s.WellbeingFactor(factor="break_due", level="clear", work_minutes=0)
        shift = c.execute("SELECT start_at, end_at FROM shifts WHERE shift_id = ?", (session.shift_id,)).fetchone() \
            if session.shift_id else None
        if shift is None or not parse_dt(shift["start_at"]) <= at <= parse_dt(shift["end_at"]):
            return s.WellbeingFactor(factor="break_due", level="unknown")
        last = c.execute("SELECT MAX(ended_at) FROM break_records WHERE operator_id = ? AND ended_at IS NOT NULL"
                         " AND ended_at <= ?", (session.operator_id, iso(at))).fetchone()[0]
        since = max(parse_dt(shift["start_at"]), parse_dt(last)) if last else parse_dt(shift["start_at"])
        minutes = int((at - since).total_seconds() // 60)
        return s.WellbeingFactor(factor="break_due", level="advisory" if minutes >= rule.advisory_minutes else "clear",
                                 work_minutes=minutes)

    def _vitals(self, c: sqlite3.Connection, operator_id: str, at: datetime, heat: s.WellbeingFactor,
                prev: str | None) -> s.WellbeingFactor:
        rule = self.policy.rules.vitals_strain
        if consent_status(c, operator_id, "vitals_processing") != "granted":
            return s.WellbeingFactor(factor="vitals_strain", level="unknown")
        marks = ",".join("?" * len(rule.min_quality))
        rows = c.execute(f"SELECT heart_rate_bpm, skin_temp_c FROM wellbeing_samples WHERE operator_id = ? AND"
                         f" observed_at > ? AND observed_at <= ? AND quality IN ({marks})",
                         (operator_id, iso(at - timedelta(seconds=rule.window_seconds)), iso(at),
                          *rule.min_quality)).fetchall()
        hrs = [r["heart_rate_bpm"] for r in rows if r["heart_rate_bpm"] is not None]
        skins = [r["skin_temp_c"] for r in rows if r["skin_temp_c"] is not None]
        if len(hrs) < rule.min_samples:
            return s.WellbeingFactor(factor="vitals_strain", level="unknown", sample_count=len(hrs),
                                     window_seconds=rule.window_seconds)
        mean = round(sum(hrs) / len(hrs), 1)
        skin = max(skins) if skins else None
        hot = heat.level in ("advisory", "high") or (skin is not None and skin >= rule.skin_temp_high_c)
        if mean >= rule.high_mean_hr_bpm or (mean >= rule.high_with_heat_mean_hr_bpm and hot):
            level = "high"
        elif mean >= rule.advisory_mean_hr_bpm:
            level = "advisory"
        elif mean < rule.clear_mean_hr_bpm or prev not in ("advisory", "high"):
            level = "clear"
        else:
            level = "advisory"  # hysteresis band
        return s.WellbeingFactor(factor="vitals_strain", level=level, mean_heart_rate_bpm=mean, max_skin_temp_c=skin,
                                 sample_count=len(hrs), window_seconds=rule.window_seconds)

    def _factors(self, c: sqlite3.Connection, session: s.Session, at: datetime,
                 heat: s.WellbeingFactor | None) -> list[s.WellbeingFactor]:
        active = self._active(c, session.operator_id)
        prev = {f["factor"]: f["level"] for f in json.loads(active["evidence_json"])["factors"]} if active else {}
        heat = heat or s.WellbeingFactor(factor="heat_index", level="unknown")
        rule = self.policy.rules.heat_index
        if heat.level == "clear" and heat.heat_index_c is not None and prev.get("heat_index") in ("advisory", "high") \
                and heat.heat_index_c >= rule.advisory_c - rule.clear_margin_c:
            heat = heat.model_copy(update={"level": "advisory"})  # hysteresis band: stays raised until clearly below
        return [heat, self._break(c, session, at), self._vitals(c, session.operator_id, at, heat,
                                                                prev.get("vitals_strain"))]

    def rule_statuses(self, factors: list[s.WellbeingFactor], at: datetime) -> list[s.WellbeingRuleStatus]:
        rules = self.policy.rules
        meta = {"heat_index": (rules.heat_index.rule_id, "published_guidance"),
                "break_due": (rules.break_due.rule_id, "synthetic_demo_assumption"),
                "vitals_strain": (rules.vitals_strain.rule_id, "synthetic_demo_assumption")}
        out = []
        for f in factors:
            rule_id, basis = meta[f.factor]
            out.append(s.WellbeingRuleStatus(rule_id=rule_id, factor=f.factor, status=f.level, basis=basis,
                                             evaluated_at=at, reason=_unknown_reason(f) if f.level == "unknown" else None))
        return out

    @staticmethod
    def _active(c: sqlite3.Connection, operator_id: str) -> sqlite3.Row | None:
        return c.execute("SELECT * FROM wellbeing_advice WHERE operator_id = ? AND rule_id = ? AND status = 'active'",
                         (operator_id, ADVICE_RULE_ID)).fetchone()

    # ------------------------------------------------------------------ episodes

    def _apply(self, c: sqlite3.Connection, session: s.Session, factors: list[s.WellbeingFactor], at: datetime,
               now: datetime, ttl: timedelta) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {"opened": [], "updated": [], "cleared": [], "announced": []}
        top = max(factors, key=lambda f: _RANK[f.level])
        raised = [f.factor for f in factors if f.level in ("advisory", "high")]
        active = self._active(c, session.operator_id)
        evidence = {"factors": [f.model_dump(mode="json") for f in factors], "evaluated_at": iso(at)}
        sharing = consent_status(c, session.operator_id, "risk_sharing_supervisor") == "granted"
        if active is None:
            if not raised:
                return out
            advice_id = "WBA-" + uuid.uuid4().hex[:12]
            evidence["contributors"] = raised
            c.execute("INSERT INTO wellbeing_advice(advice_id, operator_id, session_id, rule_id, policy_version, level,"
                      " status, evidence_json, explanation, started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'active',"
                      " ?, ?, ?, ?)",
                      (advice_id, session.operator_id, session.session_id, ADVICE_RULE_ID, self.policy.policy_version,
                       top.level, json.dumps(evidence), self.explanation(factors), iso(at), iso(at)))
            # the episode and its explanation are committed with (and before anyone can read) the announcement
            event_id = _announce(c, session.session_id, f"ann_{advice_id}_start",
                                 "high" if top.level == "high" else "normal", advice_speech(factors), now, ttl)
            c.execute("UPDATE wellbeing_advice SET announcement_event_id = ? WHERE advice_id = ?", (event_id, advice_id))
            out["opened"].append(advice_id)
            out["announced"].append(event_id)
        else:
            advice_id = active["advice_id"]
            saved = json.loads(active["evidence_json"])
            contributors = sorted(set(saved.get("contributors", [])) | set(raised))
            evidence["contributors"] = contributors
            if raised:
                if _RANK[top.level] > _RANK[active["level"]]:
                    if c.execute("SELECT 1 FROM announcements WHERE event_id = ?",
                                 (f"ann_{advice_id}_escalated",)).fetchone() is None:
                        out["announced"].append(_announce(
                            c, session.session_id, f"ann_{advice_id}_escalated", "high",
                            "Your wellbeing advice has gone up. Please take a break as soon as it's safe to stop.",
                            now, ttl))
                    out["updated"].append(advice_id)
                elif _RANK[top.level] < _RANK[active["level"]]:
                    out["updated"].append(advice_id)
                c.execute("UPDATE wellbeing_advice SET level = ?, evidence_json = ?, explanation = ?, updated_at = ?"
                          " WHERE advice_id = ?", (top.level, json.dumps(evidence), self.explanation(factors), iso(at),
                                                   advice_id))
            else:
                levels = {f.factor: f.level for f in factors}
                if all(levels.get(name) == "clear" for name in contributors):
                    c.execute("UPDATE wellbeing_advice SET status = 'cleared', ended_at = ?, updated_at = ?,"
                              " end_reason = 'observed_clear', evidence_json = ? WHERE advice_id = ?",
                              (iso(at), iso(at), json.dumps(evidence), advice_id))
                    out["cleared"].append(advice_id)
        if sharing and (out["opened"] or out["updated"] or out["cleared"]) and session.site_id:
            Store.feed_event(c, session.site_id, "risk.changed", "operator", session.operator_id, now)
        return out

    def explanation(self, factors: list[s.WellbeingFactor]) -> str:
        rules = self.policy.rules
        parts = []
        for f in factors:
            if f.factor == "heat_index" and f.heat_index_c is not None:
                source = "synthetic demo weather" if f.weather_provider == "fixture" else "modelled weather"
                parts.append(f"heat index about {f.heat_index_c:g} degrees C ({f.heat_index_band.replace('_', ' ')} band "
                             f"on the NWS chart, from {source}; an environmental screening value, not a body "
                             f"temperature)")
            elif f.factor == "break_due" and f.work_minutes is not None and f.level == "advisory":
                parts.append(f"{f.work_minutes} minutes without a recorded break (demo limit "
                             f"{rules.break_due.advisory_minutes} minutes, a synthetic demo assumption)")
            elif f.factor == "vitals_strain" and f.mean_heart_rate_bpm is not None and f.level != "clear":
                skin = f", highest skin temperature {f.max_skin_temp_c:g} degrees C" if f.max_skin_temp_c else ""
                parts.append(f"average heart rate {f.mean_heart_rate_bpm:g} bpm over {f.sample_count} samples in "
                             f"{f.window_seconds // 60} minutes{skin} (demo thresholds, synthetic demo assumptions)")
        unknown = [f.factor.replace("_", " ") for f in factors if f.level == "unknown"]
        text = "Advice because of " + "; ".join(parts) + "." if parts else "No wellbeing factor is raised."
        if unknown:
            text += f" Not evaluated: {', '.join(unknown)}."
        return text + " This is contextual advice, not a diagnosis."

    # ------------------------------------------------------------------ ingestion

    def ingest(self, session: s.Session, req: s.WellbeingSampleRequest, ttl: timedelta) -> s.WellbeingSampleResult:
        if session.binding_status != "catalog_verified":
            raise NotFound("no wellbeing record for this session")
        now = self.clock()
        self.purge_expired(now)
        # Weather is cache-only; looked up before the write transaction (hysteresis finished inside).
        heat = self._heat(session, req.observed_at)
        values = {"hr": req.heart_rate_bpm, "skin": req.skin_temp_c}
        digest = _payload_hash(req.model_dump(mode="json"))
        operator_id = session.operator_id
        with self.store._tx() as c:
            saved = c.execute("SELECT * FROM wellbeing_sample_outcomes WHERE operator_id = ? AND sample_id = ?",
                              (operator_id, req.sample_id)).fetchone()
            if saved is not None:
                if saved["request_hash"] == "purged":
                    return s.WellbeingSampleResult(sample_id=req.sample_id, status="expired", retained=False,
                                                   reason="the sample was deleted (retention or revocation)")
                if saved["request_hash"] != digest:
                    raise Conflict("sample_id was already used with a different sample")
                result = s.WellbeingSampleResult.model_validate_json(saved["result_json"])
                retained = c.execute("SELECT 1 FROM wellbeing_samples WHERE operator_id = ? AND sample_id = ?",
                                     (operator_id, req.sample_id)).fetchone() is not None
                return result.model_copy(update={"status": "duplicate", "retained": retained})
            permitted = consent_status(c, operator_id, "vitals_processing") == "granted"
            status, reason = "accepted", None
            if not permitted:
                status, reason = "rejected_consent", "vitals_processing is not granted; nothing was retained"
            else:
                newest = c.execute("SELECT MAX(observed_at) FROM wellbeing_samples WHERE operator_id = ?",
                                   (operator_id,)).fetchone()[0]
                if newest is not None and req.observed_at < parse_dt(newest):
                    status, reason = "ignored_late", "older than the newest retained sample; not retained"
                else:
                    c.execute("INSERT INTO wellbeing_samples(operator_id, sample_id, session_id, observed_at,"
                              " heart_rate_bpm, skin_temp_c, window_seconds, quality, source, received_at, expires_at)"
                              " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                              (operator_id, req.sample_id, session.session_id, iso(req.observed_at), values["hr"],
                               values["skin"], req.window_seconds, req.quality, req.source, iso(now),
                               iso(now + timedelta(hours=self.policy.retention_hours))))
            factors = self._factors(c, session, req.observed_at, heat)
            moved = self._apply(c, session, factors, req.observed_at, now, ttl)
            if moved["opened"] or moved["updated"] or moved["cleared"]:
                c.execute("UPDATE sessions SET state_version = state_version + 1 WHERE session_id = ?",
                          (session.session_id,))
            result = s.WellbeingSampleResult(
                sample_id=req.sample_id, status=status, retained=status == "accepted", reason=reason,
                advice_opened=moved["opened"], advice_updated=moved["updated"], advice_cleared=moved["cleared"],
                announcements_created=moved["announced"], rules=self.rule_statuses(factors, req.observed_at))
            if permitted:  # nothing about a refused sample is kept, not even a digest of its values
                c.execute("INSERT INTO wellbeing_sample_outcomes(operator_id, sample_id, request_hash, status,"
                          " result_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                          (operator_id, req.sample_id, digest, status, result.model_dump_json(), iso(now)))
            return result

    # ------------------------------------------------------------------ reads

    def data_time(self, session: s.Session) -> datetime:
        clocks = [self.store.data_clock(session.session_id)]
        row = self.store._one("SELECT MAX(observed_at) FROM wellbeing_samples WHERE operator_id = ?",
                              (session.operator_id,))
        clocks.append(parse_dt(row[0]) if row and row[0] else None)
        known = [t for t in clocks if t is not None]
        return max(known) if known else self.clock()

    def view(self, session: s.Session) -> s.WellbeingView:
        at = self.data_time(session)
        heat = self._heat(session, at)
        with self.store._lock:
            c = self.store._conn
            factors = self._factors(c, session, at, heat)
            rows = c.execute("SELECT * FROM wellbeing_advice WHERE operator_id = ? AND status = 'active'"
                             " ORDER BY started_at", (session.operator_id,)).fetchall()
            latest = c.execute("SELECT MAX(observed_at) FROM wellbeing_samples WHERE operator_id = ?",
                               (session.operator_id,)).fetchone()[0]
            return s.WellbeingView(
                operator_id=session.operator_id, policy_version=self.policy.policy_version,
                processing=consent_status(c, session.operator_id, "vitals_processing"),
                sharing=consent_status(c, session.operator_id, "risk_sharing_supervisor"),
                rules=self.rule_statuses(factors, at), active_advice=[_advice(r) for r in rows],
                open_break=_break_record(c.execute("SELECT * FROM break_records WHERE operator_id = ? AND ended_at IS"
                                                   " NULL", (session.operator_id,)).fetchone()),
                last_break=_break_record(c.execute("SELECT * FROM break_records WHERE operator_id = ? AND ended_at IS"
                                                   " NOT NULL ORDER BY ended_at DESC LIMIT 1",
                                                   (session.operator_id,)).fetchone()),
                latest_sample_at=parse_dt(latest), retention_hours=self.policy.retention_hours, evaluated_at=at)

    def latest_advice(self, operator_id: str) -> s.WellbeingAdvice | None:
        row = self.store._one("SELECT * FROM wellbeing_advice WHERE operator_id = ? ORDER BY (status = 'active') DESC,"
                              " started_at DESC LIMIT 1", (operator_id,))
        return _advice(row) if row else None

    def risk(self, c: sqlite3.Connection, operator_id: str, site_id: str, now: datetime) -> s.WellbeingRiskView:
        """Supervisor projection, recomputed from current consent on every read (never cached): category only."""
        sharing = consent_status(c, operator_id, "risk_sharing_supervisor")
        processing = consent_status(c, operator_id, "vitals_processing")
        if sharing != "granted" or processing != "granted":
            revoked = "revoked" in (sharing, processing)
            return s.WellbeingRiskView(operator_id=operator_id, site_id=site_id, risk_level="unavailable",
                                       unavailable_reason="consent_revoked" if revoked else "consent_not_granted",
                                       freshness="none")
        active = self._active(c, operator_id)
        received = c.execute("SELECT MAX(received_at) FROM wellbeing_samples WHERE operator_id = ?",
                             (operator_id,)).fetchone()[0]
        stale_after = timedelta(seconds=self.policy.rules.vitals_strain.stale_seconds)
        freshness = "none" if received is None else ("fresh" if now - parse_dt(received) <= stale_after else "stale")
        if active is None:
            if freshness == "none":
                return s.WellbeingRiskView(operator_id=operator_id, site_id=site_id, risk_level="unavailable",
                                           unavailable_reason="no_data", freshness="none")
            if freshness == "stale":
                return s.WellbeingRiskView(operator_id=operator_id, site_id=site_id, risk_level="unavailable",
                                           unavailable_reason="stale_data", freshness="stale",
                                           as_of=parse_dt(received))
            return s.WellbeingRiskView(operator_id=operator_id, site_id=site_id, risk_level="no_advisory",
                                       freshness="fresh", as_of=parse_dt(received))
        return s.WellbeingRiskView(operator_id=operator_id, site_id=site_id, risk_level=active["level"],
                                   freshness=freshness, as_of=parse_dt(active["updated_at"]))

    # ------------------------------------------------------------------ breaks

    @staticmethod
    def break_mutation(session: s.Session, kind: str, expected_version: int | None,
                       at: datetime) -> Callable[[sqlite3.Connection], dict[str, Any]]:
        """break.start / break.end. Only these explicit commands record a break. A break never changes a task."""

        def mutate(c: sqlite3.Connection) -> dict[str, Any]:
            if session.binding_status != "catalog_verified":
                raise NotFound("breaks are recorded for verified operator sessions only")
            open_row = c.execute("SELECT * FROM break_records WHERE operator_id = ? AND ended_at IS NULL",
                                 (session.operator_id,)).fetchone()
            if kind == "break.start":
                if open_row is not None:
                    raise InvalidTransition("on_break", "a break is already open")
                break_id = "BRK-" + uuid.uuid4().hex[:12]
                c.execute("INSERT INTO break_records(break_id, operator_id, session_id, shift_id, started_at)"
                          " VALUES (?, ?, ?, ?, ?)", (break_id, session.operator_id, session.session_id,
                                                      session.shift_id, iso(at)))
                summary = "Break started."
            else:
                if open_row is None:
                    raise InvalidTransition("working", "there is no open break to end")
                if expected_version is not None and open_row["version"] != expected_version:
                    raise VersionConflict(open_row["version"])
                break_id = open_row["break_id"]
                c.execute("UPDATE break_records SET ended_at = ?, version = version + 1 WHERE break_id = ?",
                          (iso(max(at, parse_dt(open_row["started_at"]))), break_id))
                summary = "Break ended."
            record = _break_record(c.execute("SELECT * FROM break_records WHERE break_id = ?", (break_id,)).fetchone())
            return {"record_type": "break", "record_id": break_id, "summary": summary,
                    "break_record": record.model_dump(mode="json")}

        return mutate


def _unknown_reason(f: s.WellbeingFactor) -> str:
    return {"heat_index": "no usable site temperature and humidity",
            "break_due": "no trusted shift covering this time",
            "vitals_strain": "vitals not permitted, or too few recent good-quality samples"}[f.factor]


def advice_speech(factors: list[s.WellbeingFactor]) -> str:
    """Operator-facing, without values (it may be heard in the cab)."""
    why = []
    levels = {f.factor: f.level for f in factors}
    if levels.get("heat_index") in ("advisory", "high"):
        why.append("it's very hot out")
    if levels.get("break_due") in ("advisory", "high"):
        why.append("you've been working a long time without a recorded break")
    if levels.get("vitals_strain") in ("advisory", "high"):
        why.append("your recent readings are up")
    return (f"When it's safe, stop, park the machine and take a short break: {' and '.join(why)}. "
            "This is advice, not a medical check.")


def _announce(c: sqlite3.Connection, session_id: str, event_id: str, priority: str, speech: str, now: datetime,
              ttl: timedelta) -> str:
    seq = c.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM announcements WHERE session_id = ?",
                    (session_id,)).fetchone()[0]
    c.execute("INSERT INTO announcements(event_id, session_id, sequence, type, priority, speech, alert_id, created_at,"
              " expires_at) VALUES (?, ?, ?, 'wellbeing_advice', ?, ?, NULL, ?, ?)",
              (event_id, session_id, seq, priority, speech, iso(now), iso(now + ttl)))
    return event_id


def _advice(r: sqlite3.Row) -> s.WellbeingAdvice:
    evidence = json.loads(r["evidence_json"])
    return s.WellbeingAdvice(
        advice_id=r["advice_id"], rule_id=r["rule_id"], policy_version=r["policy_version"], level=r["level"],
        status=r["status"], explanation=r["explanation"], factors=evidence["factors"],
        started_at=parse_dt(r["started_at"]), updated_at=parse_dt(r["updated_at"]), ended_at=parse_dt(r["ended_at"]),
        end_reason=r["end_reason"], announcement_event_id=r["announcement_event_id"])


def _break_record(r: sqlite3.Row | None) -> s.BreakRecord | None:
    if r is None:
        return None
    return s.BreakRecord(break_id=r["break_id"], started_at=parse_dt(r["started_at"]),
                         ended_at=parse_dt(r["ended_at"]), version=r["version"])
