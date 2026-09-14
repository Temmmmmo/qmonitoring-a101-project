from copy import deepcopy
import json
import subprocess
import sys

import pytest

from rebar.application.composite_host_review import HOST_COORDINATE_POLICY
from rebar.application.optimize_composite_layout import optimize_composite_layout
from rebar.models import Cell

from test_composite_host_review import rectangle, reference_sample
from test_composite_layout_review import ROOT, public_sources, recipe_mosaic
from test_optimize_composite_layout import config_sample


def test_fragment_application_keeps_original_coverage_and_projected_conflicts_visible():
    mosaic, reference, config = recipe_mosaic(), reference_sample(), config_sample()
    for cell in mosaic.cells:
        cell.poly = [(x + 1500, y + 400) for x,y in cell.poly]
    mosaic.cells.append(Cell([(0,400),(400,400),(400,800),(0,800)], (200,600), 2, mosaic.legend[0]))
    config["additional_origins_by_level"] = {"0": [0,-50]}
    floor = reference["floor"]
    floor["bbox_mm"] = {"min_mm": [0,0,-300], "max_mm": [9000,3000,0]}
    for side,z in (("top",0),("bottom",-300)):
        floor[side+"_faces"][0]["edge_loops"] = [rectangle((0,0,9000,3000), z)]
    before = deepcopy((mosaic, config, reference))
    result = optimize_composite_layout(mosaic, config, host_reference=reference, coordinate_policy=HOST_COORDINATE_POLICY,
        host_policy="interior-exceptions", algorithm="fragment-grid", along_step_mm=1500, across_step_mm=900,
        maximum_bar_length_mm=3800, zone_penalties=(0,), bar_penalties=(0,))
    assert result["status"] == "partial_research_front_found" and not result["placement_eligible"]
    assert result["selected_review"]["status"] == "needs_revision"
    for point in result["front"]:
        assert point["target_uncovered_cell_count"] == 0 and point["uncovered_cell_count"] == 1
        assert point["maximum_installed_bar_length_mm"] <= 3800
        assert point["engineer_comparison"]["mass_gate_passed"] is None
        assert point["host_preflight"]["unknown_depth_projected_interzone_conflicts"]["pair_count"] > 0
    assert (mosaic, config, reference) == before
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("bad", ["unknown_algorithm", "missing_interior"])
def test_fragment_configuration_cannot_weaken_the_host_policy(bad):
    with pytest.raises(ValueError):
        optimize_composite_layout(recipe_mosaic(), config_sample(), algorithm="unknown" if bad == "unknown_algorithm" else "fragment-grid")


@pytest.mark.parametrize("incompatible", [["--maximum-candidates", "16"], ["--partition-depth", "2"], []])
def test_fragment_cli_rejects_irrelevant_options_and_emits_report_without_overwrite(tmp_path, incompatible):
    dxf, shk = public_sources(tmp_path)
    config, reference, output = tmp_path/"config.json", tmp_path/"reference.json", tmp_path/"report.json"
    config.write_text(json.dumps(config_sample()))
    reference.write_text(json.dumps(reference_sample()))
    command = [sys.executable, str(ROOT/"scripts/optimize_composite_layout.py"), "--dxf", str(dxf), "--shk", str(shk),
        "--config", str(config), "--reference", str(reference), "--coordinate-policy", HOST_COORDINATE_POLICY,
        "--host-policy", "interior-exceptions", "--algorithm", "fragment-grid", "--output", str(output), *incompatible]
    # A valid tiny timeout exercises the new CLI path safely on the public mosaic.
    if not incompatible:
        from rebar.dxf_ingest import read_mosaic
        mosaic = read_mosaic(str(dxf), str(shk))
        data = config_sample()
        data["direction"] = {"layer": mosaic.direction.layer.value, "axis": mosaic.direction.axis.value}
        data["additional_origins_by_level"] = {str(i): [0,-50] for i in range(1,6)}
        config.write_text(json.dumps(data))
        command += ["--solver-time-limit", "0.000001"]
    before = {p: p.read_bytes() for p in (dxf, shk, config, reference)}
    run = subprocess.run(command, text=True, capture_output=True)
    assert all(p.read_bytes() == raw for p, raw in before.items())
    if incompatible:
        assert run.returncode == 2 and not output.exists()
    else:
        assert run.returncode == 1, run.stderr
        result = json.loads(output.read_text())
        assert result["telemetry"]["algorithm"] == "composite-fragment-guillotine/v1"
        assert result["telemetry"]["timed_out"] and not result["front"] and not result["placement_eligible"]
        assert subprocess.run(command, capture_output=True).returncode == 2
