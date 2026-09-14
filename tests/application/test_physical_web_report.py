from copy import deepcopy
from dataclasses import replace
import importlib.util
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from rebar.application.physical_layout_recovery import recover_physical_layout
from rebar.application.physical_web_report import physical_web_report
from rebar.optimization.contracts.physical import PhysicalNormalizationConfig


@pytest.fixture(scope="module")
def physical_case():
    path = Path(__file__).with_name("test_patterned_layout_recovery.py")
    spec = importlib.util.spec_from_file_location("web_physical_fixture", path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    problem, solution, settings = fixture._source()
    recovery = recover_physical_layout(problem, solution, settings,
        normalization_config=PhysicalNormalizationConfig(time_limit_s=5, stock_balance_time_limit_s=2),
        balance_time_limit_s=5, stock_time_limit_s=3)
    assert recovery.packet is not None
    return problem, recovery


def test_displayed_geometry_mass_count_and_schedule_are_one_accepted_inventory(physical_case):
    problem, recovery = physical_case
    before = deepcopy(recovery)
    report = physical_web_report(problem, recovery)
    assert report["output_kind"] == "normalized-physical-bars"
    assert not report["placement_eligible"] and report["host_envelope"] is None
    point = report["front"][0]
    count = 0
    for direction in report["directions"]:
        candidate = direction["candidates"][0]
        assert candidate["zone_drafts"] == []
        assert direction["source_zone_drafts"]
        assert candidate["coverage"]["uncovered_cell_count"] == 0
        svg = ET.fromstring(candidate["svg"])
        lines = svg.findall(".//{http://www.w3.org/2000/svg}line")
        assert len(lines) == len(candidate["physical_bars"]) == candidate["metrics"]["physical_bar_count"]
        count += len(lines)
    assert count == point["physical_bar_count"] == sum(row["physical_bar_count"] for row in point["bar_schedule"])
    assert point["additional_mass_kg"] == pytest.approx(sum(row["total_mass_kg"] for row in point["bar_schedule"]))
    assert "actual-3d-placement-and-layer-order" in report["blocking_check_ids"]
    assert report["physical_trial_packet"]["expected"] == recovery.review["expected"]
    assert recovery == before


@pytest.mark.parametrize("field,value", [("physical_bar_count", 1), ("additional_mass_kg", 1)])
def test_cannot_mix_geometry_and_metrics_from_different_stages(physical_case, field, value):
    problem, original = physical_case
    review = deepcopy(original.review)
    review["expected"][field] = value
    with pytest.raises(ValueError, match="Displayed physical inventory"):
        physical_web_report(problem, replace(original, review=review))


def test_rejected_physical_pipeline_is_labeled_as_source_only(physical_case):
    problem, recovery = physical_case
    report = physical_web_report(problem, replace(recovery, packet=None))
    assert report["output_kind"] == "source-zones-only"
    assert "промежуточный" in report["warning"]
    assert "physical_trial_packet" not in report
