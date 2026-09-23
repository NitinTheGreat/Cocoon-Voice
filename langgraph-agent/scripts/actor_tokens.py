"""Issue, list, show and revoke local actor tokens for this service's database (prototype auth, I02b).

    python scripts/actor_tokens.py issue --role operator --operator-id OP_DEMO_1_1 --out <new private file>
    python scripts/actor_tokens.py issue --role supervisor --principal-id sup-demo-1 --out <new private file>
    python scripts/actor_tokens.py list [--principal-id ID]
    python scripts/actor_tokens.py show TOKEN_ID
    python scripts/actor_tokens.py revoke TOKEN_ID [--reason TEXT]

The token is written once to --out and never printed. The target database is COCOON_DATA_DIR/cocoon.db from
langgraph-agent/.env or the environment; the first output line names it. See README.md, "Actor tokens".
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cocoon_agent.token_admin import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
