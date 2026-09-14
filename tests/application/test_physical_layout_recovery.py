"""Application orchestration is complete, reproducible, and artifact-independent."""
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from rebar.application.physical_layout_recovery import (
    recover_physical_layout, recover_physical_layout_from_report,
)
from rebar.optimization.contracts.physical import PhysicalBar, PhysicalNormalizationConfig


def _fixture(filename):
    spec = importlib.util.spec_from_file_location("pipeline_fixture", Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def source_case():
    return _fixture("test_physical_bar_trial.py").physical_source_case()


@dataclass(frozen=True)
class _Normalized:
    bars: tuple
    trace: tuple = ({"operation": "synthetic-bounded-normalizer"},)
    status: str = "normalized"
    budget_exhausted: bool = False


def _deduplicate(sources, *, config):
    grouped = {}
    for bar in sources:
        key = (bar.direction, bar.steel_class, bar.diameter_mm, bar.transverse_axis_mm, bar.installed_interval_mm)
        grouped.setdefault(key, []).append(bar.id)
    bars = tuple(PhysicalBar(f"normalized-{index}", *key, tuple(ids))
                 for index, (key, ids) in enumerate(grouped.items()))
    return _Normalized(bars)


def _unchanged(sources, *, config):
    return _Normalized(tuple(PhysicalBar(s.id, s.direction, s.steel_class, s.diameter_mm,
        s.transverse_axis_mm, s.installed_interval_mm, (s.id,)) for s in sources),
        status="budget_exhausted", budget_exhausted=True)


@pytest.fixture
def mocked_core(monkeypatch):
    monkeypatch.setattr("rebar.application.physical_layout_recovery.normalize_physical_bars", _deduplicate)


def test_resume_uses_typed_core_and_emits_exact_bound_canonical_bytes(source_case, mocked_core):
    problem, report, _raw = source_case
    provenance = {"input_files": [{"path": "source.dxf", "sha256": "a" * 64}],
                  "choice": "explicit caller selection", "placement_eligible": True}
    before = deepcopy((problem, report, provenance))
    result = recover_physical_layout_from_report(report, problem,
        normalization_config=PhysicalNormalizationConfig(), source_provenance=provenance)
    assert result.status == "prepared_rollback_only"
    assert result.packet["expected"]["source_zone_count"] == 8
    assert result.packet["expected"]["physical_bar_count"] == 32
    assert not result.packet["placement_eligible"]
    assert result.patterned_report["source_provenance"] == provenance
    assert result.normalization_report["configuration"]["allow_diameter_increase"] is False
    assert result.normalization_report["accepted"]["raw_bars_by_direction"]
    assert result.review["normalization_trace"] == [{"operation": "synthetic-bounded-normalizer"}]
    assert all(row["status"] == "pass" for row in result.review["source_original_coverage"])
    for value, content in ((result.patterned_report, result.patterned_report_bytes),
                           (result.normalization_report, result.normalization_report_bytes),
                           (result.packet, result.packet_bytes), (result.review, result.review_bytes)):
        assert json.loads(content) == value
        assert content == json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
        assert not content.endswith(b"\n")
    assert result.packet["source_report_sha256"] == hashlib.sha256(result.patterned_report_bytes).hexdigest()
    assert result.packet["raw_report_sha256"] == hashlib.sha256(result.normalization_report_bytes).hexdigest()
    assert result.review["packet_sha256"] == hashlib.sha256(result.packet_bytes).hexdigest()
    assert (problem, report, provenance) == before


def test_full_typed_layout_flows_through_patterns_without_script_or_artifact_input(mocked_core):
    problem, solution, settings = _fixture("test_patterned_layout_recovery.py")._source()
    result = recover_physical_layout(problem, solution, settings,
        normalization_config=PhysicalNormalizationConfig(), balance_time_limit_s=5)
    assert result.packet["expected"]["physical_bar_count"] == 32
    assert result.packet["expected"]["source_zone_count"] == 4
    assert result.review["stock_cutting"]["status"] == "pass"
    assert result.patterned_report["source_demand_preserved"]
    assert result.patterned_report["coverage_policy"].endswith("monotone-single-addition/research-v1")


def test_real_core_normalizer_is_called_on_complete_source_without_artifact_fallback(source_case):
    problem, report, _raw = source_case
    result = recover_physical_layout_from_report(report, problem,
        normalization_config=PhysicalNormalizationConfig(time_limit_s=5, stock_balance_time_limit_s=2))
    assert result.packet is not None
    assert result.packet["expected"]["source_zone_count"] == 8
    assert result.packet["expected"]["physical_bar_count"] == 32
    assert result.review["source_bar_reference_count"] == 64
    assert result.review["stock_cutting"]["status"] == "pass"
    assert result.normalization_report["normalization"]["source_certificate"]
    assert result.normalization_report["application_trace"] == []
    assert result.packet["manual_joint_tasks"] == []


def test_resume_keeps_existing_provenance_without_mutating_the_report(source_case, mocked_core):
    problem, report, _raw = deepcopy(source_case)
    report["source_provenance"] = {"case_selection": "explicit original choice"}
    before = deepcopy(report)
    result = recover_physical_layout_from_report(report, problem,
        normalization_config=PhysicalNormalizationConfig())
    assert result.review["source_provenance"] == report["source_provenance"]
    assert result.normalization_report["source_provenance"] == report["source_provenance"]
    assert report == before


def test_budget_exhaustion_keeps_complete_verified_source_and_visible_manual_tasks(source_case, monkeypatch):
    monkeypatch.setattr("rebar.application.physical_layout_recovery.normalize_physical_bars", _unchanged)
    problem, report, _raw = source_case
    result = recover_physical_layout_from_report(report, problem,
        normalization_config=PhysicalNormalizationConfig())
    assert result.status == "prepared_with_manual_tasks"
    assert result.packet["expected"]["physical_bar_count"] == 64
    assert result.review["source_bar_reference_count"] == 64
    assert result.review["normalization_status"] == "budget_exhausted"
    assert result.review["normalization_budget_exhausted"] is True
    assert len(result.packet["manual_joint_tasks"]) == 32
    assert result.review["stock_cutting"]["status"] == "pass"
    assert not result.packet["placement_eligible"]


def test_rejected_core_proposal_falls_back_only_to_complete_independently_checked_source(source_case, monkeypatch):
    def invalid(sources, *, config):
        result = _deduplicate(sources, config=config)
        first = replace(result.bars[0], source_bar_ids=("unknown/0/0",))
        return replace(result, bars=(first, *result.bars[1:]))
    monkeypatch.setattr("rebar.application.physical_layout_recovery.normalize_physical_bars", invalid)
    problem, report, _raw = source_case
    result = recover_physical_layout_from_report(report, problem,
        normalization_config=PhysicalNormalizationConfig())
    assert result.packet["expected"]["physical_bar_count"] == 64
    assert result.review["source_bar_reference_count"] == 64
    assert result.normalization_report["status"] == "normalization_rejected_source_retained"
    assert result.normalization_report["application_trace"][0]["status"] == "candidate_rejected"
    assert "Unknown" in result.normalization_report["application_trace"][0]["reason"]
    assert "normalization" not in result.normalization_report
    assert result.normalization_report["proposed_result_status"] == "rejected_by_independent_physical_validation"
    assert len(result.normalization_report["proposed_result"]["bars"]) == 32
    assert result.normalization_report["accepted_metrics"]["physical_bar_count"] == 64
    assert result.packet["raw_report_sha256"] == hashlib.sha256(result.normalization_report_bytes).hexdigest()


def test_mass_target_fail_keeps_complete_diagnostic_inventory_but_never_claims_limit_pass(source_case, monkeypatch):
    monkeypatch.setattr("rebar.application.physical_layout_recovery.normalize_physical_bars", _unchanged)
    problem, report, _raw = source_case
    result = recover_physical_layout_from_report(report, problem,
        normalization_config=PhysicalNormalizationConfig(maximum_mass_kg=1))
    assert result.packet is not None
    assert result.packet["expected"]["physical_bar_count"] == 64
    assert result.review["normalization_mass_limit"]["status"] == "fail"
    assert result.review["normalization_mass_limit"]["kg"] == 1
    assert "normalization-mass-target-not-met" in result.packet["source_blockers"]
    assert "normalization-mass-target-not-met" in result.review["permanent_blockers"]
    assert result.normalization_report["accepted_metrics"] == result.packet["expected"]
    assert result.review["packet_sha256"] == hashlib.sha256(result.packet_bytes).hexdigest()
    assert result.review["raw_report_sha256"] == hashlib.sha256(result.normalization_report_bytes).hexdigest()


def test_failed_candidate_and_failed_full_fallback_produce_explicit_blocked_result(source_case, mocked_core, monkeypatch):
    def reject(*_a, **_kw):
        raise ValueError("independent complete stock check not passed")
    monkeypatch.setattr("rebar.application.physical_layout_recovery.build_physical_bar_trial", reject)
    problem, report, _raw = source_case
    result = recover_physical_layout_from_report(report, problem,
        normalization_config=PhysicalNormalizationConfig())
    assert result.status == "blocked_complete_physical_validation"
    assert result.packet is None and result.packet_bytes is None
    assert result.review["placement_eligible"] is False
    assert result.normalization_report["accepted"]["raw_bars_by_direction"]
    assert sum(len(v) for v in result.normalization_report["accepted"]["raw_bars_by_direction"].values()) == 64
    assert len(result.review["issues"]) == 2


def test_failed_patterned_stock_does_not_start_normalization_or_emit_partial_trial(monkeypatch):
    from rebar.application.patterned_layout_recovery import recover_patterned_layout
    problem, solution, settings = _fixture("test_patterned_layout_recovery.py")._source()
    source = recover_patterned_layout(problem, solution, settings, balance_time_limit_s=5)
    report = deepcopy(source.report)
    report["front"] = []
    report["status"] = "stock_balance_not_found"
    monkeypatch.setattr("rebar.application.physical_layout_recovery.recover_patterned_layout",
                        lambda *_a, **_kw: replace(source, report=report, stock_balanced=False))
    def forbidden(*_a, **_kw):
        pytest.fail("Normalizer must not receive an unverified partial source")
    monkeypatch.setattr("rebar.application.physical_layout_recovery.normalize_physical_bars", forbidden)
    result = recover_physical_layout(problem, solution, settings, normalization_config=PhysicalNormalizationConfig())
    assert result.status == "blocked_patterned_stock"
    assert result.packet is None and result.packet_bytes is None
    assert result.normalization_report["accepted"] is None
    assert result.patterned_report["source_demand_preserved"]


def test_bad_source_geometry_is_rejected_before_core_invocation(source_case, monkeypatch):
    problem, report, _raw = deepcopy(source_case)
    report["directions"][0]["candidates"][-1]["zone_drafts"][0]["components"][0]["mass_kg"] += 1
    def forbidden(*_a, **_kw):
        pytest.fail("Invalid source reached core")
    monkeypatch.setattr("rebar.application.physical_layout_recovery.normalize_physical_bars", forbidden)
    with pytest.raises(ValueError):
        recover_physical_layout_from_report(report, problem, normalization_config=PhysicalNormalizationConfig())


@pytest.mark.parametrize("kwargs", [{"normalization_config": None}, {"source_provenance": []},
    {"source_provenance": {"bad": float("nan")}}, {"candidate_index": True}, {"stock_time_limit_s": 0}])
def test_invalid_explicit_configuration_or_provenance_rejected(source_case, mocked_core, kwargs):
    problem, report, _raw = source_case
    params = {"normalization_config": PhysicalNormalizationConfig(), **kwargs}
    with pytest.raises(ValueError):
        recover_physical_layout_from_report(report, problem, **params)
