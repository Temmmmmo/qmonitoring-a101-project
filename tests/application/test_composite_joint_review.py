from copy import deepcopy
import hashlib
import importlib.util
import json
import sys

import pytest

from rebar.application.composite_host_review import HOST_COORDINATE_POLICY
from rebar.application.composite_joint_review import research_composite_joint_depths
from rebar.models import Cell

from test_composite_host_review import rectangle, reference_sample
from test_composite_layout_review import ROOT, recipe_mosaic, request_sample


def joint_review_sample():
    mosaic, config, ref = recipe_mosaic(), request_sample(phase25=150), reference_sample()
    for cell in mosaic.cells:
        cell.poly = [(x + 1500, y + 400) for x, y in cell.poly]
        cell.centroid = (cell.centroid[0] + 1500, cell.centroid[1] + 400)
    mosaic.cells.append(Cell([(0,400),(400,400),(400,800),(0,800)], (200,600), 2, mosaic.legend[0]))
    mosaic.bbox = (0, 400, 5400, 1200)
    floor = ref["floor"]
    floor["bbox_mm"] = {"min_mm": [0,0,-300], "max_mm": [9000,3000,0]}
    for side, z in (("top", 0), ("bottom", -300)):
        floor[side+"_faces"][0]["edge_loops"] = [rectangle((0,0,9000,3000), z)]
    config["zones"] = [{"id": str(i), "level_index": 0, "demand_bbox_mm": [1500+1950*i,300,3450+1950*i,1200]} for i in range(2)]
    return mosaic, config, ref


def options():
    return {"coordinate_policy": HOST_COORDINATE_POLICY, "candidate_depths_by_component": ((34,77),(37.5,87.5)),
            "minimum_clear_spacing_mm": 25, "hypothesis_source": "Public synthetic experiment", "preserve_component_order": True}


def test_application_recomputes_demand_and_keeps_partial_scope_and_engineering_blockers():
    mosaic, config, ref = joint_review_sample()
    before = deepcopy((mosaic, config, ref))
    report = research_composite_joint_depths(mosaic, config, ref, **options())
    assert report["proposed_zones"] and not report["placement_eligible"]
    assert report["target_uncovered_cell_count"] == 0 and report["original_uncovered_cell_count"] == 1
    assert report["full_solution_mass_kg"] is None and report["engineer_comparison"] == "not_checked"
    assert report["depth_search"]["maximum_axis_depth_mm"] == 87.5
    assert report["depth_search"]["host_preflight"]["checks"]["additional_bar_collisions"] == "pass"
    assert (mosaic, config, ref) == before
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("bad", ["xy", "missing_coverage", "clearance", "depths"])
def test_application_never_certifies_a_different_or_incomplete_input(bad):
    mosaic, config, ref = joint_review_sample()
    settings = options()
    if bad == "xy":
        settings["coordinate_policy"] = "guess"
    elif bad == "missing_coverage":
        config["zones"].pop()
    elif bad == "clearance":
        settings["minimum_clear_spacing_mm"] = float("nan")
    else:
        settings["candidate_depths_by_component"] = ((34,),)
    with pytest.raises(ValueError):
        research_composite_joint_depths(mosaic, config, ref, **settings)


@pytest.mark.parametrize("bad", [None, "hash", "duplicate", "index", "overwrite", "rvt", "depths"])
def test_joint_cli_checks_sources_and_writes_only_new_research_json(tmp_path, monkeypatch, bad):
    # CLI file/hash/output boundaries are real; parser fixture is public and tested
    # separately by Stage A. End-to-end real-file runs are recorded in the journal.
    mosaic, config, ref = joint_review_sample()
    spec = importlib.util.spec_from_file_location("joint_experiment_cli", ROOT/"scripts/experiment_composite_joints.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "read_mosaic", lambda *args: mosaic)
    paths = {"dxf": tmp_path/"source.dxf", "shk": tmp_path/"legend.shk", "reference": tmp_path/"reference.json"}
    paths["dxf"].write_bytes(b"public parser fixture")
    paths["shk"].write_bytes(b"public scale fixture")
    paths["reference"].write_text(json.dumps(ref))
    hashes = {k: hashlib.sha256(p.read_bytes()).hexdigest() for k, p in paths.items()}
    if bad == "hash":
        hashes["dxf"] = "0"*64
    search = {"schema_version": "composite-layout-search/v1", "mode": "research-only", "units": "mm",
              "host_policy": "interior-exceptions", "placement_eligible": False, "source_sha256": hashes,
              "front": [{"review_input": config}], "selected_index": 0}
    paths["search"] = tmp_path/"search.json"
    paths["search"].write_text('{"x":1,"x":2}' if bad == "duplicate" else json.dumps(search))
    output = tmp_path / ("out.rvt" if bad == "rvt" else "out.json")
    if bad == "overwrite":
        output.write_bytes(b"keep")
    before = {p: p.read_bytes() for p in paths.values()}
    argv = [str(ROOT/"scripts/experiment_composite_joints.py"), "--output", str(output)]
    for key, path in paths.items():
        argv += ["--"+key, str(path)]
    argv += ["--coordinate-policy", HOST_COORDINATE_POLICY, "--candidate-depths-mm", "34", "77",
             "--minimum-clear-spacing-mm", "25", "--hypothesis-source", "CLI test", "--preserve-component-order"]
    if bad != "depths":
        argv += ["--candidate-depths-mm", "37.5", "87.5"]
    if bad == "index":
        argv += ["--point-index", "-1"]
    monkeypatch.setattr(sys, "argv", argv)
    if bad:
        with pytest.raises(SystemExit) as error:
            module.main()
        assert error.value.code == 2
        assert output.read_bytes() == b"keep" if output.exists() else True
    else:
        assert module.main() == 0
        data = json.loads(output.read_text())
        assert data["schema_version"] == "composite-joint-depth-research/v1" and not data["placement_eligible"]
        assert data["depth_search"]["host_preflight"]["checks"]["additional_bar_collisions"] == "pass"
        assert data["source_sha256"]["dxf"] == hashes["dxf"]
    assert all(p.read_bytes() == content for p, content in before.items())
