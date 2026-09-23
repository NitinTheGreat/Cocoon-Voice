"""One bounded live Open-Meteo request for the demo site's trusted coordinates (independent of Vertex/LLM mode).

    python scripts/weather_smoke.py [--timeout 5]

Reads the coordinates from the tracked demo fixture (never from a URL argument), performs exactly one request,
validates and normalises the response with the backend's own parser, evaluates the working-conditions policy for
"now" and prints the result. Exit code 0 only if a response was actually received and validated; 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cocoon_agent.demo_site import load_fixture  # noqa: E402
from cocoon_agent.weather import OpenMeteoWeather, Site, evaluate, load_conditions_policy  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=5.0)
    args = ap.parse_args()
    site_doc = load_fixture()["site"]
    loc = site_doc["location"]
    site = Site(site_doc["site_id"], site_doc["utc_offset"], loc["latitude"], loc["longitude"])
    now = datetime.now(timezone.utc)
    live = OpenMeteoWeather(timeout=args.timeout, fresh_seconds=900, stale_limit_seconds=3600, misalign_seconds=5400)
    try:
        records = await live.fetch(site, now)
    except Exception as exc:
        print(f"LIVE WEATHER NOT VERIFIED: {type(exc).__name__}: {exc}")
        return 1
    current = records[0]
    print(f"received {len(records)} records from Open-Meteo for {site.site_id} "
          f"(requested {site.latitude}, {site.longitude}; grid cell {current.latitude}, {current.longitude})")
    for rec in records:
        print(f"  {rec.kind:18} {rec.valid_from:%Y-%m-%d %H:%M}Z..{rec.valid_to:%H:%M}Z temp={rec.temperature_c}C "
              f"rh={rec.relative_humidity_pct}% rain={rec.precipitation_rate_mm_h}mm/h wind={rec.wind_speed_ms}m/s "
              f"gust={rec.wind_gust_ms}m/s vis={rec.visibility_m}m code={rec.weather_code} quality={rec.quality}")
    policy = load_conditions_policy()
    check = evaluate(policy, current, "fresh", data_time=now, now=now, task_type="earth_excavation")
    print(f"policy {policy.policy_version}: level={check.level} "
          f"findings={[(f.variable, f.value, f.level) for f in check.findings if f.level != 'clear']}")
    print("LIVE WEATHER OK: received and validated (weather-model output, not a machine sensor; no issue time given)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
