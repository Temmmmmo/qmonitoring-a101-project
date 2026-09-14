"""Full typed pattern recovery, strict snapshot CLI, limits and rollback-only output."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path

import pytest

from rebar import Band, Cell, Mosaic
from rebar.application.analyze_composite_plate import CompositeDirectionSettings
from rebar.application.patterned_layout_recovery import recover_patterned_layout
from rebar.application.plate_revit_trial import build_full_plate_trial
from rebar.legend import parse_recipe
from rebar.optimization import (
    PLATE_DIRECTIONS, DemandLevel, LayoutConstraints, LayoutSolution, PlateDirectionSolution,
    SolutionStatus, StrongestBBoxOptimizer, build_layout_problem, build_plate_problem,
    build_plate_solution, build_zone_from_bbox, evaluate_layout,
)
from rebar.optimization.algorithms.stock_length_balance import StockLengthBalanceResult
from rebar.optimization.services.cutting import PLATE_11700_CUT_LENGTHS_MM


def _source():
    bands = []
    for index, label in enumerate(("s300d18", "s300d18+s150d18")):
        recipe = parse_recipe(label)
        bands.append(Band(index, index + 1, label, 10 + 20 * index, recipe.background,
                          recipe.additions[0] if recipe.additions else None, recipe))
    cells = [Cell([(x, y), (x + 600, y), (x + 600, y + 600), (x, y + 600)],
                  (x + 300, y + 300), 2, bands[1]) for x in (0, 600) for y in (0, 600)]
    constraints = LayoutConstraints(allowed_cut_lengths_mm=PLATE_11700_CUT_LENGTHS_MM,
                                     cutting_profile="plate-11700")
    problems = tuple(build_layout_problem(Mosaic(direction, deepcopy(cells), deepcopy(bands),
        (0, 0, 1200, 1200), source_path=f"{direction}.dxf"), constraints) for direction in PLATE_DIRECTIONS)
    problem = build_plate_problem(problems, case_id="synthetic-only")
    solution = build_plate_solution(PlateDirectionSolution(p.demand.direction, StrongestBBoxOptimizer().solve(p))
                                    for p in problems)
    configs = tuple(CompositeDirectionSettings(direction, 0, 100, 0, "A500", "synthetic-only explicit test", "left")
                    for direction in PLATE_DIRECTIONS)
    return problem, solution, configs


def test_complete_patterns_batch_report_and_existing_packet_preserve_every_direction():
    problem, solution, configs = _source()
    before = deepcopy((problem, solution, configs))
    result = recover_patterned_layout(problem, solution, configs, balance_time_limit_s=5)
    assert result.stock_balanced
    report = result.report
    assert report["source_demand_preserved"] and not report["placement_eligible"]
    assert len(result.direction_zones) == len(report["directions"]) == 4
    assert report["runtime_ms"] > 0
    assert report["front"][0]["physical_bar_count"] == 32
    assert report["front"][0]["stock_cutting"]["status"] == "pass"
    assert report["front"][0]["position_count"] == 1
    assert report["same_plane_conflicts"]["body_intersection_count"] == 0
    assert report["same_plane_conflicts"]["actual_3d_checked"] is False
    assert report["physical_placement_status"] == "same_plane_check_clear_host_and_3d_not_checked"
    assert all(row["source_cell_count"] == 4 for row in report["directions"])
    assert all(row["candidates"][-1]["coverage"]["uncovered_cell_count"] == 0 for row in report["directions"])
    packet = build_full_plate_trial(report, 0, "a" * 64)
    assert packet["expected"]["physical_bar_count"] == 32
    assert len(packet["directions"]) == 4
    assert packet["mode"] == "commit-readback-rollback"
    assert not packet["placement_eligible"]
    # Eight bars per direction now arise from explicit 100/200, not uniform @150.
    assert {run["spacing_mm"] for row in packet["directions"] for run in row["runs"]} == {300}
    assert all(len(row["runs"]) == 2 for row in packet["directions"])
    assert (problem, solution, configs) == before


def test_coverage_and_stock_pass_do_not_hide_same_plane_intersections():
    problem, solution, configs = _source()
    doubled = []
    for original, item in zip(problem.direction_problems, solution.direction_solutions):
        zones = (*item.solution.zones, replace(item.solution.zones[0], id="overlapping-copy"))
        check = evaluate_layout(original, zones, item.solution.request)
        assert check.valid
        doubled.append(replace(item, solution=replace(item.solution, zones=zones, metrics=check.metrics)))
    result = recover_patterned_layout(problem, build_plate_solution(doubled), configs, balance_time_limit_s=5)
    assert result.stock_balanced
    report = result.report
    assert report["front"][0]["stock_cutting"]["status"] == "pass"
    assert report["same_plane_conflicts"]["body_intersection_count"] > 0
    assert report["physical_placement_status"] == "blocked_same_plane_intersections"
    assert "same-plane-additional-bar-intersections" in report["blocking_check_ids"]
    assert "same-plane-additional-bar-intersections" in build_full_plate_trial(report, 0, "a" * 64)["source_blockers"]
    assert report["placement_eligible"] is False


@pytest.mark.parametrize("kwargs", [
    {"maximum_patches_per_direction": -1}, {"maximum_patches_per_direction": 129},
    {"maximum_zones_per_direction": 0}, {"maximum_zones_per_direction": 129},
    {"maximum_batch_mass_increase_pct": 5.001}, {"maximum_batch_mass_increase_pct": float("nan")},
    {"balance_time_limit_s": 0}, {"balance_time_limit_s": 61},
])
def test_limits_are_explicit_and_never_silently_relaxed(kwargs):
    with pytest.raises(ValueError):
        recover_patterned_layout(*_source(), **kwargs)


def test_missing_explicit_direction_or_steel_class_rejected():
    problem, solution, settings = _source()
    with pytest.raises(ValueError, match="четырёх"):
        recover_patterned_layout(problem, solution, settings[:3])
    with pytest.raises(ValueError, match="steel class"):
        recover_patterned_layout(problem, solution, (replace(settings[0], steel_class=""), *settings[1:]))


def test_different_source_mesh_is_not_treated_as_one_plate():
    problem, solution, settings = _source()
    first = problem.direction_problems[0]
    cell = first.demand.cells[0]
    changed = replace(first, demand=replace(first.demand, cells=(
        replace(cell, poly=tuple((x + 100, y) for x, y in cell.poly)), *first.demand.cells[1:],
    )))
    problem = replace(problem, direction_problems=(changed, *problem.direction_problems[1:]))
    with pytest.raises(ValueError, match="геометрия"):
        recover_patterned_layout(problem, solution, settings)


def test_no_balance_produces_no_accepted_front_or_partial_trial(monkeypatch):
    module = importlib.import_module("rebar.application.patterned_layout_recovery")
    monkeypatch.setattr(module, "balance_stock_lengths", lambda *_args, **_kwargs:
                        StockLengthBalanceResult("not_checked", (), 1.0, None, {"reason": "time_limit"}))
    result = recover_patterned_layout(*_source())
    assert not result.stock_balanced
    assert result.report["front"] == []
    assert result.report["selected_index"] is None
    assert result.report["status"] == "stock_balance_not_found"
    assert len(result.report["directions"]) == 4
    assert result.report["diagnostic_front_before_cutting"][0]["physical_bar_count"] == 32
    assert "stock-cutting-zero-waste" in result.report["blocking_check_ids"]


def _coarser_source():
    problem, _solution, configs = _source()
    recipe = parse_recipe("s300d18+s300d25")
    level = DemandLevel(2, 3, 30, 50, "s300d18+s300d25", recipe.additions[0], True, recipe)
    problems, solutions = [], []
    for original in problem.direction_problems:
        original = replace(original, demand=replace(original.demand, levels=(*original.demand.levels, level)))
        zone = build_zone_from_bbox(original, original.demand.bbox, 2, "coarser-higher-level")
        check = evaluate_layout(original, (zone,))
        assert check.valid  # Old level-order model accepts it; monotone density must not.
        problems.append(original)
        solutions.append(PlateDirectionSolution(original.demand.direction,
            LayoutSolution("test", SolutionStatus.FEASIBLE, (zone,), check.metrics)))
    return build_plate_problem(problems), build_plate_solution(solutions), configs


def test_coarser_level_requires_exact_original_recipe_patches_not_as_tradeoff(monkeypatch):
    module = importlib.import_module("rebar.application.patterned_layout_recovery")
    monkeypatch.setattr(module, "balance_stock_lengths", lambda *_args, **_kwargs:
                        StockLengthBalanceResult("not_checked", (), 1.0, None, {"reason": "controlled"}))
    result = recover_patterned_layout(*_coarser_source())
    for row, zones in zip(result.report["directions"], result.direction_zones):
        option = row["candidates"][0]
        assert option["source_recovery"]["coverage_before_exact_patches"]["uncovered_cell_count"] == 4
        assert option["coverage"]["uncovered_cell_count"] == 0
        assert len(option["source_recovery"]["residual_patches"]) == 4
        assert all(zone.recipe == parse_recipe("s300d18+s150d18") for zone in zones[1:])
    assert not result.stock_balanced  # Coverage success is not fabricated stock success.


def test_patch_cap_rejects_complete_conversion_instead_of_discarding_residual_cells():
    with pytest.raises(ValueError, match="4 residual cells retained"):
        recover_patterned_layout(*_coarser_source(), maximum_patches_per_direction=3)


def _cli():
    path = Path(__file__).resolve().parents[2] / "scripts/recover_patterned_layout.py"
    spec = importlib.util.spec_from_file_location("pattern_recovery_cli_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mock_loaded(tmp_path, monkeypatch, module):
    problem, solution, _ = _source()
    path = tmp_path / "source.json"
    content = b"synthetic snapshot boundary"
    path.write_bytes(content)
    from rebar.application.layout_snapshot import LoadedLayoutSnapshot
    loaded = LoadedLayoutSnapshot(problem, solution, hashlib.sha256(content).hexdigest(),
        {"candidate_id": "plate:1", "source_dxf": [], "source_pdf": {}, "path": str(path)},
        {"mass_kg": 1000, "physical_bar_count": 40, "specification_rows": 5})
    monkeypatch.setattr(module, "load_layout_snapshot", lambda *_args, **_kwargs: loaded)
    monkeypatch.setattr(module, "_source_record", lambda *_args: None)
    return path


def _export(module, snapshot, output):
    return module.export_patterned_snapshot(snapshot, output, candidate_id="plate:1",
        background_origin_mm=0, first_300_offset_mm=100, contact_side="left",
        steel_class="A500", phase_source="synthetic test profile, not engineering acceptance", balance_time_limit_s=5)


def test_cli_writes_hash_bound_report_review_packet_and_rejects_existing_target(tmp_path, monkeypatch):
    module = _cli()
    snapshot = _mock_loaded(tmp_path, monkeypatch, module)
    output = tmp_path / "new-output"
    result = _export(module, snapshot, output)
    packet = json.loads((output / "full-plate-trial.json").read_text())
    review = json.loads((output / "engineer-review.json").read_text())
    actual_hash = hashlib.sha256((output / "patterned-analysis.json").read_bytes()).hexdigest()
    assert actual_hash == packet["source_report_sha256"] == review["source_report_sha256"]
    assert packet["expected"] == review["expected"]
    assert "prior_metrics" in review and "source_metrics" not in review
    assert result["physical_bar_count"] == 32
    assert not review["placement_eligible"]
    content = (output / "full-plate-trial.json").read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        _export(module, snapshot, output)
    assert (output / "full-plate-trial.json").read_bytes() == content


def test_cli_rejects_code_mutation_before_writing_any_output(tmp_path, monkeypatch):
    module = _cli()
    snapshot = _mock_loaded(tmp_path, monkeypatch, module)
    digests = iter(("before", "after"))
    monkeypatch.setattr(module, "_code_digest", lambda: next(digests))
    output = tmp_path / "not-created"
    with pytest.raises(ValueError, match="Core code changed"):
        _export(module, snapshot, output)
    assert not output.exists()
