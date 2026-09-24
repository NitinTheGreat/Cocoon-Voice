"""Site weather and the deterministic working-conditions check.

Two sources, chosen by COCOON_WEATHER_MODE (independent of the LLM mode):
- `fixture`: demo/weather_fixture_v1.json, synthetic site-local periods matched to the session's data clock, so a
  replay sees the conditions of its own simulated time. Labelled `synthetic_fixture`.
- `live`: Open-Meteo forecast API for the site's trusted coordinates (never a caller-supplied URL). Current (15-minute)
  and hourly values are cached per site; a background task refreshes them at a bounded interval. Lookups only read the
  cache, so a slow or failed request never delays a turn, a tap or a telemetry warning.
- `off`: no weather; checks report `unknown` / `unavailable`.

Live data is weather-model output for a grid cell, not a machine-mounted sensor. A live lookup for a data time far
from now (a replay of past data) is `misaligned`: present-day weather cannot describe a past replay. Units are checked
against the response's unit block and normalised (°C, %, mm, m/s, m); an unknown unit makes that value null
(`quality: partial`) instead of being guessed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .api import schemas as s
from .incident_time import offset_timezone

log = logging.getLogger("cocoon_agent.weather")

SERVICE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = SERVICE_DIR / "demo" / "weather_fixture_v1.json"
DEFAULT_POLICY = SERVICE_DIR / "policies" / "working_conditions_v1.json"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"  # fixed endpoint; only trusted coordinates are sent
_PROVIDER_VARS = ("temperature_2m", "relative_humidity_2m", "precipitation", "wind_speed_10m", "wind_gusts_10m",
                  "visibility", "weather_code")
NORMALISED_UNITS = {"temperature_c": "degC", "relative_humidity_pct": "%", "precipitation_mm": "mm",
                    "precipitation_rate_mm_h": "mm/h", "wind_speed_ms": "m/s", "wind_gust_ms": "m/s",
                    "visibility_m": "m"}
# provider unit -> factor to the normalised unit (anything else is rejected, never guessed)
_UNIT_FACTORS = {
    "temperature_2m": {"°C": ("c", 1.0), "°F": ("f", None)},
    "relative_humidity_2m": {"%": ("x", 1.0)},
    "precipitation": {"mm": ("x", 1.0), "inch": ("x", 25.4)},
    "wind_speed_10m": {"m/s": ("x", 1.0), "km/h": ("x", 1 / 3.6), "mp/h": ("x", 0.44704), "kn": ("x", 0.514444)},
    "wind_gusts_10m": {"m/s": ("x", 1.0), "km/h": ("x", 1 / 3.6), "mp/h": ("x", 0.44704), "kn": ("x", 0.514444)},
    "visibility": {"m": ("x", 1.0), "ft": ("x", 0.3048)},
    "weather_code": {"wmo code": ("x", 1.0)},
}
_RANK = {"clear": 0, "advisory": 1, "acknowledge": 2, "block": 3}


def rank(level: str) -> int:
    return _RANK.get(level, -1)


# ---------------------------------------------------------------------------------------------------- policy


class VariablePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit: str
    comparison: Literal["at_or_above", "below"]
    advisory: float | None = None
    acknowledge: float | None = None
    block: float | None = None
    basis: Literal["synthetic_demo_assumption", "site_configured", "published_guidance"]
    advice: str


class ConditionsPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_id: Literal["cocoon.working-conditions-policy.v1"] = Field(alias="schema")
    policy_version: str
    note: str
    levels: dict[str, str]
    missing_weather_level: Literal["advisory", "acknowledge"]
    worsening_announcement_minimum: Literal["advisory", "acknowledge", "block"]
    outdoor_only: bool = True
    variables: dict[s.WeatherVariable, VariablePolicy]
    task_overrides: dict[str, dict[s.WeatherVariable, dict[str, float | None]]] = Field(default_factory=dict)

    def limits(self, variable: str, task_type: str | None) -> VariablePolicy:
        base = self.variables[variable]  # type: ignore[index]
        override = (self.task_overrides.get(task_type or "") or {}).get(variable)  # type: ignore[call-overload]
        return base.model_copy(update=override) if override else base


def load_conditions_policy(path: Path | None = None) -> ConditionsPolicy:
    return ConditionsPolicy.model_validate(json.loads((path or DEFAULT_POLICY).read_text(encoding="utf-8")))


def evaluate(policy: ConditionsPolicy, weather: s.WeatherSnapshot | None, coverage: str, *, data_time: datetime,
             now: datetime, task_id: str | None = None, task_type: str | None = None, outdoor: bool | None = True,
             reason: str | None = None) -> s.WorkingConditionsCheck:
    """Deterministic: the same snapshot, policy and task always give the same check."""
    base = dict(task_id=task_id, policy_version=policy.policy_version, data_time=data_time, checked_at=now)
    if outdoor is False and policy.outdoor_only:
        return s.WorkingConditionsCheck(level="not_applicable", coverage="not_applicable", reason="indoor zone",
                                        **base)
    if weather is None or coverage in ("unavailable", "misaligned"):
        return s.WorkingConditionsCheck(level="unknown", coverage=coverage if coverage != "fresh" else "unavailable",
                                        reason=reason or "no usable weather", **base)
    findings = []
    for variable in policy.variables:
        lim = policy.limits(variable, task_type)
        value = getattr(weather, variable)
        level, threshold = "clear", None
        if value is None:
            level = "unknown"
        else:
            for name in ("block", "acknowledge", "advisory"):
                limit = getattr(lim, name)
                if limit is None:
                    continue
                hit = value >= limit if lim.comparison == "at_or_above" else value < limit
                if hit:
                    level, threshold = name, limit
                    break
        findings.append(s.ConditionFinding(variable=variable, value=value, unit=lim.unit, level=level,
                                           threshold=threshold, comparison=lim.comparison, basis=lim.basis))
    known = [f.level for f in findings if f.level != "unknown"]
    overall = max(known, key=rank) if known else "unknown"
    missing = [f.variable for f in findings if f.level == "unknown"]
    note = reason or (f"not supplied: {', '.join(missing)}" if missing else None)
    return s.WorkingConditionsCheck(level=overall, coverage=coverage, reason=note, findings=findings, weather=weather,
                                    **base)


def start_gate(policy: ConditionsPolicy, check: s.WorkingConditionsCheck | None) -> str:
    """How a task start treats a check: proceed, acknowledge or block. Unknown uses the policy's missing level."""
    if check is None or check.level in ("clear", "not_applicable"):
        return "proceed"
    level = policy.missing_weather_level if check.level == "unknown" else check.level
    return {"advisory": "proceed", "acknowledge": "acknowledge", "block": "block"}[level]


