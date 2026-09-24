"""Verified, read-only machine/operator catalog and trusted session bindings.

Verification chain: the SHA-256 of the exact manifest bytes must equal the configured
DATASET_MANIFEST_SHA256; every catalog file must match the digest the manifest lists for it.
The CSV text parsed is the same bytes that were hashed, so a changed file can never be bound
under an old digest. The result is an immutable snapshot keyed by the manifest hash.

Catalog membership proves an identifier exists. It is not authorisation, a licence, a current
assignment or evidence that an operator may use a given machine.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .api.schemas import ID_PATTERN

MANIFEST_RELATIVE_PATH = Path("data") / "generated" / "manifest.json"
SUPPORTED_MANIFEST_VERSIONS = frozenset({"1.0"})
MACHINE_COLUMNS = ("machine_id", "model", "category")
OPERATOR_COLUMNS = ("operator_id",)
BINDINGS_SCHEMA = "cocoon.session-bindings.v1"
SKILL_LEVELS = ("beginner", "intermediate", "expert")  # dataset operator_skill values (not LMS learning levels)

_ID = re.compile(ID_PATTERN)
_BASENAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,127}$")


class CatalogError(Exception):
    """Catalog or binding file cannot be trusted. `issue` is a short sanitized code for /readyz."""

    def __init__(self, issue: str, message: str):
        super().__init__(message)
        self.issue = issue
        self.message = message


@dataclass(frozen=True)
class MachineEntry:
    machine_id: str
    model: str
    category: str


@dataclass(frozen=True)
class Catalog:
    manifest_sha256: str
    manifest_schema_version: str
    dataset_origin: str
    generator_seed: int | None
    machines: Mapping[str, MachineEntry]
    operators: frozenset[str]
    file_sha256: Mapping[str, str]
    # Optional estimator inputs, only when the verified files carry them (never defaulted or guessed).
    operator_skill: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    machine_age_years: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))

    def has_machine(self, machine_id: str) -> bool:
        return machine_id in self.machines

    def has_operator(self, operator_id: str) -> bool:
        return operator_id in self.operators


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: Path, issue: str, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise CatalogError(issue, f"cannot read {label}") from exc


def _parse_csv(data: bytes, name: str, required: tuple[str, ...], id_column: str) -> list[dict[str, str]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CatalogError("file_malformed", f"{name} is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    header = reader.fieldnames or []
    missing = [c for c in required if c not in header]
    if missing:
        raise CatalogError("missing_columns", f"{name} lacks required columns {missing}")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for line, row in enumerate(reader, start=2):
        if None in row or any(row.get(c) is None for c in header):
            raise CatalogError("malformed_row", f"{name} line {line} has the wrong number of fields")
        if any(not (row[c] or "").strip() for c in required):
            raise CatalogError("malformed_row", f"{name} line {line} has an empty required field")
        ident = row[id_column]
        if not _ID.match(ident):
            raise CatalogError("malformed_row", f"{name} line {line} has an invalid {id_column}")
        if ident in seen:
            raise CatalogError("duplicate_id", f"{name} line {line} repeats an {id_column}")
        seen.add(ident)
        rows.append(row)
    return rows


def load_catalog(dataset_root: Path, expected_manifest_sha256: str) -> Catalog:
    """Verify and load the catalog once. Raises CatalogError; never falls back to accepting arbitrary IDs."""
    root = dataset_root.resolve()
    if not root.is_dir():
        raise CatalogError("dataset_root_missing", "DATASET_ROOT is not a directory")
    manifest_path = root / MANIFEST_RELATIVE_PATH
    manifest_bytes = _read_bytes(manifest_path, "manifest_missing", "the dataset manifest")
    actual = sha256_hex(manifest_bytes)
    if actual != expected_manifest_sha256:
        raise CatalogError("manifest_hash_mismatch",
                           f"manifest SHA-256 {actual} does not match the pinned {expected_manifest_sha256}")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogError("manifest_malformed", "manifest is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") not in SUPPORTED_MANIFEST_VERSIONS:
        raise CatalogError("unsupported_manifest_version", "unsupported manifest schema_version")
    digests, counts = manifest.get("sha256"), manifest.get("rows")
    if not isinstance(digests, dict) or not isinstance(counts, dict):
        raise CatalogError("manifest_malformed", "manifest lacks sha256/rows maps")

    verified: dict[str, bytes] = {}
    for name in ("machines.csv", "operators.csv"):
        digest = digests.get(name)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise CatalogError("manifest_malformed", f"manifest has no valid digest for {name}")
        if not _BASENAME.match(name):
            raise CatalogError("file_outside_root", f"{name} is not a plain file name")
        path = (manifest_path.parent / name).resolve()
        if not path.is_relative_to(root):
            raise CatalogError("file_outside_root", f"{name} resolves outside DATASET_ROOT")
        data = _read_bytes(path, "file_missing", name)
        if sha256_hex(data) != digest:
            raise CatalogError("file_hash_mismatch", f"{name} does not match its manifest digest")
        verified[name] = data

    machine_rows = _parse_csv(verified["machines.csv"], "machines.csv", MACHINE_COLUMNS, "machine_id")
    operator_rows = _parse_csv(verified["operators.csv"], "operators.csv", OPERATOR_COLUMNS, "operator_id")
    for key, rows in (("machines", machine_rows), ("operators", operator_rows)):
        if counts.get(key) != len(rows):
            raise CatalogError("row_count_mismatch", f"{key} row count differs from the manifest")

    seed = manifest.get("generator_seed")
    return Catalog(
        manifest_sha256=actual,
        manifest_schema_version=manifest["schema_version"],
        dataset_origin=str(manifest.get("dataset_origin", "unknown")),
        generator_seed=seed if isinstance(seed, int) else None,
        machines=MappingProxyType({r["machine_id"]: MachineEntry(r["machine_id"], r["model"], r["category"])
                                   for r in machine_rows}),
        operators=frozenset(r["operator_id"] for r in operator_rows),
        file_sha256=MappingProxyType({n: digests[n] for n in verified}),
        operator_skill=MappingProxyType({r["operator_id"]: r["operator_skill"].strip().lower() for r in operator_rows
                                         if (r.get("operator_skill") or "").strip().lower() in SKILL_LEVELS}),
        machine_age_years=MappingProxyType({r["machine_id"]: float(r["machine_age_years"]) for r in machine_rows
                                            if _number(r.get("machine_age_years"))}),
    )


def _number(value: str | None) -> bool:
    try:
        return value is not None and float(value) >= 0
    except ValueError:
        return False


# ---------------------------------------------------------------------- trusted session bindings


@dataclass(frozen=True)
class SessionBinding:
    binding_id: str
    operator_id: str
    machine_id: str
    site_id: str
    shift_id: str


@dataclass(frozen=True)
class SessionBindings:
    """Server-controlled site/shift associations. A caller-supplied site/shift is accepted only if it matches."""

    sha256: str
    origin: str
    bindings: tuple[SessionBinding, ...]

    def match(self, operator_id: str, machine_id: str, site_id: str, shift_id: str) -> SessionBinding | None:
        for b in self.bindings:
            if (b.operator_id, b.machine_id, b.site_id, b.shift_id) == (operator_id, machine_id, site_id, shift_id):
                return b
        return None


def load_session_bindings(path: Path, catalog: Catalog) -> SessionBindings:
    data = _read_bytes(path, "bindings_missing", "SESSION_BINDINGS_PATH")
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogError("bindings_malformed", "session bindings file is not valid UTF-8 JSON") from exc
    if not isinstance(doc, dict) or doc.get("schema") != BINDINGS_SCHEMA:
        raise CatalogError("bindings_malformed", f"session bindings file must declare schema {BINDINGS_SCHEMA}")
    if doc.get("catalog_manifest_sha256") != catalog.manifest_sha256:
        raise CatalogError("bindings_catalog_mismatch", "session bindings were written for a different catalog")
    origin = doc.get("origin")
    items = doc.get("bindings")
    if not isinstance(origin, str) or not origin or not isinstance(items, list):
        raise CatalogError("bindings_malformed", "session bindings need an origin and a bindings list")
    out: list[SessionBinding] = []
    ids: set[str] = set()
    keys: set[tuple[str, ...]] = set()
    for item in items:
        fields = ("binding_id", "operator_id", "machine_id", "site_id", "shift_id")
        if not isinstance(item, dict) or set(item) != set(fields) or \
                not all(isinstance(item[f], str) and _ID.match(item[f]) for f in fields):
            raise CatalogError("bindings_malformed", "each binding needs exactly five valid identifier fields")
        binding = SessionBinding(**{f: item[f] for f in fields})
        if not catalog.has_operator(binding.operator_id) or not catalog.has_machine(binding.machine_id):
            raise CatalogError("bindings_unknown_id", f"binding {binding.binding_id} names a non-catalog identifier")
        key = (binding.operator_id, binding.machine_id, binding.site_id, binding.shift_id)
        if binding.binding_id in ids or key in keys:
            raise CatalogError("bindings_duplicate", "session bindings repeat an id or association")
        ids.add(binding.binding_id)
        keys.add(key)
        out.append(binding)
    return SessionBindings(sha256=sha256_hex(data), origin=origin, bindings=tuple(out))
