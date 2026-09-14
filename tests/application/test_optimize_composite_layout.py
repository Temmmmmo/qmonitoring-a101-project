from copy import deepcopy
import hashlib
import json
import subprocess
import sys

import pytest

from rebar.application.composite_host_review import HOST_COORDINATE_POLICY
from rebar.application.optimize_composite_layout import optimize_composite_layout
from rebar.dxf_ingest import read_mosaic

from test_composite_host_review import rectangle, reference_sample
from test_composite_layout_review import ROOT, public_sources, recipe_mosaic, request_sample


def config_sample():
    data = request_sample()
    data["zones"] = []
    return data


@pytest.mark.parametrize("with_host", [False, True])
def test_application_search_to_review_preserves_inputs_and_separates_host_status(with_host):
    pytest.importorskip("scipy")
    mosaic, config = recipe_mosaic(), config_sample()
    before = deepcopy((mosaic, config))
    kwargs = {"host_reference": reference_sample(), "coordinate_policy": HOST_COORDINATE_POLICY} if with_host else {}
    result = optimize_composite_layout(mosaic, config, maximum_candidates=16, maximum_zones=4, **kwargs)
    assert result["status"] == "research_front_found" and result["optimizer_executed"]
    assert result["front"] and result["selected_review"]["status"] == "research_checks_passed"
    assert not result["placement_eligible"] and not result["selected_review"]["placement_eligible"]
    for point in result["front"]:
        assert point["uncovered_cell_count"] == 0 and point["review_input"]["zones"]
        if with_host:
            assert point["host_preflight"]["status"] == "needs_external_checks"
            assert point["host_preflight"]["demand_feasibility"]["status"] == "necessary_condition_passed"
    json.dumps(result, allow_nan=False)
    assert (mosaic, config) == before


@pytest.mark.parametrize("bad", ["zones", "missing_host", "missing_policy"])
def test_search_configuration_never_silently_ignores_user_input(bad):
    config = config_sample()
    kwargs = {}
    if bad == "zones":
        config["zones"] = request_sample()["zones"]
    elif bad == "missing_host":
        kwargs["coordinate_policy"] = HOST_COORDINATE_POLICY
    else:
        kwargs["host_reference"] = reference_sample()
    with pytest.raises(ValueError):
        optimize_composite_layout(recipe_mosaic(), config, **kwargs)


def test_incompatible_host_returns_engineering_decision_without_running_solver():
    reference = reference_sample()
    floor = reference["floor"]
    floor["bbox_mm"] = {"min_mm": [0, 0, -300], "max_mm": [3900, 800, 0]}
    for side, z in (("top", 0), ("bottom", -300)):
        floor[side + "_faces"][0]["edge_loops"] = [rectangle((0, 0, 3900, 800), z)]
    result = optimize_composite_layout(recipe_mosaic(), config_sample(), host_reference=reference,
                                      coordinate_policy=HOST_COORDINATE_POLICY, host_policy="require-planar-containment")
    assert result["status"] == "engineering_decision_required" and not result["front"]
    assert not result["telemetry"]["solver_executed"] and not result["placement_eligible"]


@pytest.mark.parametrize("bad", [None, "missing_phase", "existing_output", "rvt_output", "duplicate_key", "missing_policy"])
def test_cli_full_ingest_search_and_revalidation_without_file_mutations(tmp_path, bad):
    pytest.importorskip("scipy")
    dxf, shk = public_sources(tmp_path)
    mosaic = read_mosaic(str(dxf), str(shk))
    config = config_sample()
    config["direction"] = {"layer": mosaic.direction.layer.value, "axis": mosaic.direction.axis.value}
    config["additional_origins_by_level"] = {str(i): [0, 50] for i in range(1, 6)}
    if bad == "missing_phase":
        config["additional_origins_by_level"].pop("1")
    path, output = tmp_path / "config.json", tmp_path / ("keep.rvt" if bad == "rvt_output" else "result.json")
    path.write_text('{"x":1,"x":2}' if bad == "duplicate_key" else json.dumps(config), encoding="utf-8")
    if bad in ("existing_output", "rvt_output"):
        output.write_bytes(b"keep")
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (dxf, shk, path)}
    command = [sys.executable, str(ROOT / "scripts/optimize_composite_layout.py"), "--dxf", str(dxf),
               "--shk", str(shk), "--config", str(path), "--output", str(output),
               "--maximum-candidates", "32", "--solver-time-limit", "2"]
    if bad == "missing_policy":
        command += ["--reference", str(path)]
    run = subprocess.run(command, capture_output=True, text=True)
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == sha for p, sha in before.items())
    if bad:
        assert run.returncode == 2, run.stderr
        assert not output.exists() or output.read_bytes() == b"keep"
    else:
        assert run.returncode == 0, run.stderr
        result = json.loads(output.read_text())
        assert result["source_cell_count"] == 96 and result["status"] == "research_front_found"
        assert result["source_sha256"] == {"dxf": before[dxf], "shk": before[shk], "config": before[path]}
        assert not result["placement_eligible"]
