"""Verified catalog loading: pinned manifest hash, file digests, strict CSV validation and path resolution."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from cocoon_agent.catalog import CatalogError, load_catalog, load_session_bindings
from cocoon_agent.config import PINNED_DEV_MANIFEST_SHA256, SERVICE_DIR, Settings

from .conftest import FIXTURE_BINDINGS, FIXTURE_CATALOG, FIXTURE_MANIFEST_SHA256, MACHINES, REPO_ROOT, write_catalog

MACHINE_HEADER = "machine_id,model,category\n"
GOOD_MACHINES = MACHINE_HEADER + "EXC_DEMO_001,Cat 320,hydraulic_excavator\n"
GOOD_OPERATORS = "operator_id\nOP_A\n"


def _issue(root: Path, expected: str) -> str:
    with pytest.raises(CatalogError) as err:
        load_catalog(root, expected)
    return err.value.issue


def test_committed_fixture_catalog_loads_as_an_immutable_snapshot():
    catalog = load_catalog(FIXTURE_CATALOG, FIXTURE_MANIFEST_SHA256)
    assert catalog.manifest_sha256 == FIXTURE_MANIFEST_SHA256
    assert tuple(catalog.machines) == MACHINES
    assert catalog.machines["TRK_DEMO_001"].model == "Cat 793"
    assert catalog.operators == {"OP_TEST_1", "OP_TEST_2", "OP_TEST_3"}
    with pytest.raises(TypeError):
        catalog.machines["NEW"] = None  # type: ignore[index]
    with pytest.raises(AttributeError):
        catalog.manifest_sha256 = "x"  # type: ignore[misc]


@pytest.mark.parametrize("cwd", [REPO_ROOT, SERVICE_DIR, SERVICE_DIR / "tests"])
def test_dataset_root_resolves_from_the_service_directory_not_the_cwd(cwd, monkeypatch):
    monkeypatch.chdir(cwd)
    base = {"COCOON_SERVICE_TOKEN": "t"}
    default = Settings(**base, _env_file=None)
    assert default.dataset_root == (REPO_ROOT / "Cocoon_Dataset_v1").resolve()
    relative = Settings(**base, DATASET_ROOT="tests/fixtures/catalog", _env_file=None)
    assert relative.dataset_root == FIXTURE_CATALOG.resolve()
    absolute = Settings(**base, DATASET_ROOT=str(FIXTURE_CATALOG.resolve()), _env_file=None)
    assert absolute.dataset_root == FIXTURE_CATALOG.resolve()
    assert default.dataset_manifest_sha256 == PINNED_DEV_MANIFEST_SHA256


def test_expected_hash_setting_must_be_a_full_sha256():
    with pytest.raises(ValueError):
        Settings(COCOON_SERVICE_TOKEN="t", DATASET_MANIFEST_SHA256="bdd55830", _env_file=None)


def test_wrong_pinned_fingerprint_is_refused():
    assert _issue(FIXTURE_CATALOG, "0" * 64) == "manifest_hash_mismatch"


def test_changed_referenced_file_is_refused_even_with_the_right_manifest(tmp_path):
    root = tmp_path / "cat"
    shutil.copytree(FIXTURE_CATALOG, root)
    machines = root / "data" / "generated" / "machines.csv"
    machines.write_bytes(machines.read_bytes() + b"ZZZ_NEW_001,Cat X,other,synthetic\n")
    assert _issue(root, FIXTURE_MANIFEST_SHA256) == "file_hash_mismatch"


def test_missing_root_manifest_and_files(tmp_path):
    assert _issue(tmp_path / "nope", FIXTURE_MANIFEST_SHA256) == "dataset_root_missing"
    assert _issue(tmp_path, FIXTURE_MANIFEST_SHA256) == "manifest_missing"
    root = tmp_path / "cat"
    digest = write_catalog(root)
    (root / "data" / "generated" / "operators.csv").unlink()
    assert _issue(root, digest) == "file_missing"


@pytest.mark.parametrize("machines,operators,issue", [
    (GOOD_MACHINES + "EXC_DEMO_001,Cat 320,hydraulic_excavator\n", GOOD_OPERATORS, "duplicate_id"),
    (GOOD_MACHINES, "operator_id\nOP_A\nOP_A\n", "duplicate_id"),
    (GOOD_MACHINES + "BAD ID!,Cat 320,x\n", GOOD_OPERATORS, "malformed_row"),
    (GOOD_MACHINES + ",Cat 320,x\n", GOOD_OPERATORS, "malformed_row"),
    (GOOD_MACHINES + "DOZ_DEMO_001,,bulldozer\n", GOOD_OPERATORS, "malformed_row"),
    (GOOD_MACHINES + "DOZ_DEMO_001,Cat D6,bulldozer,extra\n", GOOD_OPERATORS, "malformed_row"),
    ("machine_id,model\nEXC_DEMO_001,Cat 320\n", GOOD_OPERATORS, "missing_columns"),
    (GOOD_MACHINES, "id\nOP_A\n", "missing_columns"),
])
def test_malformed_catalog_contents_are_refused(tmp_path, machines, operators, issue):
    root = tmp_path / "cat"
    assert _issue(root, write_catalog(root, machines, operators)) == issue


def test_row_count_and_manifest_version_are_checked(tmp_path):
    root = tmp_path / "a"
    assert _issue(root, write_catalog(root, rows={"machines": 9, "operators": 3})) == "row_count_mismatch"
    root = tmp_path / "b"
    assert _issue(root, write_catalog(root, schema_version="2.0")) == "unsupported_manifest_version"


def test_catalog_files_must_stay_inside_the_dataset_root(tmp_path):
    root = tmp_path / "cat"
    digest = write_catalog(root)
    target = tmp_path / "outside.csv"
    shutil.copy(root / "data" / "generated" / "machines.csv", target)
    link = root / "data" / "generated" / "machines.csv"
    link.unlink()
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("symlinks not permitted on this machine")
    assert _issue(root, digest) == "file_outside_root"


def test_session_bindings_are_validated_against_the_catalog(tmp_path):
    catalog = load_catalog(FIXTURE_CATALOG, FIXTURE_MANIFEST_SHA256)
    bindings = load_session_bindings(FIXTURE_BINDINGS, catalog)
    assert bindings.match("OP_TEST_1", "EXC_DEMO_001", "SITE_FIXTURE_A", "SHIFT_FIXTURE_A_DAY1").binding_id == \
        "BIND_TEST_1"
    assert bindings.match("OP_TEST_2", "EXC_DEMO_001", "SITE_FIXTURE_A", "SHIFT_FIXTURE_A_DAY1") is None
    doc = json.loads(FIXTURE_BINDINGS.read_text(encoding="utf-8"))
    for mutate, issue in (
        (lambda d: d.update(catalog_manifest_sha256="0" * 64), "bindings_catalog_mismatch"),
        (lambda d: d["bindings"][0].update(operator_id="OP_NOBODY"), "bindings_unknown_id"),
        (lambda d: d["bindings"].append(dict(d["bindings"][0])), "bindings_duplicate"),
        (lambda d: d["bindings"][0].update(extra="x"), "bindings_malformed"),
    ):
        bad = json.loads(json.dumps(doc))
        mutate(bad)
        path = tmp_path / f"{issue}.json"
        path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(CatalogError) as err:
            load_session_bindings(path, catalog)
        assert err.value.issue == issue


DEV_DATASET = REPO_ROOT / "Cocoon_Dataset_v1"


@pytest.mark.skipif(not DEV_DATASET.is_dir(), reason="local development dataset not present (untracked)")
def test_local_development_dataset_matches_the_pinned_reference():
    """LOCAL INTEGRATION ONLY: the untracked, checksum-validated dev snapshot. Not data-owner review."""
    catalog = load_catalog(DEV_DATASET, PINNED_DEV_MANIFEST_SHA256)
    assert {m: e.model for m, e in catalog.machines.items()} == {
        "EXC_DEMO_001": "Cat 320", "DOZ_DEMO_001": "Cat D6", "LDR_DEMO_001": "Cat 950 GC",
        "TRK_DEMO_001": "Cat 793", "BHL_DEMO_001": "Cat 420"}
    assert len(catalog.operators) == 15 and "OP_DEMO_1_1" in catalog.operators
