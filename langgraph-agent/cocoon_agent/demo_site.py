"""Synthetic demo site: tracked fixture → idempotent seed of sites, zones, shifts, tasks and trusted bindings.

The fixture (demo/demo_site_v1.json) is server-controlled demo data, not dataset content. Seeding never wipes a
database, never overwrites an existing row that differs (it reports a conflict instead), never touches the dataset
CSVs and never changes an existing session. Trusted site/shift bindings are written to the existing
SESSION_BINDINGS_PATH mechanism (schema cocoon.session-bindings.v1); only the server reads that file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .catalog import BINDINGS_SCHEMA, Catalog
from .config import SERVICE_DIR
from .store import Store, iso, utcnow

FIXTURE_PATH = SERVICE_DIR / "demo" / "demo_site_v1.json"
FIXTURE_SCHEMA = "cocoon.demo-site.v1"
_OFFSET = re.compile(r"^([+-])(\d{2}):(\d{2})$")


class FixtureError(Exception):
    pass


@dataclass(frozen=True)
class DemoIds:
    service_date: str

    def shift_id(self, machine_id: str) -> str:
        return f"SHF-{machine_id}-{self.service_date}"

    def task_id(self, machine_id: str, order: int) -> str:
        return f"TSK-{machine_id}-{self.service_date}-{order}"

    def binding_id(self, machine_id: str) -> str:
        return f"BIND-{machine_id}-{self.service_date}"


def load_fixture(path: Path = FIXTURE_PATH) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("schema") != FIXTURE_SCHEMA:
        raise FixtureError(f"{path.name} is not a {FIXTURE_SCHEMA} fixture")
    if not _OFFSET.match(doc["site"]["utc_offset"]):
        raise FixtureError("site.utc_offset must look like +05:30")
    zones = {z["site_zone_id"] for z in doc["zones"]}
    for a in doc["assignments"]:
        for t in a["tasks"]:
            if t["zone"] not in zones:
                raise FixtureError(f"task zone {t['zone']} is not a declared zone")
    return doc


def site_tz(doc: dict[str, Any]) -> timezone:
    sign, hh, mm = _OFFSET.match(doc["site"]["utc_offset"]).groups()
    delta = timedelta(hours=int(hh), minutes=int(mm))
    return timezone(delta if sign == "+" else -delta)


def site_today(doc: dict[str, Any], now: datetime | None = None) -> str:
    return (now or utcnow()).astimezone(site_tz(doc)).date().isoformat()


def _local(doc: dict[str, Any], service_date: str, hhmm: str) -> datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    d = date.fromisoformat(service_date)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=site_tz(doc)).astimezone(timezone.utc)


def seed(store: Store, catalog: Catalog, doc: dict[str, Any], service_date: str,
         bindings_path: Path) -> dict[str, Any]:
    """Add or reuse the demo records for one service date. Returns a report; never overwrites differing rows."""
    date.fromisoformat(service_date)  # validates the format
    if doc["catalog_manifest_sha256"] != catalog.manifest_sha256:
        raise FixtureError("the fixture was written for a different catalog snapshot than the one loaded")
    for a in doc["assignments"]:
        if not catalog.has_operator(a["operator_id"]) or not catalog.has_machine(a["machine_id"]):
            raise FixtureError(f"assignment {a['operator_id']}/{a['machine_id']} is not in the verified catalog")

    ids = DemoIds(service_date)
    site = doc["site"]
    now = iso(utcnow())
    weather = json.dumps(doc["synthetic_conditions"], sort_keys=True)
    rows: dict[str, list[tuple[str, tuple]]] = {"sites": [], "site_zones": [], "shifts": [], "task_assignments": []}
    rows["sites"].append(("site_id", (site["site_id"], site["name"], site["timezone"], site["utc_offset"],
                                      doc["fixture_version"], doc["provenance"])))
    for z in doc["zones"]:
        rows["site_zones"].append(("site_zone_id", (z["site_zone_id"], site["site_id"], z["name"], z["zone_type"],
                                                    int(bool(z["outdoor"])))))
    shift_start = _local(doc, service_date, doc["shift"]["start_local"])
    shift_end = _local(doc, service_date, doc["shift"]["end_local"])
    bindings = []
    for a in doc["assignments"]:
        shift_id = ids.shift_id(a["machine_id"])
        rows["shifts"].append(("shift_id", (shift_id, site["site_id"], a["operator_id"], a["machine_id"], service_date,
                                            iso(shift_start), iso(shift_end), catalog.manifest_sha256,
                                            doc["fixture_version"])))
        bindings.append({"binding_id": ids.binding_id(a["machine_id"]), "operator_id": a["operator_id"],
                         "machine_id": a["machine_id"], "site_id": site["site_id"], "shift_id": shift_id})
        for t in a["tasks"]:
            rows["task_assignments"].append(("task_id", (
                ids.task_id(a["machine_id"], t["order"]), shift_id, a["operator_id"], a["machine_id"], t["zone"],
                t["order"], iso(_local(doc, service_date, t["start_local"])), t["task_type"], t["title"], t["details"],
                t.get("work_quantity"), t.get("work_unit"), weather, t.get("duration_minutes"),
                "demo_supplied_estimate", now, "synthetic_demo_fixture")))
    report = store.seed_demo_site(rows)
    for a in doc["assignments"]:  # fixture 1.1 extension: filled once, never overwriting a stored value
        for t in a["tasks"]:
            if t.get("ground_condition"):
                store.fill_task_ground(ids.task_id(a["machine_id"], t["order"]), t["ground_condition"])
    loc = site.get("location")
    if loc:  # trusted coordinates for weather lookups, filled once and never overwritten with other values
        report["site_location_added"] = store.set_site_location(site["site_id"], loc["latitude"], loc["longitude"],
                                                                loc["basis"])
    report["bindings_file"] = str(bindings_path)
    report["bindings_added"] = _merge_bindings(bindings_path, catalog.manifest_sha256, bindings)
    report["service_date"] = service_date
    report["shift_ids"] = [b["shift_id"] for b in bindings]
    return report


def _merge_bindings(path: Path, manifest_sha256: str, new: list[dict[str, str]]) -> int:
    doc: dict[str, Any] = {"schema": BINDINGS_SCHEMA,
                           "origin": "synthetic_demo_fixture: written by scripts/seed_demo.py from demo/demo_site_v1.json",
                           "catalog_manifest_sha256": manifest_sha256, "bindings": []}
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("catalog_manifest_sha256") != manifest_sha256:
            raise FixtureError(f"{path} was written for a different catalog snapshot; move it aside first")
        doc["bindings"] = existing.get("bindings", [])
    have = {b["binding_id"]: b for b in doc["bindings"]}
    added = 0
    for b in new:
        if b["binding_id"] in have:
            if have[b["binding_id"]] != b:
                raise FixtureError(f"binding {b['binding_id']} already exists with different content")
            continue
        doc["bindings"].append(b)
        added += 1
    if added:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return added
