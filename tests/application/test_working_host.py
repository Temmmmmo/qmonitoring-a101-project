from copy import deepcopy
import hashlib
import json
import math

import pytest

from rebar.application.working_host import (
    HOST_TRANSLATION_POLICY, host_from_working_input, load_working_host_json, prepare_working_host,
)
from test_composite_host_review import reference_sample


def trial_snapshot(openings=True):
    """Independent extrusion fixture: top/bottom plus one wall per contour edge."""
    floor = reference_sample(openings=openings)["floor"]
    faces = deepcopy(floor["top_faces"] + floor["bottom_faces"])
    for loop in floor["top_faces"][0]["edge_loops"]:
        x_values = [e["start_mm"][0] for e in loop]
        hole = min(x_values) > floor["bbox_mm"]["min_mm"][0]
        for edge in loop:
            a, b = edge["start_mm"], edge["end_mm"]
            dx, dy = b[0]-a[0], b[1]-a[1]
            length = math.hypot(dx, dy)
            sign = -1 if hole else 1
            normal = [sign * dy/length, -sign * dx/length, 0]
            points = [[a[0], a[1], -300], [b[0], b[1], -300], [b[0], b[1], 0], [a[0], a[1], 0]]
            sides = [{"kind": "Line", "start_mm": p, "end_mm": q, "length_mm": math.dist(p, q)}
                     for p, q in zip(points, points[1:] + points[:1])]
            faces.append({"plane": {"origin_mm": points[0], "normal": normal}, "edge_loops": [sides]})
    return {"schema_version": "revit-full-plate-trial-report/v1", "units": "mm",
        "coordinate_system": "revit-internal-origin-and-axes", "placement_eligible": False,
        "status": "blocked_setup", "read_issues": [], "issues": [{"stage": "setup", "message": "Cancelled types"}],
        "host": floor, "host_solid": {"faces": faces, "volume_mm3": (12000**2 - (1000**2 if openings else 0)) * 300}}


@pytest.mark.parametrize("openings", [False, True])
def test_working_snapshot_translates_host_not_demand_and_preserves_source(openings):
    report = trial_snapshot(openings)
    before = deepcopy(report)
    digest = hashlib.sha256(json.dumps(report).encode()).hexdigest()
    prepared = prepare_working_host(report, offset_x_mm=1234, offset_y_mm=-4321, source_report_sha256=digest)
    host = host_from_working_input(prepared)
    assert host.outer_mm == (-3234, 2321, 8766, 14321)
    assert host.openings_mm == (((3766, 9321, 4766, 10321),) if openings else ())
    assert host.top_z_mm == 0 and host.bottom_z_mm == -300
    assert host.side_cover_mm == 25 and report == before
    assert prepared["coordinate_policy"] == HOST_TRANSLATION_POLICY
    assert not prepared["placement_eligible"] and digest in host.source
    prepared["snapshot"]["host_solid"]["volume_mm3"] += 10000
    with pytest.raises(ValueError, match="Объём"):
        host_from_working_input(prepared)
    assert report == before


@pytest.mark.parametrize("bad", ["pending", "restoration", "read_error", "units", "extra_face", "missing_wall",
                                 "duplicate_wall", "normal", "curve", "hole_depth", "volume", "nan", "offset", "hash"])
def test_working_host_rejects_unconfirmed_or_unsupported_geometry(bad):
    report = trial_snapshot()
    kwargs = {"offset_x_mm": 0, "offset_y_mm": 0, "source_report_sha256": "b"*64}
    face = report["host_solid"]["faces"][-1]
    if bad == "pending":
        report["status"] = "rollback_unconfirmed"
    elif bad == "restoration":
        report["status"] = "passed_rolled_back"
    elif bad == "read_error":
        report["read_issues"] = ["missing_cover"]
    elif bad == "units":
        report["units"] = "feet"
    elif bad == "extra_face":
        report["host_solid"]["faces"].append(deepcopy(face))
    elif bad == "missing_wall":
        report["host_solid"]["faces"].pop()
    elif bad == "duplicate_wall":
        report["host_solid"]["faces"][-2] = deepcopy(face)
    elif bad == "normal":
        face["plane"]["normal"] = [0.1, 0, 1]
    elif bad == "curve":
        face["edge_loops"][0][0]["kind"] = "Arc"
    elif bad == "hole_depth":
        face["edge_loops"][0][0]["start_mm"][2] += 10
    elif bad == "volume":
        report["host_solid"]["volume_mm3"] *= 0.9
    elif bad == "nan":
        report["host_solid"]["volume_mm3"] = float("nan")
    elif bad == "offset":
        kwargs["offset_x_mm"] = True
    else:
        kwargs["source_report_sha256"] = "guessed"
    with pytest.raises(ValueError):
        prepare_working_host(report, **kwargs)


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b'[]', b'', b'\xff', b' ' * (8*1024*1024+1)])
def test_host_loader_strict_and_bounded(raw):
    with pytest.raises(ValueError):
        load_working_host_json(raw)


