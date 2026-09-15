import json
from pathlib import Path

from fastapi.testclient import TestClient

from rebar.web.app import app
from rebar.web import revit_workflow as route


def request():
    return {"schema_version": "qmonitoring-workflow-calculation-request/v1", "direction": "bottom-X",
        "settings": {"background_diameter_mm": 12, "background_step_mm": 300, "anchorage_diameters": 40,
            "minimum_zone_fe_count": 2, "algorithm": "bsp", "mass_preference": .5},
        "axis_profile": {"background_origin_mm": 0, "first_300_offset_mm": 100, "contact_side": "left", "steel_class": "A500"},
        "mapping_id": "plate-zero-d12-v1", "source_sha256": {"dxf": "a"*64}}


def test_route_passes_actual_bytes_and_removes_temporary_source(monkeypatch):
    paths = []
    def analyze(path, content, *, shk_path):
        assert Path(path).read_bytes() == b"ACTUAL-DXF"
        assert json.loads(content) == request() and shk_path is None
        paths.append(Path(path))
        return {"calculation_performed": True, "placement_eligible": False}
    monkeypatch.setattr(route, "analyze_workflow", analyze)
    with TestClient(app) as client:
        response = client.post("/api/revit/workflow/analyze", data={"request_json": json.dumps(request())},
            files={"dxf": ("C:\\private\\Низ X.dxf", b"ACTUAL-DXF")})
    assert response.status_code == 200 and not response.json()["placement_eligible"]
    assert not paths[0].exists() and paths[0].name == "Низ X.dxf"


def test_route_rejects_png_and_unsafe_json_without_calling_engine(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("engine called")
    monkeypatch.setattr(route, "analyze_workflow", unexpected)
    with TestClient(app) as client:
        response = client.post("/api/revit/workflow/analyze", data={"request_json": json.dumps(request())},
            files={"dxf": ("input.png", b"PNG")})
        assert response.status_code == 422
        response = client.post("/api/revit/workflow/analyze", data={"request_json": '{"x":1,"x":2}'},
            files={"dxf": ("input.dxf", b"DXF")})
        assert response.status_code == 422


def test_route_bounded_concurrency():
    route._CAPACITY.acquire()
    try:
        with TestClient(app) as client:
            response = client.post("/api/revit/workflow/analyze", data={"request_json": json.dumps(request())},
                files={"dxf": ("input.dxf", b"DXF")})
        assert response.status_code == 429
    finally:
        route._CAPACITY.release()
