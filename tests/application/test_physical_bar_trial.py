"""Physical normalization preserves source owners, demand, 40d and rollback semantics."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from rebar.application.patterned_layout_recovery import recover_patterned_layout
from rebar.application.physical_bar_trial import build_physical_bar_trial
from rebar.optimization import PlateDirectionSolution, StrongestBBoxOptimizer, build_plate_solution, evaluate_layout


def physical_source_case(nominal_step=150):
    """Small real-contract source: eight parameterized zones, 32 merged physical bars."""
    spec = importlib.util.spec_from_file_location("physical_source_fixture", Path(__file__).with_name(
        "test_patterned_layout_recovery.py"))
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    problem, solution, settings = fixture._source()
    if nominal_step != 150:
        problems = []
        for original in problem.direction_problems:
            level = original.demand.levels[1]
            addition = replace(level.additional, step=nominal_step)
            level = replace(level, additional=addition, recipe=replace(level.recipe, additions=(addition,)))
            problems.append(replace(original, demand=replace(original.demand,
                levels=(original.demand.levels[0], level))))
        problem = replace(problem, direction_problems=tuple(problems))
        solution = build_plate_solution(PlateDirectionSolution(p.demand.direction, StrongestBBoxOptimizer().solve(p))
                                       for p in problems)
    doubled = []
    for original, item in zip(problem.direction_problems, solution.direction_solutions):
        zones = (*item.solution.zones, replace(item.solution.zones[0], id="second-owner"))
        check = evaluate_layout(original, zones, item.solution.request)
        doubled.append(PlateDirectionSolution(item.direction,
            replace(item.solution, zones=zones, metrics=check.metrics)))
    recovered = recover_patterned_layout(problem, build_plate_solution(doubled), settings, balance_time_limit_s=5)
    assert recovered.stock_balanced
    report = recovered.report
    raw = {}
    for direction, index in zip(report["directions"], report["front"][0]["direction_candidate_indexes"]):
        name = "{layer}-{axis}".format(**direction["direction"])
        candidate = next(c for c in direction["candidates"] if c["candidate_index"] == index)
        axis = 0 if name.endswith("X") else 1
        grouped = {}
        for zone in candidate["zone_drafts"]:
            for component in zone["components"]:
                box = component["bar_axis_bbox_mm"]
                for bar_index, coordinate in enumerate(component["axis_coordinates_mm"]):
                    key = (component["diameter_mm"], box[axis], box[axis + 2], coordinate)
                    source_id = "{}/{}/{}".format(zone["source_zone_id"], component["component_index"], bar_index)
                    if key not in grouped:
                        grouped[key] = {"id": f"physical-{len(grouped)}", "steel_class": "A500",
                            "diameter_mm": key[0], "coordinate_mm": coordinate,
                            "longitudinal_mm": [key[1], key[2]], "source_bar_ids": []}
                    grouped[key]["source_bar_ids"].append(source_id)
        raw[name] = list(grouped.values())
    return problem, report, raw


@pytest.fixture(scope="module")
def physical_case():
    return physical_source_case()


def _build(case, **kwargs):
    problem, report, raw = case
    return build_physical_bar_trial(report, raw, original_problem=problem,
        source_report_sha256="a" * 64, raw_report_sha256="b" * 64, **kwargs)


def test_full_physical_packet_preserves_many_owners_without_fake_source_zone_counts(physical_case):
    before = deepcopy(physical_case)
    result = _build(physical_case)
    packet, review = result.packet, result.review
    assert packet["schema_version"] == "physical-bar-plan-trial/v1"
    assert packet["mode"] == "commit-readback-rollback"
    assert packet["placement_eligible"] is False
    assert packet["expected"]["source_zone_count"] == 8
    assert packet["expected"]["execution_group_count"] == 4
    assert packet["expected"]["physical_bar_count"] == 32
    assert packet["expected"]["run_count"] == 8
    assert packet["expected"]["position_count"] == 1
    assert review["prior_metrics"]["physical_bar_count"] == 64
    assert review["expected"] == packet["expected"]
    assert review["manual_joint_tasks"] == packet["manual_joint_tasks"] == []
    assert review["stock_cutting"]["status"] == "pass"
    assert review["source_axis_and_new40d_status"] == "pass"
    assert review["same_plane_conflicts"]["body_intersection_count"] == 0
    assert review["engineering_approval"] is False
    assert all(r["status"] == "pass" for r in review["source_original_coverage"])
    assert review["packet_sha256"] == hashlib.sha256(json.dumps(packet, ensure_ascii=False,
        allow_nan=False, indent=2).encode("utf-8")).hexdigest()
    for row in packet["directions"]:
        for run in row["runs"]:
            assert "zone_id" not in run and "component_index" not in run
            assert all(len(bar["source_refs"]) == 2 for bar in run["bar_sources"])
    assert physical_case == before


@pytest.mark.parametrize("change", ["missing_direction", "missing_bar", "missing_owner", "duplicate_owner",
    "unknown_owner", "weaker_diameter", "changed_steel", "changed_axis", "short_40d", "larger_diameter_short_40d",
    "uncatalogued_length", "nonfinite", "bool_diameter", "duplicate_bar_id", "extra_field"])
def test_invalid_or_undercovering_physical_plan_never_exports(physical_case, change):
    problem, report, raw = deepcopy(physical_case)
    bars = raw["bottom-X"]
    bar = bars[0]
    if change == "missing_direction":
        del raw["top-Y"]
    elif change == "missing_bar":
        bars.pop()
    elif change == "missing_owner":
        bar["source_bar_ids"].pop()
    elif change == "duplicate_owner":
        bar["source_bar_ids"].append(bar["source_bar_ids"][0])
    elif change == "unknown_owner":
        bar["source_bar_ids"][0] = "unknown/0/0"
    elif change == "weaker_diameter":
        bar["diameter_mm"] = 16
    elif change == "changed_steel":
        bar["steel_class"] = "A400"
    elif change == "changed_axis":
        bar["coordinate_mm"] += 1
    elif change == "short_40d":
        bar["longitudinal_mm"] = [-719, 2206]
    elif change == "larger_diameter_short_40d":
        bar["diameter_mm"] = 25
    elif change == "uncatalogued_length":
        bar["longitudinal_mm"][1] += 1
    elif change == "nonfinite":
        bar["coordinate_mm"] = float("nan")
    elif change == "bool_diameter":
        bar["diameter_mm"] = True
    elif change == "duplicate_bar_id":
        bars[1]["id"] = bar["id"]
    else:
        bar["engineering_approval"] = True
    with pytest.raises(ValueError):
        _build((problem, report, raw))


@pytest.mark.parametrize("field", ["axis_coordinates_mm", "mass_kg", "required_length_mm", "installed_length_mm"])
def test_report_geometry_is_reconstructed_not_trusted(physical_case, field):
    problem, report, raw = deepcopy(physical_case)
    component = report["directions"][0]["candidates"][-1]["zone_drafts"][0]["components"][0]
    if field == "axis_coordinates_mm":
        component[field][0] += 1
    else:
        component[field] += 1
    with pytest.raises(ValueError):
        _build((problem, report, raw))


def test_fresh_original_demand_revalidation_rejects_report_claiming_no_holes(physical_case):
    problem, report, raw = deepcopy(physical_case)
    original = problem.direction_problems[0]
    moved = replace(original.demand.cells[0], poly=tuple((x + 3000, y) for x, y in original.demand.cells[0].poly))
    changed = replace(original, demand=replace(original.demand, cells=(moved, *original.demand.cells[1:])))
    with pytest.raises(ValueError):
        _build((replace(problem, direction_problems=(changed, *problem.direction_problems[1:])), report, raw))


def test_stock_check_is_independent_and_not_copied_from_source_metadata(physical_case, monkeypatch):
    monkeypatch.setattr("rebar.application.physical_bar_trial.check_stock_cutting",
                        lambda *_a, **_kw: {"status": "fail", "reason": "changed physical inventory"})
    with pytest.raises(ValueError, match="stock cutting"):
        _build(physical_case)


def test_stronger_diameter_is_accepted_only_with_full_new_40d_and_same_background(physical_case):
    problem, report, raw = deepcopy(physical_case)
    for bars in raw.values():
        for bar in bars:
            bar["diameter_mm"] = 20
    result = _build((problem, report, raw))
    assert result.review["stock_cutting"]["status"] == "pass"
    assert result.packet["expected"]["physical_bar_count"] == 32
    assert all(run["diameter_mm"] == 20 for d in result.packet["directions"] for run in d["runs"])


def test_prescribed_sto_contact_preserved_and_larger_bar_penetration_rejected():
    case = physical_source_case(nominal_step=100)
    result = _build(case)
    assert result.review["source_background_contact_count"] > 0
    assert result.review["background_contact_and_penetration_status"] == "pass"
    problem, report, raw = deepcopy(case)
    for bars in raw.values():
        for bar in bars:
            bar["diameter_mm"] = 20
    with pytest.raises(ValueError, match="background"):
        _build((problem, report, raw))


def test_all_unresolved_intersections_have_explicit_manual_tasks_and_permanent_blocker(physical_case):
    problem, report, merged = deepcopy(physical_case)
    # Undo physical deduplication without losing or duplicating any source refs.
    raw = {}
    for direction, bars in merged.items():
        raw[direction] = []
        for bar in bars:
            for index, owner in enumerate(bar["source_bar_ids"]):
                raw[direction].append({**bar, "id": f"{bar['id']}-owner-{index}", "source_bar_ids": [owner]})
    result = _build((problem, report, raw))
    assert len(result.packet["manual_joint_tasks"]) == 32
    assert result.review["same_plane_conflicts"]["body_intersection_count"] == 32
    assert "manual-joints-unresolved" in result.packet["source_blockers"]
    assert all(t["status"] == "unresolved" for t in result.packet["manual_joint_tasks"])
    assert not result.packet["placement_eligible"]


@pytest.mark.parametrize("kwargs", [{"candidate_index": True}, {"candidate_index": -1},
    {"stock_time_limit_s": 0}, {"stock_time_limit_s": float("inf")}])
def test_invalid_budgets_and_selection_rejected(physical_case, kwargs):
    with pytest.raises(ValueError):
        _build(physical_case, **kwargs)