def test_loader_accepts_full_readback_report_larger_than_old_256kib_limit():
    raw = json.dumps({**trial_snapshot(), "readback": "x" * 300000}).encode()
    assert load_working_host_json(raw)["host"]["element_id"] == 42


def test_prepared_host_is_consumed_by_full_four_direction_analysis(composite_plate_sources):
    import ezdxf
    from rebar.application.analyze_composite_plate import analyze_composite_plate
    from test_analyze_composite_plate import settings
    # One required recipe: test host wiring, not finite-pool partition quality.
    for source in composite_plate_sources:
        doc = ezdxf.readfile(source.dxf_path)
        for face in doc.modelspace().query('3DFACE[layer=="KLEENKA"]'):
            face.dxf.color = 6
        doc.saveas(source.dxf_path)
    prepared = prepare_working_host(trial_snapshot(False), offset_x_mm=0, offset_y_mm=0, source_report_sha256="c" * 64)
    report = analyze_composite_plate(composite_plate_sources, settings(), maximum_candidates=32, solver_time_limit_s=2,
        cutting_profile="continuous", host_reference=prepared, coordinate_policy=HOST_TRANSLATION_POLICY)
    assert report["front"] and len(report["directions"]) == 4
    assert report["source_demand_preserved"] and not report["placement_eligible"]
    assert "full serialized prism checked" in report["host_envelope"]["source"]
    assert all(c["host_preflight"]["checks"]["planar_host_and_openings"] == "pass"
               for d in report["directions"] for c in d["candidates"])


def test_prepare_cli_preserves_input_binds_hash_and_refuses_overwrite(tmp_path, monkeypatch):
    import runpy
    import sys
    from pathlib import Path
    source, output = tmp_path / "рабочая-плита.json", tmp_path / "host.json"
    content = json.dumps(trial_snapshot(), ensure_ascii=False).encode("utf-8")
    source.write_bytes(content)
    script = Path(__file__).resolve().parents[2] / "scripts/prepare_working_host.py"
    monkeypatch.setattr(sys, "argv", [str(script), "--report", str(source), "--offset-x-mm", "1234",
                                     "--offset-y-mm", "-4321", "--output", str(output)])
    runpy.run_path(str(script), run_name="__main__")
    result = json.loads(output.read_bytes())
    assert result["source_report_sha256"] == hashlib.sha256(content).hexdigest()
    assert host_from_working_input(result).outer_mm == (-3234, 2321, 8766, 14321)
    before = output.read_bytes()
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(script), run_name="__main__")
    assert exc.value.code == 2 and output.read_bytes() == before
    assert source.read_bytes() == content


@pytest.mark.parametrize("status", ["blocked_setup", "blocked_preflight", "passed_rolled_back", "failed_rolled_back"])
def test_physical_plan_trial_host_keeps_native_restoration_evidence(status):
    report = trial_snapshot()
    if status != "blocked_setup":
        report["execution"] = {"schema_version": report["schema_version"], "status": status,
            "placement_eligible": False, "read_issues": [], "host": deepcopy(report["host"]),
            "restoration": {"verified": True}, "readback": "large bar payload not required by host contract"}
    report.update(schema_version="revit-physical-bar-plan-trial-report/v1", status=status,
                  engineering_approval=False, physical_plan={"provenance": "not host geometry"})
    before = deepcopy(report)
    prepared = prepare_working_host(report, offset_x_mm=0, offset_y_mm=0, source_report_sha256="d" * 64)
    host = host_from_working_input(prepared)
    assert "Physical Plan Trial" in host.source
    assert host.outer_mm == (-2000, -2000, 10000, 10000)
    assert "physical_plan" not in prepared["snapshot"]
    assert "readback" not in prepared["snapshot"].get("execution", {})
    assert report == before


@pytest.mark.parametrize("bad", ["missing_execution", "status", "host", "read_issues", "restoration", "approval", "schema"])
def test_physical_plan_host_rejects_inconsistent_execution_or_unconfirmed_restore(bad):
    report = trial_snapshot()
    report.update(schema_version="revit-physical-bar-plan-trial-report/v1", status="passed_rolled_back",
                  engineering_approval=False)
    report["execution"] = {"schema_version": "revit-full-plate-trial-report/v1", "status": report["status"],
        "placement_eligible": False, "read_issues": [], "host": deepcopy(report["host"]),
        "restoration": {"verified": True}}
    if bad == "missing_execution":
        del report["execution"]
    elif bad == "status":
        report["execution"]["status"] = "rollback_unconfirmed"
    elif bad == "host":
        report["execution"]["host"]["element_id"] += 1
    elif bad == "read_issues":
        report["execution"]["read_issues"] = ["cannot read host"]
    elif bad == "restoration":
        report["execution"]["restoration"]["verified"] = False
    elif bad == "approval":
        report["engineering_approval"] = True
    else:
        report["execution"]["schema_version"] = "unknown"
    with pytest.raises(ValueError):
        prepare_working_host(report, offset_x_mm=0, offset_y_mm=0, source_report_sha256="d" * 64)
