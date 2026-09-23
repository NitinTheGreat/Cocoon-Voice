"""Regenerate ../contracts/proposed/ (the PROPOSED target contract) from cocoon_agent/contract/.

    python scripts/export_proposed_contract.py          # write
    python scripts/export_proposed_contract.py --check  # exit 1 if a committed file has drifted

The target spec is built on top of the committed runtime export ../contracts/openapi.yaml, so run
`python scripts/export_openapi.py` first whenever the runtime schemas change.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

from cocoon_agent.contract.spec import build_all  # noqa: E402

CONTRACTS = SERVICE_DIR.parent / "contracts"
PROPOSED = CONTRACTS / "proposed"
GENERATED_DIRS = ("schemas", "internal")


def render(doc: dict) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def expected_files() -> dict[Path, str]:
    runtime = yaml.safe_load((CONTRACTS / "openapi.yaml").read_text(encoding="utf-8"))
    return {PROPOSED / rel: render(doc) for rel, doc in build_all(runtime).items()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    files = expected_files()
    if args.check:
        stale = [p for p, text in files.items() if not p.exists() or p.read_text(encoding="utf-8") != text]
        extra = [p for d in GENERATED_DIRS for p in (PROPOSED / d).glob("*.json") if p not in files]
        for p in stale + extra:
            print(f"{p.relative_to(CONTRACTS.parent)} is out of date; run python scripts/export_proposed_contract.py",
                  file=sys.stderr)
        if stale or extra:
            return 1
        print("proposed contract up to date")
        return 0
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {path.relative_to(CONTRACTS.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
