"""Seed the synthetic demo site, shifts and assigned tasks for one service date (idempotent, never wipes).

    python scripts/seed_demo.py [--service-date YYYY-MM-DD] [--bindings-out PATH]

Uses COCOON_DATA_DIR/cocoon.db and the verified catalog (DATASET_ROOT, DATASET_MANIFEST_SHA256) from
langgraph-agent/.env or the environment. Writes/merges the trusted session-binding file (default
<data dir>/demo/session_bindings.json); start the backend with SESSION_BINDINGS_PATH pointing to it.
Re-running adds nothing new and reports reused rows; a differing existing row is refused, never overwritten.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cocoon_agent.catalog import CatalogError, load_catalog  # noqa: E402
from cocoon_agent.config import get_settings  # noqa: E402
from cocoon_agent.demo_site import FixtureError, load_fixture, seed, site_today  # noqa: E402
from cocoon_agent.store import Conflict, Store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--service-date", help="site-local date YYYY-MM-DD (default: today at the site)")
    ap.add_argument("--bindings-out", type=Path, help="trusted binding file (default: <data dir>/demo/session_bindings.json)")
    args = ap.parse_args()
    settings = get_settings()
    doc = load_fixture()
    service_date = args.service_date or site_today(doc)
    bindings_path = args.bindings_out or settings.data_dir / "demo" / "session_bindings.json"
    try:
        catalog = load_catalog(settings.dataset_root, settings.dataset_manifest_sha256)
    except CatalogError as exc:
        print(f"refused: catalog unavailable ({exc.issue})", file=sys.stderr)
        return 1
    store = Store(settings.db_path)
    try:
        store.init_schema()
        store.seed_demo()
        store.register_catalog(catalog)
        report = seed(store, catalog, doc, service_date, bindings_path.resolve())
    except (FixtureError, Conflict, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(f"database: {settings.db_path}")
    print(json.dumps(report, indent=2))
    print(f"start the backend with SESSION_BINDINGS_PATH={report['bindings_file']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