# ---------------------------------------------------------------------------------------------------- sources


@dataclass(frozen=True)
class Site:
    site_id: str
    utc_offset: str
    latitude: float | None
    longitude: float | None


def _record_id(*parts: Any) -> str:
    return "wx_" + hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:20]


class FixtureWeather:
    def __init__(self, path: Path = DEFAULT_FIXTURE):
        self.path = path
        self._stamp = None
        self._reload()

    def _reload(self) -> None:
        """Re-read the file when it changed on disk (a demo "forecast change" is a new fixture_version, so every
        record ID and value hash changes with it; unchanged content keeps its IDs)."""
        stamp = self.path.stat().st_mtime_ns
        if stamp == self._stamp:
            return
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        if doc.get("schema") != "cocoon.weather-fixture.v1":
            raise ValueError(f"{self.path.name} is not a cocoon.weather-fixture.v1 file")
        self.doc, self._stamp = doc, stamp

    def lookup(self, site: Site, at: datetime, now: datetime) -> tuple[s.WeatherSnapshot | None, str, str | None]:
        self._reload()
        if self.doc["site_id"] != site.site_id:
            return None, "unavailable", "no fixture weather for this site"
        tz = offset_timezone(site.utc_offset)
        local = at.astimezone(tz)
        for p in self.doc["periods"]:
            start = _local_at(local.date(), p["from_local"], tz)
            end = _local_at(local.date(), p["to_local"], tz)
            if start <= local < end:
                snap = s.WeatherSnapshot(
                    record_id=_record_id("fixture", self.doc["fixture_version"], site.site_id, start.isoformat()),
                    provider="fixture", kind="synthetic_fixture", site_id=site.site_id,
                    valid_from=start.astimezone(timezone.utc), valid_to=end.astimezone(timezone.utc),
                    temperature_c=p["temperature_c"], relative_humidity_pct=p["relative_humidity_pct"],
                    precipitation_rate_mm_h=p["precipitation_rate_mm_h"], wind_speed_ms=p["wind_speed_ms"],
                    wind_gust_ms=p["wind_gust_ms"], visibility_m=p["visibility_m"], weather_code=p["weather_code"],
                    retrieved_at=now, units=NORMALISED_UNITS, quality="complete",
                    provenance=f"synthetic_demo_fixture v{self.doc['fixture_version']}: {p['summary']}")
                return snap, "fresh", None
        return None, "unavailable", "outside the fixture periods"


