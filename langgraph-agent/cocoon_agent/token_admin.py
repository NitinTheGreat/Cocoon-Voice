"""Local administration of actor tokens: issue, list, show, revoke (used by scripts/actor_tokens.py).

The plaintext token is written exactly once to a new private file (--out) and is never printed, logged or stored.
Listing and showing print metadata only: never the token or its digest. Revocation uses the non-secret token_id.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import timedelta
from pathlib import Path
from typing import TextIO

from .api.schemas import ID_PATTERN
from .auth import (DEFAULT_TTL, ROLE_SCOPES, fmt_time, generate_token, token_digest, utcnow, validate_ttl,
                   write_token_file)
from .catalog import CatalogError, load_catalog
from .config import Settings, get_settings
from .store import Conflict, Store

_ID = re.compile(ID_PATTERN)


class AdminError(Exception):
    """A refused administrative request (bad input, unknown identity, conflict). Never contains a secret."""


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="actor_tokens.py", description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    issue = sub.add_parser("issue", help="issue a new token and write it once to --out")
    issue.add_argument("--role", choices=sorted(ROLE_SCOPES), required=True)
    issue.add_argument("--operator-id", help="catalog operator ID (required for --role operator)")
    issue.add_argument("--principal-id", help="default for operators: operator:<operator-id>; required for supervisors")
    issue.add_argument("--display-name")
    issue.add_argument("--ttl-hours", type=float, default=DEFAULT_TTL.total_seconds() / 3600,
                       help="lifetime in hours (default 12; allowed 5 minutes to 30 days)")
    issue.add_argument("--out", type=Path, required=True, help="NEW private file for the token (never overwritten)")
    lst = sub.add_parser("list", help="list token metadata (never the token or its digest)")
    lst.add_argument("--principal-id")
    show = sub.add_parser("show", help="show one token's metadata")
    show.add_argument("token_id")
    revoke = sub.add_parser("revoke", help="revoke by token_id (idempotent)")
    revoke.add_argument("token_id")
    revoke.add_argument("--reason", default=None)
    return ap


def _status(meta: dict, now) -> str:
    if meta["revoked_at"] is not None:
        return "revoked"
    return "expired" if meta["expires_at"] <= now else "active"


def _describe(meta: dict, now) -> str:
    return (f"{meta['token_id']}  principal={meta['principal_id']} kind={meta['kind']} "
            f"operator={meta['operator_id'] or '-'} scopes={','.join(meta['scopes'])} "
            f"issued={fmt_time(meta['issued_at'])} expires={fmt_time(meta['expires_at'])} "
            f"status={_status(meta, now)}"
            + (f" revoked={fmt_time(meta['revoked_at'])}" if meta["revoked_at"] else ""))


def _issue(args: argparse.Namespace, settings: Settings, store: Store, out: TextIO, now) -> None:
    try:
        ttl = validate_ttl(timedelta(hours=args.ttl_hours))
    except (ValueError, OverflowError) as exc:
        raise AdminError(str(exc)) from exc
    if args.display_name is not None and len(args.display_name) > 128:
        raise AdminError("--display-name is limited to 128 characters")
    catalog_sha = None
    if args.role == "operator":
        if not args.operator_id:
            raise AdminError("--operator-id is required for an operator token")
        try:
            catalog = load_catalog(settings.dataset_root, settings.dataset_manifest_sha256)
        except CatalogError as exc:
            raise AdminError(f"catalog unavailable ({exc.issue}); operator tokens cannot be issued") from exc
        if not catalog.has_operator(args.operator_id):
            raise AdminError("operator_id is not in the verified catalog")
        store.register_catalog(catalog)
        catalog_sha = catalog.manifest_sha256
        principal_id = args.principal_id or f"operator:{args.operator_id}"
    else:
        if args.operator_id:
            raise AdminError("a supervisor principal has no operator_id")
        if not args.principal_id:
            raise AdminError("--principal-id is required for a supervisor token")
        principal_id = args.principal_id
    if not _ID.match(principal_id):
        raise AdminError("principal_id must match [A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
    if args.out.exists():
        raise AdminError("--out already exists; refusing to overwrite it")

    token = generate_token()
    try:
        meta = store.issue_actor_token(
            kind=args.role, principal_id=principal_id, operator_id=args.operator_id if args.role == "operator" else None,
            operator_catalog_sha256=catalog_sha, display_name=args.display_name, token_sha256=token_digest(token),
            scopes=ROLE_SCOPES[args.role], issued_at=fmt_time(now), expires_at=fmt_time(now + ttl),
        )
    except Conflict as exc:
        raise AdminError(str(exc)) from exc
    try:
        write_token_file(args.out, token)
    except OSError as exc:
        store.revoke_actor_token(meta["token_id"], fmt_time(utcnow()), "token file could not be written")
        raise AdminError(f"could not write --out ({type(exc).__name__}); the new token was revoked") from exc
    finally:
        del token
    print(f"issued {_describe(meta, now)}", file=out)
    print(f"token written once to {args.out} (keep it private; revoke with: revoke {meta['token_id']})", file=out)


def main(argv: list[str] | None = None, *, settings: Settings | None = None, out: TextIO | None = None,
         err: TextIO | None = None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    args = _parser().parse_args(argv)
    settings = settings or get_settings()
    now = utcnow()
    print(f"database: {settings.db_path}", file=out)
    store = Store(settings.db_path)
    try:
        store.init_schema()
        if args.command == "issue":
            _issue(args, settings, store, out, now)
        elif args.command == "list":
            rows = store.list_actor_tokens(args.principal_id)
            for meta in rows:
                print(_describe(meta, now), file=out)
            print(f"{len(rows)} token record(s)", file=out)
        elif args.command == "show":
            meta = store.get_actor_token(args.token_id)
            if meta is None:
                raise AdminError("no such token_id")
            print(_describe(meta, now), file=out)
        elif args.command == "revoke":
            meta, changed = store.revoke_actor_token(args.token_id, fmt_time(now), args.reason)
            if meta is None:
                raise AdminError("no such token_id")
            print(("revoked " if changed else "already revoked ") + _describe(meta, now), file=out)
        return 0
    except AdminError as exc:
        print(f"refused: {exc}", file=err)
        return 1
    finally:
        store.close()
