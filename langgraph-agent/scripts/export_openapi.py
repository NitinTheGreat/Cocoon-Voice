"""Regenerate ../contracts/openapi.yaml from the FastAPI app (the schemas are the source of truth).

    python scripts/export_openapi.py          # write
    python scripts/export_openapi.py --check  # exit 1 if the committed file has drifted
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

from cocoon_agent.api.app import create_app  # noqa: E402
from cocoon_agent.config import Settings  # noqa: E402

CONTRACT = SERVICE_DIR.parent / "contracts" / "openapi.yaml"
HEADER = (
    "# GENERATED from langgraph-agent/cocoon_agent/api/schemas.py by scripts/export_openapi.py.\n"
    "# Do not edit by hand. See API_CONTRACT.md for the change process.\n"
)


def build_spec() -> dict:
    settings = Settings(**{"COCOON_SERVICE_TOKEN": "export-only", "COCOON_LLM_MODE": "mock"}, _env_file=None)
    return create_app(settings).openapi()


def render(spec: dict) -> str:
    return HEADER + yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=110)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    text = render(build_spec())
    if args.check:
        current = CONTRACT.read_text(encoding="utf-8") if CONTRACT.exists() else ""
        if current != text:
            print(f"{CONTRACT} is out of date; run python scripts/export_openapi.py", file=sys.stderr)
            return 1
        print("contract up to date")
        return 0
    CONTRACT.write_text(text, encoding="utf-8")
    print(f"wrote {CONTRACT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