def _local_at(day: date, hhmm: str, tz: timezone) -> datetime:
    if hhmm == "24:00":
        return datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(days=1)
    hour, minute = (int(x) for x in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)


@dataclass
class _Cached:
    retrieved_at: datetime
    records: list[s.WeatherSnapshot] = field(default_factory=list)


class OpenMeteoWeather:
    """Bounded Open-Meteo client with a per-site cache. `lookup` never performs I/O."""

    def __init__(self, *, timeout: float, fresh_seconds: int, stale_limit_seconds: int, misalign_seconds: int,
                 forecast_hours: int = 6, transport: httpx.AsyncBaseTransport | None = None):
        self.timeout = timeout
        self.fresh = timedelta(seconds=fresh_seconds)
        self.stale_limit = timedelta(seconds=stale_limit_seconds)
        self.misalign = timedelta(seconds=misalign_seconds)
        self.forecast_hours = forecast_hours
        self._transport = transport
        self._cache: dict[str, _Cached] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self.last_error: dict[str, str] = {}

    async def fetch(self, site: Site, now: datetime) -> list[s.WeatherSnapshot]:
        """One request (bounded by `timeout`); validated and normalised. Raises on failure."""
        if site.latitude is None or site.longitude is None:
            raise ValueError("site has no trusted coordinates")
        params = {"latitude": f"{site.latitude:.4f}", "longitude": f"{site.longitude:.4f}",
                  "current": ",".join(_PROVIDER_VARS), "hourly": ",".join(_PROVIDER_VARS),
                  "forecast_hours": str(self.forecast_hours), "timezone": "GMT", "wind_speed_unit": "ms",
                  "temperature_unit": "celsius", "precipitation_unit": "mm"}
        async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
            r = await client.get(OPEN_METEO_URL, params=params)
        r.raise_for_status()
        return parse_open_meteo(r.json(), site.site_id, now)

    async def refresh(self, site: Site, now: datetime) -> bool:
        try:
            records = await self.fetch(site, now)
        except Exception as exc:  # network, HTTP, schema: the old cache stays and ages into stale/unavailable
            self.last_error[site.site_id] = f"{type(exc).__name__}"
            log.warning("weather refresh failed site=%s error=%s", site.site_id, type(exc).__name__)
            return False
        self._cache[site.site_id] = _Cached(retrieved_at=now, records=records)
        self.last_error.pop(site.site_id, None)
        log.info("weather refreshed site=%s records=%d", site.site_id, len(records))
        return True

    def ensure_refresh(self, site: Site, now: datetime) -> None:
        """Schedule one background refresh when the cache is missing or stale (never awaited by callers)."""
        cached = self._cache.get(site.site_id)
        if cached is not None and now - cached.retrieved_at <= self.fresh:
            return
        task = self._inflight.get(site.site_id)
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._inflight[site.site_id] = loop.create_task(self.refresh(site, now))

    def lookup(self, site: Site, at: datetime, now: datetime) -> tuple[s.WeatherSnapshot | None, str, str | None]:
        if abs(at - now) > self.misalign:
            return None, "misaligned", "live weather cannot describe this data time (replay or far future)"
        cached = self._cache.get(site.site_id)
        if cached is None:
            self.ensure_refresh(site, now)
            return None, "unavailable", "no live weather retrieved yet"
        age = now - cached.retrieved_at
        if age > self.stale_limit:
            self.ensure_refresh(site, now)
            return None, "unavailable", "cached live weather is too old"
        coverage = "fresh" if age <= self.fresh else "stale"
        if coverage == "stale":
            self.ensure_refresh(site, now)
        for rec in cached.records:  # current first, then hourly
            if rec.valid_from <= at < rec.valid_to:
                return rec, coverage, None
        current = cached.records[0] if cached.records else None
        if current is not None and current.kind == "modelled_current" and \
                current.valid_from - timedelta(minutes=15) <= at < current.valid_from:
            return current, coverage, None
        return None, "unavailable", "data time outside the retrieved weather window"


