"""Interactive text chat with a RUNNING backend over HTTP (Developer B's no-audio loop).

    python scripts/chat_cli.py [--room demo-room --identity op-demo-1-1 --operator OP_DEMO_1_1 --machine EXC_DEMO_001]

Commands: /state  /events  /quit
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _http import client, ensure_session  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url")
    ap.add_argument("--room", default="cli-room")
    ap.add_argument("--identity", default="op-demo-1-1")
    ap.add_argument("--operator", default="OP_DEMO_1_1", help="catalog operator ID")
    ap.add_argument("--machine", default="EXC_DEMO_001", help="catalog asset ID")
    args = ap.parse_args()
    c = client(args.base_url)
    sid = ensure_session(c, args.room, args.identity, args.operator, args.machine)
    print(f"session {sid} ({c.get('/readyz').json()['llm_mode']} mode). /state /events /quit")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text == "/quit":
            break
        if text in ("/state", "/events"):
            print(json.dumps(c.get(f"/v1/sessions/{sid}/{text[1:]}").json(), indent=2))
            continue
        r = c.post(f"/v1/sessions/{sid}/turns",
                   json={"turn_id": "cli-" + uuid.uuid4().hex[:12], "text": text, "source": "text"})
        body = r.json()
        if r.status_code != 200:
            print(f"[{r.status_code}] {body}")
            continue
        print(f"cocoon> {body['speech']}")
        print(f"        actions={[a['type'] for a in body['actions']]} state_version={body['state_version']}")


if __name__ == "__main__":
    main()
