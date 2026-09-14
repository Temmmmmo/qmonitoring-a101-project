"""Fresh source restoration, asymmetric endpoint roundtrip, no disguised clipping."""
from copy import deepcopy
from dataclasses import replace
import importlib.util
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from rebar.application.analyze_composite_plate import CompositeDirectionSettings, _direction_candidate, _placements
from rebar.models import Axis, Rebar, ReinforcementRecipe
from rebar.optimization.contracts.composite_coverage import MONOTONE_SINGLE_STO_COVERAGE_POLICY
from rebar.optimization.contracts.composite_search import CompositeSearchProblem
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS, PlateProblem
from rebar.optimization.contracts.problem import DemandCell, DemandLevel, DemandMap, LayoutConstraints, LayoutProblem
from rebar.optimization.services.bar_schedule import build_bar_schedule, composite_schedule_groups
from rebar.optimization.services.composite_detailing import build_composite_zone
from rebar.optimization.services.cutting import PLATE_11700_BATCH_PROFILE, PLATE_11700_CUT_LENGTHS_MM
from rebar.reporting.serialization import to_jsonable


@pytest.fixture
def cli(monkeypatch):
    scripts = Path(__file__).resolve().parents[2]/"scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("asymmetric_shift_experiment", scripts/"experiment_asymmetric_zone_shift.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def source():
    recipe = ReinforcementRecipe(Rebar(300, 10), (Rebar(150, 10),))
    levels = (DemandLevel(0, 1, 0, 1, "background", None, False, replace(recipe, additions=())),
        DemandLevel(1, 2, 1, 2, "addition", recipe.additions[0], True, recipe))
    cell = DemandCell(42, ((1000, 1000), (1300, 1000), (1300, 1300), (1000, 1300)), (1150, 1150), 2, 1)
    constraints = LayoutConstraints(cutting_profile="plate-11700", allowed_cut_lengths_mm=PLATE_11700_CUT_LENGTHS_MM)
    problems, groups, settings, directions = [], [], [], []
    for direction in PLATE_DIRECTIONS:
        demand = DemandMap(direction, levels, (cell,), (1000, 1000, 1300, 1300), source_path=f"{direction}.dxf")
        problems.append(LayoutProblem(demand, constraints))
        setting = CompositeDirectionSettings(direction, 0, 100, 150, "A500", "explicit synthetic test")
        settings.append(setting)
        placement = _placements(demand, setting)
        batch = replace(constraints, cutting_profile=PLATE_11700_BATCH_PROFILE)
        bbox = (1000, 850, 1300, 1450) if direction.axis is Axis.X else (850, 1000, 1450, 1300)
        zone = build_composite_zone(demand, bbox, 1, "zone", dict(placement)[1],
            constraints=batch, installed_lengths_mm=(2925,))
        zone = replace(zone, components=tuple(replace(c,
            longitudinal_interval_mm=tuple(v+50 for v in c.longitudinal_interval_mm)) for c in zone.components))
        groups.append((zone,))
        search = CompositeSearchProblem(demand, placement, batch, MONOTONE_SINGLE_STO_COVERAGE_POLICY)
        directions.append({"direction": to_jsonable(direction), "settings": to_jsonable(setting),
            "source": {"filenames": {"dxf": f"{direction}.dxf"}},
            "candidates": [_direction_candidate(search, (zone,), setting, 0)]})
    schedule = build_bar_schedule(group for zones, setting in zip(groups, settings)
        for group in composite_schedule_groups(zones, steel_class=setting.steel_class))
    point = {"direction_candidate_indexes": [0]*4, "zone_count": 4, "position_count": len(schedule),
        "physical_bar_count": sum(p.physical_bar_count for p in schedule),
        "additional_mass_kg": sum(p.total_mass_kg for p in schedule)}
    report = {"schema_version": "composite-plate-analysis/v1", "units": "mm", "case_id": "synthetic",
        "placement_eligible": False, "source_demand_preserved": True, "averaging": "not_applied",
        "coverage_policy": MONOTONE_SINGLE_STO_COVERAGE_POLICY,
        "blocking_check_ids": ["not-engineering-accepted"], "front": [point], "directions": directions}
    return report, PlateProblem(tuple(problems), case_id="synthetic"), tuple(groups), tuple(settings)


def test_restoration_preserves_asymmetric_ends_in_every_direction(cli, source):
    report, problem, groups, settings = source
    original = deepcopy((report, problem))
    assert cli.restore_zones(report, problem) == (groups, settings)
    assert (report, problem) == original


def test_roundtrip_rebuilds_new_full_coverage_certificates_without_old_endpoints(cli, source):
    report, problem, groups, settings = source
    moved = tuple(tuple(replace(z, components=tuple(replace(c,
        longitudinal_interval_mm=tuple(v+20 for v in c.longitudinal_interval_mm)) for c in z.components))
        for z in zones) for zones in groups)
    rebuilt = cli.rebuild_report(report, problem, moved, settings)
    assert cli.restore_zones(rebuilt, problem) == (moved, settings)
    assert rebuilt["front"][0]["stock_cutting"]["status"] == "pass"
    assert rebuilt["placement_eligible"] is False
    assert report["directions"][0]["candidates"][0]["zone_drafts"] != rebuilt["directions"][0]["candidates"][0]["zone_drafts"]
    assert "not-engineering-accepted" in rebuilt["blocking_check_ids"]


