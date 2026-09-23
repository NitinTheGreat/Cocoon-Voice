"""Working-condition checks shared by the voice graph, the tap command route and telemetry (one implementation).

The data time of a check is the session's newest applied observation (the simulator/replay clock), else the wall
clock. Weather comes from the cache-only WeatherService, so a check never waits on the network.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from .api import schemas as s
from .store import RuleOutcome, Store, utcnow
from .weather import ConditionsPolicy, WeatherService, evaluate, rank, start_gate

RULE_ID = "demo.working_conditions.v1"
_LABELS = {"temperature_c": ("temperature", "degrees"), "relative_humidity_pct": ("humidity", "percent"),
           "precipitation_rate_mm_h": ("rain", "millimetres an hour"), "wind_speed_ms": ("wind", "metres a second"),
           "wind_gust_ms": ("gusts", "metres a second"), "visibility_m": ("visibility", "metres")}


def spoken_findings(check: s.ConditionCheck, minimum: str = "advisory") -> str:
    """"gusts 14 metres a second, rain 7 millimetres an hour" for findings at or above `minimum`."""
    parts = []
    for f in sorted(check.findings, key=lambda f: -rank(f.level)):
        if rank(f.level) >= rank(minimum) and f.value is not None:
            name, unit = _LABELS[f.variable]
            value = f"{f.value:g}"
            parts.append(f"{name} {value} {unit}")
    return ", ".join(parts)


def conditions_sentence(check: s.ConditionCheck) -> str:
    """One spoken sentence for a check (used by briefings and "what are the conditions?")."""
    if check.level == "not_applicable":
        return "That work is in an indoor zone, so no weather check applies."
    if check.level == "unknown":
        return f"weather isn't available right now ({check.reason}), so check conditions yourself."
    source = "synthetic demo weather" if check.weather and check.weather.provider == "fixture" else "model weather"
    stale = " The weather data is getting old." if check.coverage == "stale" else ""
    found = spoken_findings(check)
    if not found:
        return f"conditions are within the demo limits ({source}).{stale}"
    need = {"advisory": "Take care", "acknowledge": "you'd need to confirm before starting",
            "block": "a task start would be refused"}[check.level]
    return f"{found} ({source}); {need}.{stale}"


class Conditions:
    def __init__(self, store: Store, weather: WeatherService, policy: ConditionsPolicy,
                 clock: Callable[[], datetime] = utcnow):
        self.store, self.weather, self.policy, self.clock = store, weather, policy, clock

    def data_time(self, session: s.Session) -> datetime:
        return self.store.data_clock(session.session_id) or self.clock()

    def check(self, session: s.Session, task: s.AssignedTask | None, at: datetime | None = None) -> s.ConditionCheck:
        now = self.clock()
        at = at or self.data_time(session)
        snap, coverage, reason = self.weather.lookup(self.store.get_site(session.site_id), at, now)
        return evaluate(self.policy, snap, coverage, data_time=at, now=now,
                        task_id=task.task_id if task else None, task_type=task.task_type if task else None,
                        outdoor=task.outdoor if task else True, reason=reason)

    def gate_for(self, session: s.Session):
        """The task-start gate used inside the start transaction: (check, proceed|acknowledge|block)."""
        def gate(task: s.AssignedTask) -> tuple[s.ConditionCheck, str]:
            check = self.check(session, task)
            return check, start_gate(self.policy, check)
        return gate

    def worsening(self, session: s.Session, observed_at: datetime) -> tuple[RuleOutcome | None, s.ConditionCheck | None]:
        """Compare conditions at this observation with the check the in-progress outdoor task started under. An
        episode holds while the level is above both the start level and the policy's announcement minimum; unknown
        weather neither opens nor clears it."""
        task = self.store.in_progress_task(session.shift_id)
        if task is None or not task.outdoor or task.start_check is None:
            return None, None
        now_check = self.check(session, task, at=observed_at)
        started = task.start_check.level
        if now_check.level in ("unknown", "not_applicable"):
            held = None
        else:
            held = rank(now_check.level) > max(rank(started), 0) and \
                rank(now_check.level) >= rank(self.policy.worsening_announcement_minimum)
        found = spoken_findings(now_check, self.policy.worsening_announcement_minimum)
        at = observed_at.strftime("%H:%M UTC")
        source = "synthetic fixture weather" if now_check.weather and now_check.weather.provider == "fixture" \
            else "Open-Meteo model weather"
        outcome = RuleOutcome(
            rule_id=RULE_ID, family="working_conditions", alert_type="working_conditions",
            severity="critical" if now_check.level == "block" else "warning",
            message=f"Working conditions worsened during {task.title}.",
            reason=f"Conditions reached '{now_check.level}' after the task started under '{started}'.",
            recommended_action=("Stop in a safe place and wait for conditions to improve." if now_check.level == "block"
                                else "Slow down, keep loads low and consider pausing the task."),
            start_speech=(f"Heads up: conditions have worsened for {task.title}: {found}. "
                          + ("Stop in a safe place and wait for them to improve." if now_check.level == "block"
                             else "Take extra care, or pause the task.")),
            clear_speech=f"Conditions for {task.title} are back to what you started with.",
            policy_version=self.policy.policy_version, source_status="demo_assumption", held=held,
            explanation=(f"At {at} the {source} for this site showed {found or 'no finding'}, level "
                         f"{now_check.level} under policy {self.policy.policy_version} (demo limits); the task started "
                         f"at level {started}. These limits are demo assumptions, not published limits."),
            details={"task_id": task.task_id, "start_check_id": task.start_check.check_id,
                     "start_level": started, "level": now_check.level,
                     "check": now_check.model_dump(mode="json")},
            subject_key=task.task_id, priority="critical" if now_check.level == "block" else "high")
        return outcome, now_check
