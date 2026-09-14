"""Real uniform/STO host guards and isolated CLI provenance/post-stock checks."""
from collections import Counter
from copy import deepcopy
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from shapely.affinity import affine_transform
from shapely.geometry import box

from rebar.application.analyze_composite_plate import CompositeDirectionSettings, _placements
from rebar.application.physical_layout_recovery import _bytes
from rebar.models import Axis, Direction, Layer, Rebar, ReinforcementRecipe
from rebar.optimization.contracts import LayoutSolution, SolutionStatus
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS, PlateProblem
from rebar.optimization.contracts.problem import DemandCell, DemandLevel, DemandMap, LayoutProblem
from rebar.optimization.services.composite_detailing import build_composite_zone
from rebar.optimization.services.composite_windows import covering_composite_window
from rebar.optimization.services.detailing import build_zone
from rebar.optimization.services.evaluation import evaluate_layout
from rebar.optimization.services.host_search_domain import uniform_zone_host_check
from rebar.optimization.services.solid_host import OrthogonalSolidHost, SolidHostSection


@pytest.fixture
def cli(monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "host_constrained_genetic_experiment_test", scripts / "experiment_host_constrained_genetic.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def geometry_case(direction=Direction(Layer.TOP, Axis.X), step=150, hole_axis=None):
    polygon = box(1000, 0, 2000, 300)
    material = box(0, -1000, 4000, 1500)
    if hole_axis is not None:
        material = material.difference(box(1450, hole_axis-5, 1550, hole_axis+5))
    if direction.axis is Axis.Y:
        polygon = affine_transform(polygon, (0, 1, 1, 0, 0, 0))
        material = affine_transform(material, (0, 1, 1, 0, 0, 0))
    recipe = ReinforcementRecipe(Rebar(300, 10), (Rebar(step, 10),))
    levels = (DemandLevel(0, 1, 0, 1, "background", None, False, replace(recipe, additions=())),
        DemandLevel(1, 2, 1, 2, "addition", recipe.additions[0], True, recipe))
    cell = DemandCell(42, tuple(polygon.exterior.coords)[:-1],
        (polygon.centroid.x, polygon.centroid.y), 2, 1)
    problem = LayoutProblem(DemandMap(direction, levels, (cell,), polygon.bounds))
    zone = build_zone(problem, (42,), 1, "zone")
    setting = CompositeDirectionSettings(direction, 0, 100, 150, "A500", "synthetic explicit profile")
    placement = dict(_placements(problem.demand, setting))[1]
    window = covering_composite_window(problem.demand, zone.demand_bbox, 1, placement,
        constraints=problem.constraints)
    patterned = build_composite_zone(problem.demand, window, 1, zone.id, placement,
        constraints=problem.constraints)
    host = OrthogonalSolidHost((SolidHostSection(0, 200, material),),
        25, 25, 25, material.area*200, 6)
    return problem, zone, setting, patterned, host


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
@pytest.mark.parametrize("step", (100, 150))
def test_guard_checks_actual_sto_axes_not_a_uniform_nominal_step(cli, axis, step):
    problem, zone, setting, patterned, host = geometry_case(Direction(Layer.TOP, axis), step, 100)
    original = deepcopy((problem, zone, setting))
    assert uniform_zone_host_check(problem, zone, host)["valid"]
    bars = cli.patterned_bars((patterned,), setting.steel_class)
    coordinates = tuple(bar.transverse_axis_mm for bar in bars)
    assert coordinates == ((-100, -10, 100, 200, 290, 400) if step == 100 else (-100, 100, 200, 400))
    assert 100 in coordinates
    guard, stats = cli.make_guard(problem, host, setting)
    assert guard(problem, zone) is False
    assert stats["uniform_host_rejected"] == 0
    assert stats["pattern_host_or_geometry_rejected"] == 1
    assert (problem, zone, setting) == original


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_guard_explicitly_requires_both_uniform_and_sto_host_fit(cli, axis):
    problem, zone, setting, patterned, host = geometry_case(Direction(Layer.TOP, axis), 150, 0)
    assert not uniform_zone_host_check(problem, zone, host)["valid"]
    material, _, _ = cli.material_and_holes(host)
    assert all(cli.contained(bar, material, host.side_cover_mm)
        for bar in cli.patterned_bars((patterned,), setting.steel_class))
    guard, stats = cli.make_guard(problem, host, setting)
    assert guard(problem, zone) is False
    assert stats["uniform_host_rejected"] == 1
    # This is a conservative candidate restriction, not proof that the STO plan cannot fit.
    assert stats["pattern_host_or_geometry_rejected"] == 0


def test_guard_binds_original_problem_and_caches_only_pattern_conversion(cli, monkeypatch):
    problem, zone, setting, _, host = geometry_case()
    actual = cli.build_composite_zone
    builds = []

    def record(*args, **kwargs):
        builds.append(args)
        return actual(*args, **kwargs)

    monkeypatch.setattr(cli, "build_composite_zone", record)
    guard, stats = cli.make_guard(problem, host, setting)
    assert guard(problem, zone) is True
    assert guard(problem, replace(zone, id="same-source-another-id")) is True
    assert len(builds) == 1 and stats["calls"] == 2
    with pytest.raises(ValueError, match="exact original problem"):
        guard(replace(problem), zone)
    # A cached STO result must never hide malformed new uniform geometry.
    with pytest.raises(ValueError):
        guard(problem, replace(zone, installed_length_mm=float("nan")))


def test_unconstructable_pattern_is_rejected_not_clipped_or_returned_as_valid(cli, monkeypatch):
    problem, zone, setting, _, host = geometry_case()

    def unsupported(*args, **kwargs):
        raise ValueError("finite catalogue cannot contain the full proposal")

    monkeypatch.setattr(cli, "covering_composite_window", unsupported)
    guard, stats = cli.make_guard(problem, host, setting)
    assert guard(problem, zone) is False
    assert guard(problem, zone) is False
    assert stats["pattern_geometry_rejected"] == 1
    assert stats["pattern_host_or_geometry_rejected"] == 2


@pytest.fixture
def run_case(cli, tmp_path, monkeypatch):
    args = SimpleNamespace(pipeline_dir=tmp_path / "pipeline", snapshot=tmp_path / "snapshot.json",
        working_host_report=tmp_path / "host.json", output=tmp_path / "new" / "report.json",
        candidate_id="plate:fixture", confirm_identity_xy=True, control_unbounded=False,
        population=4, generations=1, seed=7, maximum_pool_merges=16,
        recombination_variants=0, time_limit_s=1)
    args.snapshot.write_bytes(b"immutable source snapshot")
    args.working_host_report.write_bytes(_bytes({"host": "fixture"}))
    dxf = tmp_path / "original.dxf"
    dxf.write_bytes(b"original complete demand")
    data = tuple(geometry_case(d) for d in PLATE_DIRECTIONS)
    problem = PlateProblem(tuple(row[0] for row in data), case_id="fixture")
    settings = tuple(row[2] for row in data)
    groups = tuple((row[3],) for row in data)
    # A common large host contains both X and Y plans before any simulated stock extension.
    footprint = box(-1000, -1000, 5000, 5000)
    host = OrthogonalSolidHost((SolidHostSection(0, 200, footprint),),
        25, 25, 25, footprint.area*200, 6)
    loaded = SimpleNamespace(problem=problem, snapshot={"candidate_id": args.candidate_id},
        source_sha256=hashlib.sha256(args.snapshot.read_bytes()).hexdigest(),
        engineer_comparison={"mass_kg": 20, "physical_bar_count": 16})
    recovery = SimpleNamespace(patterned_report={"directions": [
        {"settings": cli.to_jsonable(setting)} for setting in settings]})
    summary = {"working_host_source": {"sha256": hashlib.sha256(args.working_host_report.read_bytes()).hexdigest()}}
    records = [cli.source_record(dxf, role="dxf")]
    point = {"additional_mass_kg": 20, "physical_bar_count": 16}
    rebuilt = SimpleNamespace(direction_zones=groups, stock_balanced=True,
        report={"front": [point], "diagnostic_front_before_cutting": []})
    outcomes = {}
    for current, zone, *_ in data:
        metrics = evaluate_layout(current, (zone,)).metrics
        outcomes[current.demand.direction] = (LayoutSolution("fixture", SolutionStatus.FEASIBLE, (zone,), metrics),)
    state = SimpleNamespace(args=args, problem=problem, settings=settings, host=host, loaded=loaded,
        recovery=recovery, summary=summary, records=records, rebuilt=rebuilt, outcomes=outcomes,
        calls=[], guards=[], dxf=dxf, on_solve=None)

    class FakeOptimizer:
        def __init__(self, *, candidate_guard):
            state.guards.append(candidate_guard)

        def solve_many(self, actual_problem, request):
            assert actual_problem is problem.problem(actual_problem.demand.direction)
            state.calls.append(("solve", actual_problem.demand.direction, request))
            if state.on_solve:
                state.on_solve()
            return state.outcomes[actual_problem.demand.direction]

    def mock_guard(actual_problem, actual_host, setting):
        assert actual_host is host and actual_problem.demand.direction == setting.direction
        return lambda p, z: True, Counter({"fixture": 1})

    def rebuild(actual_problem, plate, actual_settings):
        assert actual_problem is problem and actual_settings == settings
        assert plate.valid and plate.metrics.direction_count == 4
        state.calls.append(("recover_after_all_four",))
        return state.rebuilt

    monkeypatch.setattr(cli, "_code_digest", lambda: "a"*64)
    monkeypatch.setattr(cli, "load_layout_snapshot", lambda path, **kw: loaded)
    monkeypatch.setattr(cli, "_load_recovery", lambda *a: (recovery, summary, list(records)))
    monkeypatch.setattr(cli, "load_working_host_json", lambda content, **kw: json.loads(content))
    monkeypatch.setattr(cli, "inspect_working_solid", lambda report: (host, {}))
    monkeypatch.setattr(cli, "optimistic_host_reachability", lambda *a: {"status": "not_ruled_out"})
    monkeypatch.setattr(cli, "make_guard", mock_guard)
    monkeypatch.setattr(cli, "GeneticParetoOptimizer", FakeOptimizer)
    monkeypatch.setattr(cli, "recover_patterned_layout", rebuild)
    return state


@pytest.mark.parametrize("control", (False, True))
def test_run_retains_all_four_directions_full_demand_and_explicit_unbounded_control(cli, run_case, control):
    run_case.args.control_unbounded = control
    original = deepcopy(run_case.problem)
    report = cli.run(run_case.args)
    assert report["status"] == "complete_source_candidate_physically_rechecked"
    assert report["physical_result"]["full_physical_checks_passed"] is True
    assert [row["direction"] for row in report["directions"]] == list(map(str, PLATE_DIRECTIONS))
    assert len(run_case.guards) == 4
    assert all(guard is None if control else callable(guard) for guard in run_case.guards)
    assert run_case.calls[-1] == ("recover_after_all_four",)
    assert run_case.problem == original
    assert report["control_unbounded"] is control
    for flag in ("placement_eligible", "engineering_approval", "structural_placement_supported",
                 "source_demand_removed", "source_demand_values_changed"):
        assert report[flag] is False
    assert report["source_to_revit_xy_mm"] == [0, 0]
    assert "CONTROL APPROXIMATION" in report["warning"]
    assert "actual_Z_3D_existing_rebar" in report["not_checked"]
    assert run_case.args.output.read_bytes() == _bytes(report)
    for record in report["source_files"]:
        assert record == cli.source_record(record["path"], role=record["role"])


def test_host_is_rechecked_on_actual_stock_lengthened_bars_not_only_guarded_source(cli, run_case):
    before = run_case.rebuilt.direction_zones[0][0]
    component = before.components[0]
    longer = replace(component, longitudinal_interval_mm=(-1500, 5500), installed_length_mm=7000)
    run_case.rebuilt.direction_zones = ((replace(before, components=(longer,)),),
        *run_case.rebuilt.direction_zones[1:])
    report = cli.run(run_case.args)
    result = report["physical_result"]
    assert result["host_blocked_bar_count"] == component.bar_count
    assert result["full_physical_checks_passed"] is False


def test_same_direction_collisions_after_recovery_block_the_physical_result(cli, run_case):
    first = run_case.rebuilt.direction_zones[0][0]
    run_case.rebuilt.direction_zones = ((first, replace(first, id="second-zone")),
        *run_case.rebuilt.direction_zones[1:])
    result = cli.run(run_case.args)["physical_result"]
    assert result["same_direction_body_pairs"] == first.components[0].bar_count
    assert result["full_physical_checks_passed"] is False


def test_stock_failure_is_not_approved_despite_host_and_collision_pass(cli, run_case):
    run_case.rebuilt.stock_balanced = False
    run_case.rebuilt.report["diagnostic_front_before_cutting"] = run_case.rebuilt.report["front"]
    run_case.rebuilt.report["front"] = []
    result = cli.run(run_case.args)["physical_result"]
    assert result["host_blocked_bar_count"] == result["same_direction_body_pairs"] == 0
    assert result["full_physical_checks_passed"] is False


@pytest.mark.parametrize("failure", ("empty", "infeasible", "uncovered"))
def test_incomplete_direction_is_not_exported_or_compared_as_a_complete_plate(cli, run_case, failure):
    direction = PLATE_DIRECTIONS[-1]
    chosen = run_case.outcomes[direction][0]
    run_case.outcomes[direction] = (() if failure == "empty" else
        (replace(chosen, status=SolutionStatus.INFEASIBLE),) if failure == "infeasible" else
        (replace(chosen, metrics=replace(chosen.metrics, under_reinforced_cell_count=1)),))
    report = cli.run(run_case.args)
    assert report["status"] == "no_complete_candidate" and report["physical_result"] is None
    assert len(report["directions"]) == 4
    assert report["directions"][-1]["full_source_solution_found"] is False
    assert all(call[0] != "recover_after_all_four" for call in run_case.calls)


@pytest.mark.parametrize(("field", "value"), (("confirm_identity_xy", False), ("confirm_identity_xy", 1),
    ("time_limit_s", float("nan")), ("time_limit_s", float("inf")), ("time_limit_s", 0),
    ("time_limit_s", 601), ("maximum_pool_merges", 0), ("maximum_pool_merges", 5001),
    ("recombination_variants", -1), ("recombination_variants", 5001)))
def test_explicit_coordinate_confirmation_and_resource_bounds_fail_closed(cli, run_case, field, value):
    setattr(run_case.args, field, value)
    with pytest.raises(ValueError):
        cli.run(run_case.args)
    assert not run_case.args.output.parent.exists() and run_case.calls == []


def test_existing_output_is_preserved_before_any_source_or_search_work(cli, run_case):
    run_case.args.output.parent.mkdir()
    run_case.args.output.write_bytes(b"previous user report")
    with pytest.raises(ValueError, match="NEW file"):
        cli.run(run_case.args)
    assert run_case.args.output.read_bytes() == b"previous user report" and run_case.calls == []


def test_pipeline_must_be_bound_to_exact_working_host_sha(cli, run_case):
    run_case.summary["working_host_source"]["sha256"] = "0"*64
    with pytest.raises(ValueError, match="Host differs"):
        cli.run(run_case.args)
    assert not run_case.args.output.parent.exists()


def test_pattern_settings_cannot_be_silently_assigned_to_another_direction(cli, run_case):
    rows = run_case.recovery.patterned_report["directions"]
    rows[0], rows[1] = rows[1], rows[0]
    with pytest.raises(ValueError, match="settings order differs"):
        cli.run(run_case.args)
    assert not run_case.args.output.parent.exists() and run_case.calls == []


@pytest.mark.parametrize("target", ("snapshot", "working_host_report", "dxf"))
def test_changed_inputs_during_ga_prevent_all_output(cli, run_case, target):
    path = run_case.dxf if target == "dxf" else getattr(run_case.args, target)
    run_case.on_solve = lambda: path.write_bytes(b"changed during GA")
    with pytest.raises(ValueError, match="Source changed"):
        cli.run(run_case.args)
    assert not run_case.args.output.parent.exists()


def test_snapshot_change_after_restoration_is_rejected_before_search(cli, run_case):
    run_case.args.snapshot.write_bytes(b"different restored bytes")
    with pytest.raises(ValueError, match="Snapshot changed"):
        cli.run(run_case.args)
    assert not run_case.args.output.parent.exists() and run_case.calls == []


def test_core_change_during_ga_prevents_all_output(cli, run_case, monkeypatch):
    digests = iter(("a"*64, "b"*64))
    monkeypatch.setattr(cli, "_code_digest", lambda: next(digests))
    with pytest.raises(ValueError, match="Code changed"):
        cli.run(run_case.args)
    assert not run_case.args.output.parent.exists()


def test_exclusive_output_write_protects_against_a_late_second_writer(cli, run_case, monkeypatch):
    actual = cli.verify_source_records

    def other_writer(records):
        actual(records)
        run_case.args.output.parent.mkdir()
        run_case.args.output.write_bytes(b"another writer")

    monkeypatch.setattr(cli, "verify_source_records", other_writer)
    with pytest.raises(FileExistsError):
        cli.run(run_case.args)
    assert run_case.args.output.read_bytes() == b"another writer"


@pytest.mark.parametrize(("physical", "expected"), ((None, 1),
    ({"full_physical_checks_passed": False}, 1), ({"full_physical_checks_passed": True}, 0)))
def test_cli_exit_code_distinguishes_checked_result_from_missing_or_blocked(cli, tmp_path, monkeypatch, physical, expected):
    monkeypatch.setattr(cli, "run", lambda args: {"physical_result": physical})
    assert cli.main(["--pipeline-dir", str(tmp_path), "--snapshot", str(tmp_path / "source.json"),
        "--candidate-id", "plate:1", "--working-host-report", str(tmp_path / "host.json"),
        "--output", str(tmp_path / "new.json"), "--confirm-identity-xy"]) == expected
