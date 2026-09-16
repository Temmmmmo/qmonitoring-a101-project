import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rebar.optimization.contracts.plate import PLATE_DIRECTIONS

web = importlib.import_module("rebar.web.app")
client = TestClient(web.app)


def uploads(sources):
    files = {}
    for source, direction in zip(sources, PLATE_DIRECTIONS):
        key = f"{direction.layer.value}_{direction.axis.value.lower()}"
        files["dxf_" + key] = (source.dxf_path.name, source.dxf_path.read_bytes(), "application/octet-stream")
        files["shk_" + key] = (source.shk_path.name, source.shk_path.read_bytes(), "application/octet-stream")
    return files


def settings():
    return {"directions": [{"layer": d.layer.value, "axis": d.axis.value, "background_origin_mm": 0,
            "first_300_offset_mm": 150, "second_offset_mm": 50, "steel_class": "A500",
            "source": "Synthetic HTTP test, not project approval", "contact_side": "left"} for d in PLATE_DIRECTIONS]}


def options(**overrides):
    return {"placement_settings": json.dumps(settings()), "maximum_candidates": 32,
            "solver_time_limit_s": 2, "cutting_profile": "continuous", **overrides}


def test_composite_workspace_link_assets_and_no_cache():
    assert 'href="/composite"' in client.get("/").text
    for path in ("/composite", "/static/composite.js", "/static/composite.css"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store, max-age=0"
    page = client.get("/composite").text
    assert "Excel не нужен" in page
    assert "расчётный черновик" in page and "запрет инженерного размещения" in page
    assert 'id="run-demo" type="button"' in page and "Посмотреть пример" in page
    assert 'id="custom-inputs"' in page and 'id="demo-notice"' in page
    assert 'href="/composite?demo=1"' in client.get("/").text


def test_one_click_demo_runs_without_upload_and_does_not_approve_project_settings():
    response = client.post("/api/composite-demo")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store, max-age=0"
    report = response.json()
    assert report["demo"]["source_kind"] == "synthetic"
    assert report["front"] and not report["placement_eligible"]
    assert [d["source_cell_count"] for d in report["directions"]] == [96, 96, 96, 96]
    assert report["maximum_cutting_overhead_pct"] == 5 and report["host_envelope"] is None
    assert all(d["settings"]["background_origin_mm"] == 0 and "DEMO" in d["settings"]["source"] for d in report["directions"])
    for direction in report["directions"]:
        for candidate in direction["candidates"]:
            assert 'class="source-zones"' in candidate["svg"]
            assert candidate["svg"].count('data-zone-id=') == len(candidate["zone_drafts"])


def test_real_multipart_to_four_direction_core_and_temporary_cleanup(composite_plate_sources, monkeypatch):
    seen = []
    actual = web.analyze_composite_plate

    def record(sources, *args, **kwargs):
        seen.extend(Path(source.dxf_path) for source in sources)
        seen.extend(Path(source.shk_path) for source in sources)
        assert all(path.exists() for path in seen)
        assert len(set(seen)) == 8  # identical SHK filenames cannot overwrite another role
        return actual(sources, *args, **kwargs)

    monkeypatch.setattr(web, "analyze_composite_plate", record)
    response = client.post("/api/analyze-composite-plate", files=uploads(composite_plate_sources), data=options())
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["direction_count"] == 4 and report["front"] and not report["placement_eligible"]
    assert [d["direction"] for d in report["directions"]] == [{"layer": d.layer.value, "axis": d.axis.value} for d in PLATE_DIRECTIONS]
    assert all(not path.exists() for path in seen)
    assert all(len(d["source"]["sha256"]["shk"]) == 64 for d in report["directions"])


@pytest.mark.parametrize("bad", ["missing_dxf", "missing_shk", "wrong_direction", "wrong_extension", "empty_file",
    "missing_phase", "duplicate_direction", "extra_field", "nan", "duplicate_json_key", "wrong_units", "huge_budget",
    "missing_host", "host_unconfirmed", "malformed_host"])
def test_bad_upload_is_not_silently_replaced_with_partial_or_old_solution(composite_plate_sources, bad):
    files, data, profile = uploads(composite_plate_sources), options(), settings()
    expected_status = 422
    if bad == "missing_dxf":
        del files["dxf_bottom_x"]
    elif bad == "missing_shk":
        del files["shk_top_y"]
    elif bad == "wrong_direction":
        files["dxf_top_x"], files["dxf_top_y"] = files["dxf_top_y"], files["dxf_top_x"]
    elif bad == "wrong_extension":
        files["shk_top_x"] = ("not-shk.xlsx", b"bad", "application/octet-stream")
    elif bad == "empty_file":
        files["shk_top_x"] = ("empty.shk", b"", "application/octet-stream")
        expected_status = 400
    elif bad == "missing_phase":
        profile["directions"][0]["background_origin_mm"] = None
    elif bad == "duplicate_direction":
        profile["directions"][1] = dict(profile["directions"][0])
    elif bad == "extra_field":
        profile["directions"][0]["ignore_host"] = True
    elif bad == "nan":
        profile["directions"][0]["second_offset_mm"] = float("nan")
    elif bad == "wrong_units":
        profile["units"] = "m"
    elif bad == "huge_budget":
        data["maximum_candidates"] = 1025
    elif bad == "missing_host":
        data["host_xy_confirmed"] = "true"
    elif bad in ("host_unconfirmed", "malformed_host"):
        files["host_reference"] = ("reference.json", b'{"floor":[]}', "application/json")
        if bad == "malformed_host":
            data["host_xy_confirmed"] = "true"
    data["placement_settings"] = '{"directions":[],"directions":[]}' if bad == "duplicate_json_key" else json.dumps(profile)
    response = client.post("/api/analyze-composite-plate", files=files, data=data)
    assert response.status_code == expected_status, response.text
    assert "front" not in response.json()


def test_large_upload_closes_request_and_returns_413(composite_plate_sources, monkeypatch):
    monkeypatch.setattr(web, "MAX_UPLOAD_BYTES", 10)
    response = client.post("/api/analyze-composite-plate", files=uploads(composite_plate_sources), data=options())
    assert response.status_code == 413