@pytest.mark.parametrize("corruption", ("end", "mass", "cell", "direction"))
def test_source_tampering_is_not_repaired_or_accepted(cli, source, corruption):
    report, problem, _, _ = source
    if corruption == "end":
        report["directions"][0]["candidates"][0]["zone_drafts"][0]["components"][0]["bar_axis_bbox_mm"][0] += 10
    elif corruption == "mass":
        report["front"][0]["additional_mass_kg"] += 1
    elif corruption == "cell":
        p = problem.direction_problems[0]
        c = p.demand.cells[0]
        p = replace(p, demand=replace(p.demand, cells=(replace(c, poly=tuple((x+10000, y) for x, y in c.poly)),)))
        problem = replace(problem, direction_problems=(p, *problem.direction_problems[1:]))
    else:
        report["directions"].reverse()
    with pytest.raises(ValueError):
        cli.restore_zones(report, problem)


@pytest.mark.parametrize("field,value", (("confirm_identity_xy", False),
    ("maximum_transverse_shift_mm", float("nan")), ("maximum_transverse_shift_mm", float("inf")),
    ("maximum_transverse_shift_mm", -1), ("maximum_transverse_shift_mm", 1201),
    ("maximum_candidates_per_zone", 0), ("maximum_candidates_per_zone", True),
    ("maximum_passes", 0), ("maximum_passes", 9)))
def test_cli_rejects_bad_bounds_before_loading_sources(cli, tmp_path, monkeypatch, field, value):
    args = SimpleNamespace(output_dir=tmp_path/"new", confirm_identity_xy=True,
        maximum_transverse_shift_mm=600, maximum_candidates_per_zone=32, maximum_passes=2)
    setattr(args, field, value)
    monkeypatch.setattr(cli, "load_layout_snapshot", lambda *a, **k: pytest.fail("input load must not run"))
    with pytest.raises(ValueError):
        cli.run(args)
    assert not args.output_dir.exists()


def test_cli_never_overwrites_previous_output(cli, tmp_path):
    with pytest.raises(ValueError, match="NEW"):
        cli.run(SimpleNamespace(output_dir=tmp_path))


@pytest.mark.parametrize("corruption", (None, "source", "normal", "packet", "review", "dict_bytes", "missing_packet"))
def test_original_pipeline_byte_chain_is_checked_before_reusing_source(cli, corruption):
    source = {"source": "fixture"}
    source_bytes = cli._bytes(source)
    normal = {"source_report_sha256": hashlib.sha256(source_bytes).hexdigest()}
    normal_bytes = cli._bytes(normal)
    packet = {"source_report_sha256": normal["source_report_sha256"],
        "raw_report_sha256": hashlib.sha256(normal_bytes).hexdigest()}
    packet_bytes = cli._bytes(packet)
    review = {"packet_sha256": hashlib.sha256(packet_bytes).hexdigest()}
    recovery = SimpleNamespace(patterned_report=source, patterned_report_bytes=source_bytes,
        normalization_report=normal, normalization_report_bytes=normal_bytes,
        packet=packet, packet_bytes=packet_bytes, review=review, review_bytes=cli._bytes(review))
    if corruption is None:
        cli.verify_recovery_bindings(recovery)
        return
    if corruption == "source":
        recovery.patterned_report = {"source": "changed"}
        recovery.patterned_report_bytes = cli._bytes(recovery.patterned_report)
    elif corruption == "normal":
        recovery.normalization_report["unrecorded_change"] = True
        recovery.normalization_report_bytes = cli._bytes(recovery.normalization_report)
    elif corruption == "packet":
        recovery.packet["unrecorded_change"] = True
        recovery.packet_bytes = cli._bytes(recovery.packet)
    elif corruption == "review":
        recovery.review["packet_sha256"] = "0"*64
        recovery.review_bytes = cli._bytes(recovery.review)
    elif corruption == "dict_bytes":
        recovery.patterned_report["unrecorded_change"] = True
    else:
        recovery.packet = None
    with pytest.raises(ValueError):
        cli.verify_recovery_bindings(recovery)


@pytest.mark.parametrize("corruption", (None, "config", "xy", "host", "metrics"))
def test_recorded_host_policy_and_comparison_match_original_pipeline(cli, corruption):
    summary = {"normalization_config": {"allow_diameter_increase": True},
        "expected": {"physical_bar_count": 902}}
    recovery = SimpleNamespace(normalization_report={"configuration": deepcopy(summary["normalization_config"]),
        "host_fit": {"source_to_revit_xy_mm": [0, 0], "source_host_report_sha256": "a"*64}},
        packet={"expected": deepcopy(summary["expected"])})
    if corruption is None:
        cli.verify_recorded_policies(summary, recovery, "a"*64)
        return
    if corruption == "config":
        summary["normalization_config"]["allow_diameter_increase"] = False
    elif corruption == "xy":
        recovery.normalization_report["host_fit"]["source_to_revit_xy_mm"] = [100, 0]
    elif corruption == "host":
        recovery.normalization_report["host_fit"]["source_host_report_sha256"] = "b"*64
    else:
        summary["expected"]["physical_bar_count"] = 901
    with pytest.raises(ValueError):
        cli.verify_recorded_policies(summary, recovery, "a"*64)
