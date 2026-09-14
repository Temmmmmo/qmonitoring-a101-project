"""Candidate hints cannot bypass the complete typed transverse checker."""
from copy import deepcopy
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    module = load_module(ROOT / "scripts/experiment_fe_transverse_repair.py", "transverse_cli_tests")
    fixtures = load_module(ROOT / "tests/optimization/test_fe_transverse_repair.py", "transverse_case_tests")
    bars, lanes, problem, host = fixtures.case()
    original = tmp_path / "original-fe.json"
    original.write_text("{}", encoding="utf-8")
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    pool = {"schema_version": "free-axis-catalogue-research/v1", "placement_eligible": False,
        "engineering_approval": False, "source_demand_removed": False,
        "source_files": [{"path": str(original), "sha256": digest}],
        "catalogue_candidates": [{"direction": str(bars[0].direction), "bar_id": bars[0].id,
            "candidates": [{"coordinate_mm": 150, "length_mm": 11700,
                "background_original_contact_and_no_penetration": True,
                "current_other_body_collisions": []}]}]}
    pool_path = tmp_path / "candidate-pool.json"
    pool_path.write_bytes(module._bytes(pool))
    inputs = SimpleNamespace(bars=bars, lanes=lanes, problem=problem, host=host,
        source_files=(), input_sha256=digest, input_report={"source_host_report_sha256": "b"*64})
    # The shared loader has its own source-chain integration tests. Here only its
    # IO boundary is mocked; real candidate selection, FE, host, stock checks run.
    monkeypatch.setattr(module, "load_fe_research_inputs", lambda **kwargs: inputs)
    monkeypatch.setattr(module, "_code_digest", lambda: "a"*64)
    args = SimpleNamespace(output=tmp_path/"out.json", candidate_pool=pool_path,
        shifted_dir=tmp_path, snapshot=tmp_path/"snapshot.json", working_host_report=tmp_path/"host.json",
        fe_report=original, candidate_id="plate:52", confirm_identity_xy=True,
        maximum_shift_mm=300, stock_time_limit_s=1, maximum_candidates_per_bar=128,
        maximum_total_candidates=5000)
    return module, inputs, args, pool


def test_complete_cli_keeps_original_inputs_and_writes_only_false_research(setup):
    module, inputs, args, _ = setup
    before = deepcopy(inputs.bars)
    sources_before = args.fe_report.read_bytes(), args.candidate_pool.read_bytes()
    report = module.run(args)
    assert report["schema_version"] == "fe-transverse-repair-experiment/v1"
    assert report["source_report_sha256"] == inputs.input_sha256
    assert report["checks"]["host_blocked_after"] == 0 and report["checks"]["moved_bar_count"] == 1
    assert report["checks"]["stock_cutting"]["status"] == "pass"
    assert not report["placement_eligible"] and not report["structural_placement_supported"]
    assert args.output.exists() and inputs.bars == before
    assert sources_before == (args.fe_report.read_bytes(), args.candidate_pool.read_bytes())
    with pytest.raises(ValueError, match="cannot be overwritten"):
        module.run(args)


def test_geometry_hint_and_its_pass_flags_do_not_authorize_lost_FE(setup):
    module, _, args, pool = setup
    pool["catalogue_candidates"][0]["candidates"][0]["coordinate_mm"] = 250
    args.candidate_pool.write_bytes(module._bytes(pool))
    with pytest.raises(ValueError, match="frozen original owner FE"):
        module.run(args)
    assert not args.output.exists()


def test_pool_must_bind_exact_preceding_FE_report(setup):
    module, _, args, pool = setup
    pool["source_files"][0]["sha256"] = "0"*64
    args.candidate_pool.write_bytes(module._bytes(pool))
    with pytest.raises(ValueError):
        module.run(args)
    assert not args.output.exists()


@pytest.mark.parametrize("bad", (True, float("nan"), float("inf"), "150"))
def test_pool_rejects_nonfinite_or_untyped_axes(setup, bad):
    module, inputs, _, pool = setup
    pool["catalogue_candidates"][0]["candidates"][0]["coordinate_mm"] = bad
    with pytest.raises(ValueError, match="Finite numeric"):
        module.select_candidates(pool, inputs, maximum_shift_mm=300,
            maximum_candidates_per_bar=128, maximum_total_candidates=5000)


@pytest.mark.parametrize("change", ("unknown", "duplicate", "true_permission", "raw_budget"))
def test_complete_candidate_pool_has_boundaries_and_no_unknown_or_duplicate_IDs(setup, change):
    module, inputs, _, pool = setup
    if change == "unknown":
        pool["catalogue_candidates"][0]["bar_id"] = "invented"
    elif change == "duplicate":
        pool["catalogue_candidates"] *= 2
    elif change == "true_permission":
        pool["placement_eligible"] = True
    else:
        pool["catalogue_candidates"][0]["candidates"] *= 2
    with pytest.raises(ValueError):
        module.select_candidates(pool, inputs, maximum_shift_mm=300,
            maximum_candidates_per_bar=128, maximum_total_candidates=1 if change == "raw_budget" else 5000)


@pytest.mark.parametrize("key,value", (("confirm_identity_xy", False), ("maximum_shift_mm", True),
    ("maximum_shift_mm", -1), ("maximum_shift_mm", 301), ("stock_time_limit_s", 0),
    ("maximum_candidates_per_bar", 0), ("maximum_total_candidates", True)))
def test_argument_checks_precede_io(setup, key, value):
    module, _, args, _ = setup
    setattr(args, key, value)
    with pytest.raises(ValueError):
        module.validate_args(args)
    assert not args.output.exists()
