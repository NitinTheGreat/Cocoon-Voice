"""Public v1 HTTP contract for the Cocoon backend.

These Pydantic models are the source of truth for `contracts/openapi.yaml`.
Change them only in backwards-compatible ways (see API_CONTRACT.md), then
regenerate the committed spec with `python scripts/export_openapi.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal, Union

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}$"

StableId = Annotated[str, Field(pattern=ID_PATTERN, examples=["lk-item_4f2c9a"])]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- errors

ErrorCode = Literal[
    "unauthorized",
    "not_found",
    "validation_error",
    "idempotency_conflict",
    "session_conflict",
    "llm_unavailable",
    "turn_failed",
    "internal_error",
    "unknown_machine",
    "unknown_operator",
    "catalog_unavailable",
    "forbidden",
    "auth_unavailable",
    "version_conflict",
    "invalid_transition",
    "decision_conflict",
    "approval_expired",
    "invalid_cursor",
    "replay_expired",
    "rate_limited",
]


class ErrorDetail(ContractModel):
    field: str
    issue: str


class ErrorBody(ContractModel):
    code: ErrorCode
    message: str
    retryable: bool
    request_id: str
    details: list[ErrorDetail] | None = None


class ErrorResponse(ContractModel):
    error: ErrorBody


# --------------------------------------------------------------------------- sessions


class SessionCreateRequest(ContractModel):
    client_session_key: str = Field(min_length=1, max_length=256)
    room_name: str = Field(min_length=1, max_length=256)
    participant_identity: str = Field(min_length=1, max_length=256)
    operator_id: str = Field(min_length=1, max_length=128)
    machine_id: str = Field(
        min_length=1, max_length=128,
        description="Exact catalog asset ID for a NEW session (unknown → 422 unknown_machine). An existing "
                    "client_session_key is resolved against its stored association first.")
    site_id: str | None = Field(
        default=None, pattern=ID_PATTERN,
        description="Optional. Accepted only together with shift_id and only if it matches a server-configured "
                    "trusted binding; otherwise 422. Never authoritative on its own.")
    shift_id: str | None = Field(default=None, pattern=ID_PATTERN,
                                 description="Optional; see site_id.")


BindingStatus = Literal["catalog_verified", "legacy_unverified"]
ContextStatus = Literal["unavailable", "trusted_binding", "legacy_unverified"]


class Session(ContractModel):
    session_id: str
    client_session_key: str
    room_name: str
    participant_identity: str
    operator_id: str
    machine_id: str
    state_version: int
    created_at: datetime
    dataset_manifest_sha256: str | None = Field(
        default=None, description="Verified catalog snapshot this session was admitted under. Null for sessions "
                                  "created before catalog binding existed (never attached retroactively).")
    binding_status: BindingStatus = Field(
        default="legacy_unverified",
        description="catalog_verified: IDs checked against dataset_manifest_sha256 at creation. legacy_unverified: "
                    "pre-upgrade session; its IDs were never checked.")
    site_id: str | None = None
    shift_id: str | None = None
    context_status: ContextStatus = Field(
        default="legacy_unverified",
        description="unavailable: no trusted site/shift is known (not a default site). trusted_binding: site/shift "
                    "matched a server-configured binding. legacy_unverified: pre-upgrade session.")
    context_source: str | None = Field(default=None, description="Binding record that established site/shift.")


# --------------------------------------------------------------------------- domain records


class Task(ContractModel):
    task_id: str
    title: str
    details: str
    priority: Literal["low", "normal", "high"]
    status: Literal["pending", "in_progress", "done"]


class TaskConditions(ContractModel):
    source: Literal["synthetic_demo_fixture"] = Field(
        description="Where the conditions come from. Only a synthetic fixture exists until live weather (Batch C).")
    summary: str | None = None
    temperature_c: float | None = None


class TaskDuration(ContractModel):
    minutes: int | None = None
    source: Literal["demo_supplied_estimate"] = Field(
        description="A supplied demo figure, not a calibrated prediction (the estimator is Batch C).")


WeatherVariable = Literal["temperature_c", "relative_humidity_pct", "precipitation_rate_mm_h", "wind_speed_ms",
                          "wind_gust_ms", "visibility_m"]


class WeatherSnapshot(ContractModel):
    """Numeric site conditions exactly as used by a check. Values are normalised to the units in `units`.

    Open-Meteo values are weather-model output for the site's grid cell (`kind` modelled_current / modelled_forecast),
    not a machine-mounted sensor. Instant variables apply at `valid_from`; precipitation is the provider's sum over
    [`precipitation_window_start`, `precipitation_window_end`], also given as a rate. `issued_at` is only set when the
    provider states a forecast issue time (Open-Meteo does not; retrieval time and generation time are not issue
    times). Fixture values are synthetic and aligned to the site-local clock of the data time."""

    record_id: str
    provider: Literal["open_meteo", "fixture"]
    kind: Literal["modelled_current", "modelled_forecast", "synthetic_fixture"]
    site_id: str
    latitude: float | None = Field(default=None, description="Provider grid-cell latitude (may differ from the site).")
    longitude: float | None = None
    valid_from: datetime
    valid_to: datetime
    precipitation_window_start: datetime | None = None
    precipitation_window_end: datetime | None = None
    temperature_c: float | None = None
    relative_humidity_pct: float | None = None
    precipitation_mm: float | None = None
    precipitation_rate_mm_h: float | None = None
    wind_speed_ms: float | None = None
    wind_gust_ms: float | None = None
    visibility_m: float | None = None
    weather_code: int | None = Field(default=None, description="WMO weather code as reported by the provider.")
    retrieved_at: datetime
    issued_at: datetime | None = None
    units: dict[str, str]
    quality: Literal["complete", "partial"]
    provenance: str


ConditionLevel = Literal["clear", "advisory", "acknowledge", "block", "unknown"]


class ConditionFinding(ContractModel):
    variable: WeatherVariable
    value: float | None = Field(description="Null = not supplied by the source (the level is then unknown).")
    unit: str
    level: ConditionLevel
    threshold: float | None = Field(default=None, description="Policy limit that set this level.")
    comparison: Literal["at_or_above", "below"]
    basis: Literal["synthetic_demo_assumption", "site_configured", "published_guidance"]


class WorkingConditionsCheck(ContractModel):
    """A deterministic working-conditions check for an outdoor task under a versioned policy.

    level: clear (proceed), advisory (proceed, told), acknowledge (the operator must confirm before starting), block
    (start refused), unknown (no usable weather: shown, never read as fine), not_applicable (indoor zone).
    coverage: fresh / stale (cached value older than the freshness limit, labelled) / unavailable / misaligned (live
    weather cannot describe a replay's data time) / not_applicable."""

    check_id: str | None = Field(default=None, description="Set when the check was saved (task start, in-task).")
    task_id: str | None = None
    level: Literal["clear", "advisory", "acknowledge", "block", "unknown", "not_applicable"]
    coverage: Literal["fresh", "stale", "unavailable", "misaligned", "not_applicable"]
    reason: str | None = None
    findings: list[ConditionFinding] = Field(default_factory=list)
    weather: WeatherSnapshot | None = None
    policy_version: str
    data_time: datetime = Field(description="The session's data clock the conditions were matched to.")
    checked_at: datetime
    acknowledged: bool = Field(default=False, description="The operator confirmed starting despite the findings.")


class TaskDurationFactor(ContractModel):
    name: str = Field(description="ground_condition, operator_skill, machine_age_years, a weather variable, ...")
    value: Any
    multiplier: float
    effect_minutes: float = Field(description="Minutes this factor added (negative = removed), applied in order.")
    basis: str = Field(description="configured_demo_assumption, fixture_weather or open_meteo_weather.")


class TaskDurationEstimate(ContractModel):
    """A saved, versioned duration estimate. The explanation lists exactly the factors the calculation applied; no
    statistical interval is claimed. `calibration_status: uncalibrated_configured_prior` means the configuration was
    not fitted on historical outcomes. Operator skill here is the dataset/configured skill, never an LMS level."""

    estimate_id: str
    task_id: str | None = None
    estimator_version: str
    config_sha256: str
    calibration_status: Literal["uncalibrated_configured_prior", "calibrated"]
    method: Literal["productivity_with_factors", "provided_estimate_adjusted", "typical_duration_fallback",
                    "not_estimable"]
    predicted_minutes: float | None
    base_minutes: float | None = None
    base_source: str | None = None
    factors: list[TaskDurationFactor] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(description="Input snapshot the estimate was computed from.")
    explanation: str
    created_at: datetime


class AssignedTask(ContractModel):
    """A task assigned to the session's operator/machine for its trusted shift. Versioned for command concurrency."""

    task_id: str
    shift_id: str
    machine_id: str
    site_zone_id: str
    zone_name: str
    scheduled_order: int
    scheduled_start_at: datetime
    scheduled_start_local: str = Field(description="HH:MM at the site (site UTC offset).")
    task_type: str
    title: str
    details: str
    work_quantity: float | None = None
    work_unit: str | None = None
    status: Literal["scheduled", "in_progress", "completed"]
    version: int
    started_at: datetime | None = None
    completed_at: datetime | None = None
    weather: TaskConditions
    duration: TaskDuration
    outdoor: bool | None = Field(default=None, description="The task's zone is outdoors (working-condition checks).")
    conditions: WorkingConditionsCheck | None = Field(
        default=None, description="Current working-conditions check for this task (not saved; for display).")
    start_check: WorkingConditionsCheck | None = Field(
        default=None, description="The saved check the task started under; later weather never rewrites it.")
    ground_condition: str | None = Field(default=None, description="Synthetic fixture ground condition, if any.")
    estimate: TaskDurationEstimate | None = Field(default=None, description="Current saved estimate for this task.")
    start_estimate: TaskDurationEstimate | None = Field(
        default=None, description="The estimate in force when the task started; never rewritten afterwards.")
    elapsed_minutes: float | None = Field(
        default=None, description="In-progress/completed tasks: wall-clock minutes from started_at to completion "
                                  "(or now). Separate from any estimate.")


class ShiftInfo(ContractModel):
    shift_id: str
    site_id: str
    site_name: str
    timezone: str
    service_date: str
    start_at: datetime
    end_at: datetime
    utc_offset: str = Field(pattern=r"^[+-]\d{2}:\d{2}$", description="Site offset used for the service date.")
    source: Literal["synthetic_demo_fixture"]

    def tz(self) -> timezone:
        sign = 1 if self.utc_offset[0] == "+" else -1
        return timezone(sign * timedelta(hours=int(self.utc_offset[1:3]), minutes=int(self.utc_offset[4:6])))

    def local_date(self, when: datetime) -> str:
        return when.astimezone(self.tz()).date().isoformat()


Severity = Literal["low", "medium", "high", "critical"]


ZoneBasis = Literal["reported", "active_task"]
SeverityBasis = Literal["reported", "stated_unknown", "rule_default"]
# time_of_report: no occurrence time was stated, so the report time is recorded AS the report time (not a known
# occurrence time). observation_time: the triggering telemetry sample. operator_relative / operator_clock_time: the
# operator's phrase (`occurred_expression`) interpreted against `occurred_reference_at`. operator_entered: an exact
# time entered on a device. unresolved: a phrase was given but could not be interpreted; `occurred_at` is null.
OccurredBasis = Literal["time_of_report", "observation_time", "operator_relative", "operator_clock_time",
                        "operator_entered", "unresolved"]


class Incident(ContractModel):
    """A confirmed report. `description` is the "what". Structured fields are additive; each `*_basis` says where a
    value came from (stated by the operator, taken from recorded context, or a rule default) so nothing looks
    invented. `severity` is never inferred from wording: null with basis `stated_unknown` means the operator said
    they do not know; null with no basis is a report saved before severity was asked for. A `rule_default` severity
    is the warning rule's configured value, not an operator assessment of the incident."""

    incident_id: str
    incident_number: int
    session_id: str
    operator_id: str
    machine_id: str
    description: str
    source_turn_id: str
    created_at: datetime
    status: Literal["confirmed"] = Field(default="confirmed", description="Unconfirmed drafts are IncidentDraft.")
    origin: Literal["operator_reported", "auto_draft"] = "operator_reported"
    severity: Severity | None = Field(default=None, description="Null = not stated (never guessed).")
    severity_basis: SeverityBasis | None = None
    site_id: str | None = None
    site_zone_id: str | None = None
    zone_basis: ZoneBasis | None = None
    location_text: str | None = None
    occurred_at: datetime | None = None
    occurred_basis: OccurredBasis | None = None
    occurred_expression: str | None = Field(default=None, description="The operator's own time phrase, verbatim.")
    occurred_reference_at: datetime | None = Field(
        default=None, description="Persisted instant the phrase was interpreted against (first receipt of the turn "
                                  "or command); retries reuse it, so the occurrence time never moves forward.")
    episode_id: str | None = Field(default=None, description="Alert episode behind a confirmed automatic draft.")
    draft_id: str | None = Field(default=None, description="The draft this incident was confirmed from.")
    confirmed_at: datetime | None = None


DraftFact = Literal["severity", "occurred_time"]


class IncidentDraft(ContractModel):
    """An unconfirmed report: an automatic draft from a safety episode, or an operator's report that still misses a
    fact (`missing`). Not an incident until confirmed; confirming allocates the real incident ID once
    (`incident_id`). A draft with missing facts cannot be confirmed until each is stated or explicitly unknown."""

    draft_id: str
    draft_number: int
    session_id: str
    operator_id: str
    machine_id: str
    origin: Literal["auto_draft", "operator_report"]
    status: Literal["draft", "confirmed", "dismissed"]
    description: str
    severity: Severity | None = None
    severity_basis: SeverityBasis | None = None
    site_id: str | None = None
    site_zone_id: str | None = None
    zone_basis: ZoneBasis | None = None
    location_text: str | None = None
    occurred_at: datetime | None = None
    occurred_basis: OccurredBasis | None = None
    occurred_expression: str | None = None
    occurred_reference_at: datetime | None = None
    episode_id: str | None = None
    version: int
    incident_id: str | None = Field(default=None, description="Set once, when the draft is confirmed.")
    created_at: datetime
    confirmed_at: datetime | None = None
    dismissed_at: datetime | None = None
    notify_supervisor: bool = Field(default=False, description="Confirming also requests supervisor review.")
    missing: list[DraftFact] = Field(default_factory=list, description="Facts still needed before confirmation.")


class ApprovalRequest(ContractModel):
    """A request for supervisor review. `pending` until a supervisor decision route exists (Batch D); creating it
    is not a notification and not an approval."""

    approval_id: str
    kind: Literal["incident_escalation", "repeated_violations", "schedule_change"]
    incident_id: str | None = None
    alert_id: str | None = Field(default=None, description="repeated_violations: the repeat episode behind it.")
    status: Literal["pending", "approved", "rejected", "expired", "cancelled"]
    created_at: datetime


class ActionRecord(ContractModel):
    """Durable outcome of one write in this turn (committed together with the write)."""

    action_id: str
    kind: str
    outcome: Literal["completed", "failed", "unknown"]
    record_type: str | None = None
    record_id: str | None = None
    summary: str
    state_version: int | None = None
    created_at: datetime


class Lesson(ContractModel):
    lesson_id: str
    title: str
    summary: str
    duration_minutes: int
    version: str | None = Field(default=None, description="Content version, when lesson text exists.")
    content_text: str | None = Field(default=None, description="Readable lesson text (demo lessons only so far).")
    content_status: Literal["demo_authored_unreviewed"] | None = Field(
        default=None, description="demo_authored_unreviewed: written for the demo, not reviewed by a trainer.")


class LessonMediaAsset(ContractModel):
    """Catalog media for a lesson. `content_ref` is a server route resolved by catalog ID inside the content root (never
    an arbitrary path or URL); fetch it with the same bearer token. Metadata or a fetch is not playback evidence and
    never counts as completing a lesson."""

    asset_id: str
    version: int
    kind: Literal["video"]
    mime_type: str
    title: str
    duration_seconds: float
    availability: Literal["available", "withdrawn"] = Field(
        description="withdrawn: the file is missing or does not match its catalog checksum; clients must not play it.")
    content_ref: str | None = None
    captions_ref: str | None = None
    checksum_sha256: str | None = None
    licence: str
    provenance: str
    review_status: str
    playback: str


class LessonStepView(ContractModel):
    step_id: str
    kind: Literal["text", "media"]
    index: int = Field(description="1-based.")
    total: int
    speak: str = Field(description="Short text for TTS.")
    media_asset_ids: list[str] = Field(default_factory=list)


class AssessmentSummary(ContractModel):
    """Shape of a lesson's assessment. The answer key is never part of a public payload."""

    kind: Literal["quiz", "scenario"]
    quiz_id: str
    version: int
    question_count: int | None = Field(default=None, description="Null for a branching scenario.")
    pass_mark: float


class LessonView(ContractModel):
    lesson_id: str
    version: str
    title: str
    summary: str
    level: Literal["beginner", "intermediate", "expert"]
    intended_duration_seconds: int
    machine_applicability: list[str]
    prerequisites: list[str]
    review_status: str = Field(description="demo_authored_unreviewed: written for the prototype, not reviewed.")
    curriculum_version: str
    steps: list[LessonStepView]
    media: list[LessonMediaAsset]
    assessment: AssessmentSummary


class QuizChoice(ContractModel):
    choice_id: str
    text: str


class QuizQuestionView(ContractModel):
    attempt_id: str
    lesson_id: str
    kind: Literal["quiz", "scenario"]
    question_id: str = Field(description="Quiz question or scenario node; answering needs this exact ID.")
    index: int
    total: int | None = None
    prompt: str
    choices: list[QuizChoice]


class AnswerFeedback(ContractModel):
    question_id: str
    choice_id: str
    correct: bool
    correct_choice_id: str = Field(description="Shown only after the answer was saved.")
    remediation: str | None = None
    feedback: str | None = None


class AttemptResult(ContractModel):
    attempt_id: str
    lesson_id: str
    lesson_version: str
    attempt_number: int
    kind: Literal["quiz", "scenario"]
    status: Literal["passed", "failed"]
    correct: int
    total: int
    score: float
    pass_mark: float
    lesson_completed: bool = Field(description="True only when this pass completed the lesson.")
    remediation: list[str] = Field(default_factory=list)


class LessonProgressView(ContractModel):
    lesson_id: str
    title: str
    level: str
    lesson_version: str | None = None
    status: Literal["not_started", "in_progress", "paused", "deferred", "awaiting_assessment", "completed"]
    current_step: int | None = None
    total_steps: int
    steps_presented: int
    attempts: int
    best_score: float | None = None
    last_attempt_status: str | None = None
    assignment_id: str | None = None
    assigned_for_episode_id: str | None = Field(default=None, description="Safety episode behind the assignment.")
    deferred_until: datetime | None = None


class LevelTransition(ContractModel):
    level: str
    previous_level: str | None = None
    criteria_version: str
    evidence: dict[str, Any]
    achieved_at: datetime


class LearnerView(ContractModel):
    """The operator's learning record. `level` is a demo educational level under `criteria_version`; it is not an
    equipment certification and is separate from `dataset_operator_skill`."""

    learner_id: str
    level: Literal["beginner", "intermediate", "expert"]
    criteria_version: str
    level_note: str
    dataset_operator_skill: str | None = None
    level_history: list[LevelTransition] = Field(default_factory=list)
    lessons: list[LessonProgressView]
    active_question: QuizQuestionView | None = None
    recommended: list[str] = Field(default_factory=list)
    curriculum_version: str


class TrainingAssignment(ContractModel):
    assignment_id: str
    lesson_id: str
    lesson_title: str
    operator_id: str
    status: Literal["assigned", "completed"] = Field(description="Reading or playback never sets completed; only a "
                                                                 "passed assessment of the lesson does (C4).")
    assigned_at: datetime
    source_episode_id: str | None = Field(default=None, description="Safety episode that triggered the assignment.")
    completed_at: datetime | None = None
    deferred_until: datetime | None = None


OperatingState = Literal["off", "idle", "working", "travel"]


class ProximityDetection(ContractModel):
    """One detected entity in a proximity scan. Distance and bearing are relative to the machine body frame
    (bearing 0 = straight ahead, clockwise in degrees). Generated demo detections are `synthetic_scenario`; they are
    not Cat Detect or phone BLE output."""

    entity_id: StableId
    entity_type: Literal["person", "vehicle", "object", "unknown"]
    distance_m: float = Field(ge=0, le=500)
    bearing_deg: float = Field(ge=0, lt=360)
    reference_frame: Literal["machine_body"] = "machine_body"
    quality: Literal["good", "degraded", "poor"]
    source: Literal["synthetic_scenario"]


class MotionSample(ContractModel):
    t: AwareDatetime = Field(description="Observation time of this speed value.")
    speed_mps: float = Field(ge=0, le=40, description="Ground speed in metres per second.")


class MotionWindow(ContractModel):
    """Higher-rate speed samples ending at (or before) the observation. Acceleration is derived between consecutive
    samples; minute averages and daily jerk scores are not accepted as motion evidence."""

    samples: list[MotionSample] = Field(min_length=2, max_length=200)
    source: Literal["synthetic_scenario"]


class TelemetryReadings(ContractModel):
    engine_on: bool
    seatbelt_fastened: bool
    idle_seconds: int = Field(ge=0, le=86_400, description="Client-reported counter; informational. Idle rules time "
                                                           "idling from observation timestamps instead.")
    operating_state: OperatingState | None = Field(
        default=None, description="Observed machine state. Omitted = unknown: idle rules neither open nor clear.")
    speed_kph: float | None = Field(default=None, ge=0, le=100, description="Observed ground speed (motion).")
    proximity: list[ProximityDetection] | None = Field(
        default=None, description="A proximity scan: omitted = no detector data (unknown); an empty list = a scan "
                                  "with no detections, which is still not proof that nobody is near.")
    motion: MotionWindow | None = None
    pitch_deg: float | None = Field(default=None, ge=-90, le=90, description="Fore-aft tilt in degrees.")
    roll_deg: float | None = Field(default=None, ge=-90, le=90, description="Side tilt in degrees.")
    grade_pct: float | None = Field(default=None, ge=-300, le=300,
                                    description="Ground grade in percent (100 % = 45 degrees), when pitch is not given.")
    fuel_meter_l: float | None = Field(default=None, ge=0, description="Cumulative fuel meter, litres.")
    load_cycles_total: int | None = Field(default=None, ge=0, description="Cumulative work/load cycle counter.")


class AlertEvidence(ContractModel):
    """What the rule saw when the episode opened. Saved once; later readings never change it."""

    event_id: str
    observed_at: datetime = Field(description="Observation (data) time of the triggering sample.")
    readings: TelemetryReadings
    idle_since: datetime | None = Field(default=None, description="Start of the observed idle streak, if any.")
    idle_seconds_observed: int | None = Field(default=None, description="observed_at - idle_since (data clock).")
    machine_category: str | None = None
    applicability: Literal["all_categories", "category_listed"] = "all_categories"


class AlertUpdate(ContractModel):
    """A later change of a published episode (e.g. warning -> danger), with its own evidence and identity."""

    update_id: str
    level: str
    previous_level: str | None = None
    observed_at: datetime
    event_id: str = Field(description="Telemetry sample that caused the change.")
    details: dict[str, Any]
    announcement_event_id: str | None = Field(default=None, description="Set when the change was announced.")


class Alert(ContractModel):
    alert_id: str
    rule_id: str
    alert_type: Literal["seatbelt_unfastened", "prolonged_idle", "idle_unbelted", "working_conditions", "proximity",
                        "sudden_start", "sudden_stop", "steep_slope", "abnormal_fuel_per_cycle", "repeated_violations"]
    severity: Literal["warning", "critical"]
    status: Literal["active", "cleared"]
    message: str
    explanation: str
    simulated: bool
    trigger_readings: TelemetryReadings
    started_at: datetime
    cleared_at: datetime | None = None
    policy_version: str | None = Field(default=None, description="Safety policy that produced this episode.")
    source_status: Literal["demo_assumption", "published_limit"] | None = Field(
        default=None, description="demo_assumption: thresholds are demo values, not published CAT limits.")
    reason: str | None = None
    recommended_action: str | None = None
    evidence: AlertEvidence | None = None
    correlated_alert_id: str | None = Field(
        default=None, description="Parent episode this one overlaps (same physical situation). A linked combined "
                                  "episode keeps its own rule identity and evidence but is deliberately not announced "
                                  "or drafted again; `announced: false` is expected, not a defect.")
    draft_incident_id: str | None = Field(default=None, description="Automatic incident draft linked to this episode.")
    training_assignment_id: str | None = Field(default=None, description="Lesson assignment linked to this episode.")
    announced: bool = Field(default=True, description="An alert_started announcement was created (not: heard).")
    details: dict[str, Any] | None = Field(
        default=None, description="Family-specific evidence saved when the episode opened (e.g. the condition check "
                                  "of a working_conditions episode). Null for belt/idle episodes.")
    subject_key: str = Field(default="", description="What the episode is about within its rule (e.g. the detected "
                                                     "entity); empty for one-per-rule episodes.")
    level: str | None = Field(default=None, description="Current level of a graded episode (e.g. warning, danger).")
    cleared_reason: str | None = Field(
        default=None, description="observed_clear, expired_without_detection (not proof the hazard left), "
                                  "instant_event (a one-off event such as a sudden stop), ...")
    updates: list[AlertUpdate] = Field(default_factory=list)


class MachineStateView(ContractModel):
    """Latest accepted observation. Two clocks: durations use observation time (`observed_at`); freshness uses the
    server's receipt time. Missing or stale data is reported as such, never as healthy."""

    status: Literal["unavailable", "fresh", "stale"]
    observed_at: datetime | None = None
    received_at: datetime | None = None
    engine_on: bool | None = None
    seatbelt_fastened: bool | None = None
    operating_state: OperatingState | None = None
    speed_kph: float | None = None
    idle_since: datetime | None = None
    idle_seconds_observed: int | None = None
    stale_after_seconds: int


class PendingQuestion(ContractModel):
    """The one question Cocoon is waiting on. For incident_severity / incident_time the report is already saved as
    `draft_id` (nothing is lost if the operator never answers)."""

    kind: Literal["incident_description", "incident_severity", "incident_time", "task_start_ack", "quiz_answer"]
    for_action: Literal["log_incident", "start_task", "answer_quiz"]
    asked_in_turn_id: str
    notify_supervisor: bool = Field(default=False, description="The report was asked to go to a supervisor too.")
    severity: Severity | None = None
    severity_unknown: bool = False
    location_text: str | None = None
    time_expression: str | None = None
    draft_id: str | None = None
    then_confirm: bool = Field(default=True, description="Answering also confirms the draft once nothing is missing "
                                                         "(false when the question came from an edit).")
    task_id: str | None = Field(default=None, description="task_start_ack: the task waiting for confirmation.")
    attempt_id: str | None = Field(default=None, description="quiz_answer: the active attempt.")
    question_id: str | None = Field(default=None, description="quiz_answer: the question/node being asked.")
    choices: list[QuizChoice] = Field(default_factory=list, description="quiz_answer: the options (no answer key).")


# --------------------------------------------------------------------------- turns


class TurnRequest(ContractModel):
    turn_id: StableId
    text: str = Field(min_length=1, max_length=2000)
    source: Literal["voice", "text"]

    @field_validator("text")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must contain non-whitespace characters")
        return value


class NextTaskAction(ContractModel):
    type: Literal["next_task"]
    task: Task | None


class IncidentLoggedAction(ContractModel):
    type: Literal["incident_logged"]
    incident: Incident
    created: bool = Field(description="False when an earlier attempt of the same turn already saved it.")


class InformationRequestedAction(ContractModel):
    type: Literal["information_requested"]
    for_action: Literal["log_incident"]
    missing_field: Literal["description", "severity", "occurred_time"]
    draft: "IncidentDraft | None" = Field(
        default=None, description="The saved draft the answer will complete (severity / time questions).")
    reason: str | None = Field(default=None, description="Why a stated time could not be used, when relevant.")


class TrainingAssignedAction(ContractModel):
    type: Literal["training_assigned"]
    assignment: TrainingAssignment
    created: bool = Field(description="False when this lesson was already assigned to the operator.")


class TrainingStatusAction(ContractModel):
    type: Literal["training_status"]
    assignments: list[TrainingAssignment]
    available_lessons: list[Lesson]


class AlertExplainedAction(ContractModel):
    """The explanation comes from the episode's saved evidence, never from the latest readings."""

    type: Literal["alert_explained"]
    alert: Alert | None
    announcement_event_id: str | None = Field(default=None, description="The announcement that raised this alert.")
    deliveries: list["DeliveryRecord"] = Field(
        default_factory=list, description="Playback reports for that announcement. Played is not acknowledgement.")
    related_alerts: list[Alert] = Field(
        default_factory=list, description="Linked episodes of the same situation (e.g. the combined idle + unbelted "
                                          "episode under a seatbelt warning), each with its own saved evidence.")


class IdleReasonRecordedAction(ContractModel):
    """The operator's stated reason for idling. Recording it does not clear any warning."""

    type: Literal["idle_reason_recorded"]
    reason_id: str
    reason_text: str
    alert_id: str | None = Field(default=None, description="Idle episode the reason is linked to, if one is active.")
    belt_warning_active: bool
    created: bool


class LearningAction(ContractModel):
    """One LMS outcome of a turn or tap (lesson step, assessment question, answer feedback, result, progress...)."""

    type: Literal["learning"]
    event: Literal["lesson_started", "lesson_resumed", "lesson_step", "lesson_paused", "lesson_deferred",
                   "assessment_ready", "assessment_started", "assessment_resumed", "answer_recorded",
                   "assessment_finished", "lesson_already_completed", "progress", "training_needs",
                   "answer_unclear", "rejected"]
    lesson_id: str | None = None
    lesson_title: str | None = None
    step: LessonStepView | None = None
    media: list[LessonMediaAsset] = Field(default_factory=list)
    question: QuizQuestionView | None = None
    feedback: AnswerFeedback | None = None
    result: AttemptResult | None = None
    level_change: dict[str, Any] | None = None
    progress: LessonProgressView | None = None
    learner: LearnerView | None = None
    deferred_until: str | None = None
    attempt_number: int | None = None
    reason: str | None = Field(default=None, description="Why nothing changed (rejected / answer_unclear).")


class LessonContentAction(ContractModel):
    type: Literal["lesson_content"]
    lesson: Lesson
    assignment: TrainingAssignment | None = None


class PendingCancelledAction(ContractModel):
    type: Literal["pending_cancelled"]
    cancelled: Literal["log_incident", "start_task", "answer_quiz"] | None
    kept_draft_number: int | None = Field(
        default=None, description="The report stays saved as this draft; cancelling a question never deletes it.")


class AssignedTasksAction(ContractModel):
    """Read of the shift's assigned tasks (next task or full list)."""

    type: Literal["assigned_tasks"]
    scope: Literal["next", "all"]
    tasks: list[AssignedTask]
    shift_bound: bool = Field(description="False when the session has no trusted shift; tasks is then empty.")


class TaskTransitionAction(ContractModel):
    type: Literal["task_started", "task_completed"]
    task: AssignedTask
    command_id: str
    created: bool = Field(description="False when this exact command was already committed (retry).")
    conditions: WorkingConditionsCheck | None = Field(default=None, description="The saved start check (task_started).")


class TaskEstimateAction(ContractModel):
    """Answer to "how long will this take?" from the saved estimate of the current or next task."""

    type: Literal["task_estimate"]
    task: AssignedTask | None
    estimate: TaskDurationEstimate | None


class ConditionsReportAction(ContractModel):
    """Answer to "what are the conditions?": the check for the next/current task (or the site when unbound)."""

    type: Literal["conditions_report"]
    check: WorkingConditionsCheck
    task_title: str | None = None


class IncidentDraftAction(ContractModel):
    type: Literal["incident_confirmed", "incident_dismissed", "incident_draft_edited"]
    draft: IncidentDraft
    incident: Incident | None = Field(default=None, description="The incident created by confirming (confirm only).")
    created: bool


class EscalationRequestedAction(ContractModel):
    type: Literal["escalation_requested"]
    approval: ApprovalRequest
    created: bool


class IncidentDraftsAction(ContractModel):
    type: Literal["incident_drafts"]
    drafts: list[IncidentDraft]


class ClarificationAction(ContractModel):
    """Nothing was changed: several workflows could match, or there is nothing to act on."""

    type: Literal["clarification_needed"]
    for_action: Literal["confirm_draft", "dismiss_draft", "edit_draft", "explain_alert", "affirm", "read_lesson"]
    reason: Literal["several_candidates", "nothing_pending", "missing_facts", "nothing_to_change"]
    options: list[str] = Field(default_factory=list)


class CapabilityUnavailableAction(ContractModel):
    type: Literal["capability_unavailable"]
    capability: str = Field(max_length=60)


class TaskRejectedAction(ContractModel):
    """A task command that changed nothing, with the reason (no shift, no eligible task, illegal transition)."""

    type: Literal["task_rejected"]
    for_action: Literal["task.start", "task.complete"]
    reason: Literal["no_shift", "no_eligible_task", "invalid_transition", "conditions_block",
                    "conditions_need_acknowledgement"]
    current_status: str | None = None
    conditions: WorkingConditionsCheck | None = Field(default=None, description="The check that stopped the start.")
    task_id: str | None = None
    task_title: str | None = None


class WellbeingAction(ContractModel):
    """Voice outcome for breaks, wellbeing advice explanations and consent status (operator-private)."""

    type: Literal["wellbeing"]
    event: Literal["break_started", "break_ended", "break_rejected", "advice_explained", "no_advice",
                   "consent_status"]
    break_record: "BreakRecord | None" = None
    advice: "WellbeingAdvice | None" = None
    consents: "list[OperatorConsent]" = Field(default_factory=list)
    created: bool | None = None
    reason: str | None = None


ActionResult = Annotated[
    Union[
        WellbeingAction,
        NextTaskAction,
        IncidentLoggedAction,
        InformationRequestedAction,
        TrainingAssignedAction,
        TrainingStatusAction,
        AlertExplainedAction,
        PendingCancelledAction,
        AssignedTasksAction,
        TaskTransitionAction,
        TaskRejectedAction,
        IncidentDraftAction,
        IncidentDraftsAction,
        IdleReasonRecordedAction,
        LessonContentAction,
        ConditionsReportAction,
        TaskEstimateAction,
        LearningAction,
        EscalationRequestedAction,
        ClarificationAction,
        CapabilityUnavailableAction,
    ],
    Field(discriminator="type"),
]


class TurnResult(ContractModel):
    session_id: str
    turn_id: str
    status: Literal["processing", "completed", "failed"]
    speech: str | None = Field(
        default=None, description="Operator-facing text for TTS. Present only when status is completed."
    )
    actions: list[ActionResult] = Field(default_factory=list)
    state_version: int | None = None
    llm_mode: Literal["live", "mock"] | None = None
    error: ErrorBody | None = None
    retry_after_ms: int | None = Field(
        default=None, description="Present when status is processing: wait this long, then GET poll_url."
    )
    poll_url: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    branch: Literal["tasks", "safety_incidents", "training", "general_assistance"] | None = Field(
        default=None, description="Workflow branch the turn was classified into.")
    action_records: list[ActionRecord] = Field(
        default_factory=list, description="Writes committed by this turn, also on a failed turn: a failure after a "
                                          "committed write never means nothing was saved.")


# --------------------------------------------------------------------------- state


class SessionState(ContractModel):
    session_id: str
    state_version: int
    llm_mode: Literal["live", "mock"]
    tasks: list[Task]
    incidents: list[Incident]
    training_assignments: list[TrainingAssignment]
    available_lessons: list[Lesson]
    active_alerts: list[Alert]
    latest_alert: Alert | None
    pending_question: PendingQuestion | None
    shift: ShiftInfo | None = Field(
        default=None, description="The session's trusted shift (synthetic demo fixture), or null when unbound.")
    assigned_tasks: list[AssignedTask] = Field(
        default_factory=list, description="Tasks of the trusted shift. `tasks` stays the legacy shared demo list.")
    incident_drafts: list[IncidentDraft] = Field(
        default_factory=list, description="Open automatic drafts. `incidents` lists confirmed reports only.")
    pending_approvals: list[ApprovalRequest] = Field(default_factory=list)
    machine_state: MachineStateView | None = Field(default=None, description="Freshness of machine observations.")
    idle_reasons: list["IdleReason"] = Field(default_factory=list)
    shift_briefing: "ShiftBriefing | None" = None
    learning: LearnerView | None = Field(
        default=None, description="Learning record of a verified operator (null for legacy/unverified sessions).")
    rule_coverage: list["RuleCoverage"] = Field(
        default_factory=list, description="Whether each safety rule could evaluate the latest observation.")
    site_conditions: WorkingConditionsCheck | None = Field(
        default=None, description="Site conditions at the session's data clock (null when the session has no site).")
    wellbeing: "WellbeingView | None" = Field(
        default=None, description="Operator-private wellbeing advice, rule status and break record (never shown to a "
                                  "supervisor; supervisors get only WellbeingRiskView).")


class RuleCoverage(ContractModel):
    """Evaluation status of one rule at the latest applied observation. `unknown` / `not_configured` are never read
    as safe: they mean the rule could not decide."""

    rule_id: str
    family: str
    status: Literal["evaluated", "unknown", "not_applicable", "not_configured"]
    reason: str | None = None
    observed_at: datetime | None = None


class IdleReason(ContractModel):
    reason_id: str
    reason_text: str
    alert_id: str | None = None
    created_at: datetime


class ShiftBriefing(ContractModel):
    """Created once per shift and published as a `shift_briefing` announcement."""

    shift_id: str
    event_id: str
    speech: str
    created_at: datetime


# --------------------------------------------------------------------------- telemetry


class TelemetryProvenance(ContractModel):
    origin: Literal["synthetic_scenario", "dataset_replay"]
    generator: str = Field(max_length=80)
    record_ref: str | None = Field(default=None, max_length=120, description="Dataset record ID for a replay.")


class TelemetryRequest(ContractModel):
    event_id: StableId
    observed_at: AwareDatetime = Field(description="Timezone-aware timestamp; normalised to UTC.")
    simulated: Literal[True] = Field(description="Must be true: v1 accepts prototype simulator data only.")
    readings: TelemetryReadings
    provenance: TelemetryProvenance | None = Field(
        default=None, description="Where a simulated sample came from. Stored, never used by the rules.")

    @field_validator("observed_at")
    @classmethod
    def _to_utc(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)


class TelemetryResult(ContractModel):
    session_id: str
    event_id: str
    duplicate: bool
    stale: bool = Field(description="True when the sample was not applied (late or conflicting).")
    ignored_reason: Literal["late", "conflicting"] | None = Field(
        default=None, description="late: older than the newest applied sample; conflicting: same observation time as "
                                  "the newest applied sample but different readings.")
    alerts_opened: list[str]
    alerts_cleared: list[str]
    alerts_updated: list[str] = Field(default_factory=list, description="Active episodes whose level changed.")
    announcements_created: list[str]
    drafts_created: list[str] = Field(default_factory=list, description="Automatic incident drafts opened.")
    active_alerts: list[Alert]
    state_version: int


# --------------------------------------------------------------------------- announcements


DeliveryStatus = Literal["played", "interrupted", "failed", "expired"]


class DeliveryReport(ContractModel):
    consumer_id: StableId
    status: DeliveryStatus
    detail: str | None = Field(default=None, max_length=500)


class DeliveryRecord(ContractModel):
    event_id: str
    consumer_id: str
    status: DeliveryStatus
    detail: str | None = None
    recorded_at: datetime


class Announcement(ContractModel):
    event_id: str
    sequence: int
    type: Literal["alert_started", "alert_cleared", "shift_briefing", "alert_escalated", "coaching_prompt",
                  "wellbeing_advice", "schedule_changed"]
    priority: Literal["low", "normal", "high", "critical"]
    speech: str
    alert_id: str | None = None
    created_at: datetime
    expires_at: datetime | None = None
    deliveries: list[DeliveryRecord] = Field(
        default_factory=list, description="Latest playback report per consumer. Played is not acknowledgement."
    )


class EventsPage(ContractModel):
    session_id: str
    events: list[Announcement]
    next_cursor: int = Field(description="Pass as ?after= on the next poll.")
    has_more: bool


# --------------------------------------------------------------------------- current principal


class SessionAssociation(ContractModel):
    """An association the caller actually holds. For an operator: one of their own catalog_verified sessions,
    created by the trusted service. Catalog membership alone is never listed as an association."""

    operator_id: str
    machine_id: str
    site_id: str | None = None
    shift_id: str | None = None
    session_id: str | None = Field(default=None, description="The owned session this association comes from.")


class MeResponse(ContractModel):
    """GET /v1/me. Never contains a bearer token, its digest or service configuration."""

    subject_id: str = Field(description="`service` for the service credential; otherwise the principal_id.")
    principal_kind: Literal["service", "operator", "supervisor"]
    operator_id: str | None = Field(default=None, description="Catalog operator ID of an operator principal.")
    display_name: str | None = None
    site_ids: list[str] = Field(description="Supervisor: sites granted through the admin CLI (D2). Always empty for "
                                            "operators and the service.")
    allowed_associations: list[SessionAssociation] = Field(
        description="Operator: own catalog_verified sessions. Service and supervisor: empty (the service is trusted "
                    "for all sessions it manages; supervisors have no session access in I02b).")
    scopes: list[str] = Field(default_factory=list, description="Actor token scopes; empty for the service.")
    token_id: str | None = Field(default=None, description="Non-secret record ID of the presented actor token.")
    token_expires_at: datetime | None = Field(default=None, description="Null for the service credential.")


# --------------------------------------------------------------------------- health


class HealthResponse(ContractModel):
    status: Literal["ok"]


class ReadyResponse(ContractModel):
    status: Literal["ready", "not_ready"]
    llm_mode: Literal["live", "mock"]
    database: bool
    checkpointer: bool
    version: str
    catalog: bool = Field(default=False, description="Verified machine/operator catalog loaded; needed to admit "
                                                     "new sessions. Existing sessions keep working without it.")
    catalog_version: str | None = Field(default=None, description="Manifest SHA-256 of the loaded catalog.")
    catalog_issue: str | None = Field(default=None, description="Sanitized reason code when catalog is false.")
    schema_version: int | None = Field(default=None, description="Applied cocoon.db migration version.")


# --------------------------------------------------------------------------- commands (taps and graph tools)

CommandKind = Literal["task.start", "task.complete", "incident.edit", "incident.confirm", "incident.dismiss",
                      "lesson.start", "lesson.next", "lesson.pause", "lesson.resume", "lesson.defer", "quiz.start",
                      "quiz.answer", "break.start", "break.end"]


class CommandPayload(ContractModel):
    task_id: StableId | None = None
    incident_id: StableId | None = Field(default=None, description="incident.* commands: the draft ID (DRF-…).")
    description: str | None = Field(default=None, min_length=1, max_length=1000)
    severity: Severity | None = None
    severity_unknown: bool | None = Field(
        default=None, description="incident.edit: the operator states the severity is unknown (not a guess).")
    location_text: str | None = Field(default=None, max_length=300)
    occurred_expression: str | None = Field(
        default=None, min_length=1, max_length=120,
        description="incident.edit: a spoken/typed time phrase (e.g. 'ten minutes ago'), interpreted server-side "
                    "against the command's first receipt time and the trusted site time zone.")
    occurred_at: AwareDatetime | None = Field(
        default=None, description="incident.edit: an exact occurrence time picked on the device.")
    lesson_id: StableId | None = Field(default=None, description="lesson.* and quiz.start commands.")
    attempt_id: StableId | None = Field(default=None, description="quiz.answer: the active attempt.")
    question_id: StableId | None = Field(default=None, description="quiz.answer: the question or scenario node shown.")
    choice_id: StableId | None = Field(default=None, description="quiz.answer: the chosen choice/option.")
    expected_step: int | None = Field(default=None, ge=1, description="lesson.next: the 1-based step on screen.")
    defer_minutes: int | None = Field(default=None, ge=5, le=480, description="lesson.defer (default 30).")
    acknowledge_conditions: bool | None = Field(
        default=None, description="task.start: the operator confirms starting despite findings that need "
                                  "acknowledgement (never overrides a block).")


class SessionCommand(ContractModel):
    """POST /v1/sessions/{session_id}/commands. A subset of the proposed cocoon.command.v1 envelope: the binding is
    taken from the authenticated session (never from the body); `expected_version` guards against stale taps."""

    schema_version: Literal["cocoon.command.v1"] = "cocoon.command.v1"
    command_id: StableId = Field(description="Unique per caller; an identical retry returns the saved result.")
    kind: CommandKind
    captured_at: AwareDatetime | None = Field(default=None, description="Device time of the tap (informational).")
    expected_version: int | None = Field(default=None, ge=1)
    payload: CommandPayload

    @model_validator(mode="after")
    def _payload_for_kind(self) -> "SessionCommand":
        if self.kind.startswith("task.") and not self.payload.task_id:
            raise ValueError("task commands need payload.task_id")
        if self.kind.startswith("incident.") and not self.payload.incident_id:
            raise ValueError("incident commands need payload.incident_id")
        if (self.kind.startswith("lesson.") or self.kind == "quiz.start") and not self.payload.lesson_id:
            raise ValueError("lesson and quiz.start commands need payload.lesson_id")
        if self.kind == "quiz.answer" and not (self.payload.attempt_id and self.payload.question_id
                                               and self.payload.choice_id):
            raise ValueError("quiz.answer needs payload.attempt_id, question_id and choice_id")
        return self


class SessionCommandResult(ContractModel):
    command_id: str
    kind: str
    status: Literal["completed"]
    duplicate: bool = Field(description="True when this identical command was already committed.")
    record_type: str | None = None
    record_id: str | None = None
    summary: str
    state_version: int
    task: AssignedTask | None = None
    draft: IncidentDraft | None = None
    incident: Incident | None = None
    approval: ApprovalRequest | None = Field(
        default=None, description="Pending supervisor-review request created with a confirmation (not a notification).")
    learning: LearningAction | None = Field(default=None, description="lesson.* / quiz.* outcome.")
    break_record: "BreakRecord | None" = Field(default=None, description="break.start / break.end outcome.")
    created_at: datetime


# --------------------------------------------------------------------------- consent (D1)

ConsentPurposeName = Literal["vitals_processing", "risk_sharing_supervisor"]


class OperatorConsent(ContractModel):
    """One purpose. `not_set` means the operator never decided: it is treated exactly like revoked (no implicit
    opt-in). Runtime shape of the proposed ConsentRecord."""

    purpose: ConsentPurposeName
    status: Literal["granted", "revoked", "not_set"]
    notice_version: str | None = Field(default=None, description="Notice the last decision was made against.")
    current_notice_version: str = Field(description="Notice a grant must name now.")
    notice_summary: str
    effective_at: datetime | None = None
    revoked_at: datetime | None = None
    is_synthetic_demo_record: bool = False
    requires: list[ConsentPurposeName] = Field(default_factory=list,
                                               description="Purposes that must be granted first.")


class OperatorConsentState(ContractModel):
    operator_id: str
    version: int = Field(description="Increments on every applied change; send it as expected_version.")
    consents: list[OperatorConsent]


class OperatorConsentChange(ContractModel):
    """POST /v1/operators/{operator_id}/consents. Only the operator's own actor token may send it."""

    change_id: StableId = Field(description="Idempotency key per operator.")
    purpose: ConsentPurposeName
    action: Literal["grant", "revoke"]
    notice_version: str = Field(min_length=1, max_length=64, description="Must equal the current notice version.")
    expected_version: int = Field(ge=0, description="OperatorConsentState.version the operator saw.")
    is_synthetic_demo_record: bool = Field(default=False, description="True for rehearsal grants by synthetic actors.")
    captured_at: AwareDatetime | None = Field(default=None, description="Device time of the decision (informational;"
                                                                        " ordering uses expected_version).")


class OperatorConsentChangeResult(ContractModel):
    change_id: str
    applied: bool = Field(description="False for an identical retry: the saved change is not re-applied.")
    duplicate: bool
    cascaded: list[ConsentPurposeName] = Field(
        default_factory=list, description="Purposes revoked together with this change (sharing needs processing).")
    state: OperatorConsentState


# --------------------------------------------------------------------------- wellbeing (D1)


class WellbeingSampleRequest(ContractModel):
    """POST /v1/sessions/{session_id}/wellbeing/samples. Private operator data: heart rate in bpm and SKIN temperature
    in degrees Celsius only (not core temperature). Values are never echoed back or written to telemetry history,
    logs, checkpoints or command payloads; without an effective vitals_processing grant nothing is retained."""

    sample_id: StableId = Field(description="Idempotency key per operator.")
    observed_at: AwareDatetime = Field(description="End of the summary window at the source; normalised to UTC.")
    heart_rate_bpm: float | None = Field(default=None, ge=20, le=250, allow_inf_nan=False)
    skin_temp_c: float | None = Field(default=None, ge=20, le=45, allow_inf_nan=False,
                                      description="Skin temperature, degrees Celsius. Not core body temperature.")
    window_seconds: int = Field(ge=1, le=3600, description="Window the values summarise.")
    quality: Literal["good", "degraded", "poor"]
    source: Literal["synthetic_wearable_fixture", "wearable_device"]
    simulated: bool = Field(description="True for synthetic samples (every sample in this prototype).")

    @field_validator("observed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _honest(self) -> "WellbeingSampleRequest":
        if self.heart_rate_bpm is None and self.skin_temp_c is None:
            raise ValueError("a sample needs heart_rate_bpm or skin_temp_c")
        if self.source == "synthetic_wearable_fixture" and not self.simulated:
            raise ValueError("a synthetic fixture sample must be marked simulated")
        return self


WellbeingLevel = Literal["clear", "advisory", "high", "unknown"]


class WellbeingRuleStatus(ContractModel):
    rule_id: str
    factor: Literal["heat_index", "break_due", "vitals_strain"]
    status: WellbeingLevel = Field(description="unknown = could not evaluate (missing, stale, not permitted); never "
                                               "read as normal.")
    reason: str | None = None
    basis: Literal["published_guidance", "synthetic_demo_assumption"]
    evaluated_at: datetime | None = None


class WellbeingSampleResult(ContractModel):
    """Never contains the submitted values."""

    sample_id: str
    status: Literal["accepted", "duplicate", "rejected_consent", "ignored_late", "expired"]
    retained: bool = Field(description="True only when the raw sample is kept (bounded retention).")
    reason: str | None = None
    advice_opened: list[str] = Field(default_factory=list)
    advice_updated: list[str] = Field(default_factory=list)
    advice_cleared: list[str] = Field(default_factory=list)
    announcements_created: list[str] = Field(default_factory=list)
    rules: list[WellbeingRuleStatus] = Field(default_factory=list)


class WellbeingFactor(ContractModel):
    factor: Literal["heat_index", "break_due", "vitals_strain"]
    level: WellbeingLevel
    heat_index_c: float | None = Field(default=None, description="NWS heat index from site air temperature and "
                                                                 "relative humidity (environmental screening input).")
    heat_index_band: Literal["none", "caution", "extreme_caution", "danger", "extreme_danger"] | None = None
    weather_provider: Literal["open_meteo", "fixture"] | None = None
    work_minutes: int | None = Field(default=None, description="Minutes since the shift start or the last recorded "
                                                               "break end.")
    mean_heart_rate_bpm: float | None = None
    max_skin_temp_c: float | None = None
    sample_count: int | None = None
    window_seconds: int | None = None


class WellbeingAdvice(ContractModel):
    """Operator-only advice episode with its saved derived evidence. Advice, not a diagnosis."""

    advice_id: str
    rule_id: str
    policy_version: str
    level: Literal["advisory", "high"]
    status: Literal["active", "cleared", "withdrawn"]
    explanation: str
    factors: list[WellbeingFactor]
    started_at: datetime
    updated_at: datetime
    ended_at: datetime | None = None
    end_reason: Literal["observed_clear", "consent_revoked"] | None = None
    announcement_event_id: str | None = None


class BreakRecord(ContractModel):
    break_id: str
    started_at: datetime
    ended_at: datetime | None = None
    version: int
    source: Literal["operator_command"] = Field(
        default="operator_command", description="Only an explicit operator command records a break; engine-off, "
                                                "waiting or telemetry silence never do.")


class WellbeingView(ContractModel):
    operator_id: str
    policy_version: str
    processing: Literal["granted", "revoked", "not_set"]
    sharing: Literal["granted", "revoked", "not_set"]
    rules: list[WellbeingRuleStatus]
    active_advice: list[WellbeingAdvice]
    open_break: BreakRecord | None = None
    last_break: BreakRecord | None = None
    latest_sample_at: datetime | None = None
    retention_hours: int
    evaluated_at: datetime
    notice: str = "Contextual advice from a demo policy; not a medical assessment or a fitness-to-work decision."


class WellbeingRiskView(ContractModel):
    """The ONLY wellbeing shape a supervisor may receive: derived category, availability and freshness. No vitals,
    evidence, explanation or speech."""

    operator_id: str
    site_id: str
    risk_level: Literal["no_advisory", "advisory", "high", "unavailable"]
    unavailable_reason: Literal["consent_not_granted", "consent_revoked", "stale_data", "no_data"] | None = None
    freshness: Literal["fresh", "stale", "none"]
    as_of: datetime | None = None


# --------------------------------------------------------------------------- supervision and approvals (D2)


class SupervisorTaskItem(ContractModel):
    task_id: str
    operator_id: str
    machine_id: str
    shift_id: str
    title: str
    zone_name: str | None = None
    status: Literal["scheduled", "in_progress", "completed"]
    scheduled_order: int
    scheduled_start_at: datetime
    version: int


class SupervisorAlertItem(ContractModel):
    """Machine-safety episode summary: no readings, evidence, explanation or speech."""

    alert_id: str
    operator_id: str
    machine_id: str
    alert_type: str
    severity: Literal["warning", "critical"]
    level: str | None = None
    status: Literal["active", "cleared"]
    started_at: datetime
    cleared_at: datetime | None = None


class SupervisorIncidentItem(ContractModel):
    """Structured incident status. The operator's free-text description is never included."""

    incident_id: str
    operator_id: str
    machine_id: str
    origin: Literal["operator_reported", "auto_draft"]
    severity: Severity | None = None
    site_zone_id: str | None = None
    occurred_at: datetime | None = None
    status: Literal["confirmed"] = "confirmed"


class ApprovalDecisionView(ContractModel):
    decision_id: str
    decision: Literal["approve", "reject"]
    decided_by: str
    decided_at: datetime
    reason: str | None = None


class ApprovalApplicationView(ContractModel):
    """Separate from the decision: an approved request may still fail to apply (never reported as applied)."""

    status: Literal["not_started", "pending", "applied", "failed_stale_inputs", "failed", "not_applicable"]
    reason: str | None = None
    applied_at: datetime | None = None


class ScheduleChangeItem(ContractModel):
    task_id: str
    title: str
    before_order: int
    after_order: int
    before_start_at: datetime
    after_start_at: datetime
    duration_minutes: float
    before_level: str
    after_level: str
    reason: str


class ForecastReference(ContractModel):
    provider: Literal["open_meteo", "fixture"]
    kind: str
    record_ids: list[str]
    content_sha256: str = Field(description="Hash of the forecast values used (retrieval time excluded).")
    retrieved_at: datetime | None = None
    issued_at: datetime | None = Field(default=None, description="Unknown: the provider does not supply an issue "
                                                                 "time.")


class ScheduleProposalView(ContractModel):
    shift_id: str
    schedule_version: int
    policy_version: str
    estimator_config_sha256: str | None = None
    forecast: ForecastReference
    before_score: list[int] = Field(description="[blocks, acknowledgements, advisories] over whole task intervals.")
    after_score: list[int]
    changes: list[ScheduleChangeItem]
    constraints_version: str


class EscalationView(ContractModel):
    incident_id: str | None = None
    alert_id: str | None = None
    rule_family: str | None = None
    episode_count: int | None = None


class ApprovalView(ContractModel):
    """Runtime shape of the proposed ApprovalRecord. Built from explicit fields only."""

    approval_id: str
    kind: Literal["incident_escalation", "repeated_violations", "schedule_change"]
    action_type: Literal["notify_supervisor", "escalate_repeat_violation", "apply_schedule_change"]
    status: Literal["pending", "approved", "rejected", "expired", "cancelled"]
    eligibility: Literal["eligible", "ineligible_no_site"]
    site_id: str | None = None
    operator_id: str
    machine_id: str | None = None
    proposer: str
    created_at: datetime
    expires_at: datetime | None = None
    version: int
    payload_sha256: str
    escalation: EscalationView | None = None
    schedule: ScheduleProposalView | None = None
    decision: ApprovalDecisionView | None = None
    application: ApprovalApplicationView


class ApprovalPageView(ContractModel):
    items: list[ApprovalView]
    next_cursor: str | None = None


class ApprovalDecisionRequest(ContractModel):
    """POST /v1/approvals/{approval_id}/decision (scoped supervisor)."""

    decision_id: StableId
    decision: Literal["approve", "reject"]
    expected_version: int = Field(ge=1)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str | None = Field(default=None, max_length=300)


class ApprovalDecisionResult(ContractModel):
    approval: ApprovalView
    decision_recorded: bool = Field(description="False for an identical retry (the saved decision is returned).")


class SupervisorNotificationItem(ContractModel):
    """An in-app record. `created` is not delivered or read; presented/acknowledged come from the supervisor client."""

    notification_id: str
    site_id: str
    kind: str
    priority: Literal["normal", "urgent"]
    policy_id: str | None = None
    source_type: str
    source_id: str
    operator_id: str | None = None
    summary: str
    status: Literal["created", "presented", "acknowledged"]
    created_at: datetime
    presented_at: datetime | None = None
    acknowledged_at: datetime | None = None


class NotificationReceipt(ContractModel):
    status: Literal["presented", "acknowledged"]


class SupervisorSiteOverview(ContractModel):
    """GET /v1/supervisor/overview. Runtime shape of the proposed SupervisorOverview: explicit safe fields only;
    wellbeing is a risk category under the operator's current sharing consent."""

    site_id: str
    generated_at: datetime
    feed_cursor: int = Field(description="Pass as ?after= to the supervisor event stream to continue from here.")
    tasks: list[SupervisorTaskItem]
    alerts: list[SupervisorAlertItem]
    incidents: list[SupervisorIncidentItem]
    approvals: list[ApprovalView]
    risks: list[WellbeingRiskView]
    notifications: list[SupervisorNotificationItem]


class ScheduleProposalRequest(ContractModel):
    note: str | None = Field(default=None, max_length=200, description="Why the planner was asked (logged only).")


class ScheduleProposalResult(ContractModel):
    status: Literal["proposed", "duplicate", "no_proposal"]
    reason: str | None = None
    approval: ApprovalView | None = None
    evaluated: list[ScheduleChangeItem] = Field(default_factory=list,
                                                description="no_proposal: the current order as evaluated.")


AlertExplainedAction.model_rebuild()
WellbeingAction.model_rebuild()
SessionCommandResult.model_rebuild()
SessionState.model_rebuild()