def _norm(var: str, value: Any, unit: str | None) -> float | None:
    if value is None:
        return None
    conv = _UNIT_FACTORS[var].get(unit or "")
    if conv is None:
        return None  # unknown unit: never guessed
    kind, factor = conv
    if kind == "f":
        return round((float(value) - 32) * 5 / 9, 2)
    return round(float(value) * factor, 3)


def parse_open_meteo(doc: dict[str, Any], site_id: str, now: datetime) -> list[s.WeatherSnapshot]:
    """Validate and normalise an Open-Meteo response (requested with timezone=GMT). Current record first."""
    if "current" not in doc or "hourly" not in doc:
        raise ValueError("response lacks current/hourly blocks")
    lat, lon = doc.get("latitude"), doc.get("longitude")
    out: list[s.WeatherSnapshot] = []

    def snapshot(kind: str, at_text: str, interval_s: int, values: dict[str, Any], units: dict[str, str]):
        start = datetime.fromisoformat(at_text).replace(tzinfo=timezone.utc)
        norm = {v: _norm(v, values.get(v), units.get(v)) for v in _PROVIDER_VARS}
        partial = any(values.get(v) is not None and norm[v] is None for v in _PROVIDER_VARS) or \
            any(values.get(v) is None for v in _PROVIDER_VARS)
        precip = norm["precipitation"]
        return s.WeatherSnapshot(
            record_id=_record_id("open_meteo", site_id, kind, start.isoformat(), now.isoformat()),
            provider="open_meteo", kind=kind, site_id=site_id, latitude=lat, longitude=lon,
            valid_from=start, valid_to=start + timedelta(seconds=interval_s),
            precipitation_window_start=start - timedelta(seconds=interval_s), precipitation_window_end=start,
            temperature_c=norm["temperature_2m"], relative_humidity_pct=norm["relative_humidity_2m"],
            precipitation_mm=precip,
            precipitation_rate_mm_h=round(precip * 3600 / interval_s, 3) if precip is not None else None,
            wind_speed_ms=norm["wind_speed_10m"], wind_gust_ms=norm["wind_gusts_10m"],
            visibility_m=norm["visibility"],
            weather_code=int(norm["weather_code"]) if norm["weather_code"] is not None else None,
            retrieved_at=now, issued_at=None, units=NORMALISED_UNITS,
            quality="partial" if partial else "complete",
            provenance=("Open-Meteo forecast API (weather-model output for the grid cell; precipitation summed over "
                        "the preceding interval; no forecast issue time is provided)"))

    cur = doc["current"]
    interval = int(cur.get("interval") or 900)
    out.append(snapshot("modelled_current", cur["time"], interval, cur, doc.get("current_units", {})))
    hourly = doc["hourly"]
    units = doc.get("hourly_units", {})
    for i, at_text in enumerate(hourly.get("time", [])):
        values = {v: (hourly.get(v) or [None] * (i + 1))[i] for v in _PROVIDER_VARS}
        out.append(snapshot("modelled_forecast", at_text, 3600, values, units))
    return out


class WeatherService:
    """The configured source plus the policy; every lookup is cache-only and returns a labelled coverage."""

    def __init__(self, mode: str, *, fixture_path: Path | None = None, live: OpenMeteoWeather | None = None):
        self.mode = mode
        self.fixture = FixtureWeather(fixture_path or DEFAULT_FIXTURE) if mode == "fixture" else None
        self.live = live if mode == "live" else None

    def lookup(self, site: Site | None, at: datetime, now: datetime) -> tuple[s.WeatherSnapshot | None, str, str | None]:
        if site is None:
            return None, "unavailable", "the session has no trusted site"
        if self.mode == "off":
            return None, "unavailable", "weather is switched off (COCOON_WEATHER_MODE=off)"
        if self.fixture is not None:
            return self.fixture.lookup(site, at, now)
        assert self.live is not None
        if site.latitude is None or site.longitude is None:
            return None, "unavailable", "the site has no trusted coordinates"
        return self.live.lookup(site, at, now)

    async def refresher(self, sites: list[Site], interval_seconds: int, clock) -> None:
        """Live mode: refresh every located site once per interval (bounded; one request per site)."""
        if self.live is None:
            return
        while True:
            for site in sites:
                if site.latitude is not None:
                    await self.live.refresh(site, clock())
            await asyncio.sleep(interval_seconds)
