import hashlib
import json

import pytest

from rebar.application.demo import write_demo_dxf
from rebar.application.revit_workflow import analyze_workflow, parse_request


def request_for(path, **settings):
    return {"schema_version": "qmonitoring-workflow-calculation-request/v1", "direction": "bottom-X",
        "settings": {"background_diameter_mm": 12, "background_step_mm": 300, "anchorage_diameters": 40,
            "minimum_zone_fe_count": 2, "algorithm": "bsp", "mass_preference": 0.5, **settings},
        "axis_profile": {"background_origin_mm": 0, "first_300_offset_mm": 100, "contact_side": "left", "steel_class": "A500"},
        "mapping_id": "plate-zero-d12-v1", "source_sha256": {"dxf": hashlib.sha256(path.read_bytes()).hexdigest()}}


@pytest.fixture
def source(tmp_path):
    path = tmp_path/"Нижняя по оси X.dxf"
    write_demo_dxf("irregular-plate-x", path)
    return path


def test_real_calculation_preserves_single_direction_and_checks_sto(source):
    request = json.dumps(request_for(source)).encode()
    result = analyze_workflow(source, request)
    assert result["calculation_performed"] is True
    assert result["placement_eligible"] is result["engineering_approval"] is False
    assert result["request_sha256"] == hashlib.sha256(request).hexdigest()
    assert result["source_cell_count"] == len(result["source"]["cells"]) == 96
    assert result["source"]["direction"] == {"layer": "bottom", "axis": "X"}
    assert result["metrics"]["source_zone_count"] > 0
    assert result["checks"]["host_3d_revit"] == "not_checked"
    for zone in result["source"]["zone_drafts"]:
        for component in zone["components"]:
            if component["nominal_step_mm"] == 150:
                assert component["placement"]["pattern"]["offsets_mm"] == [100, 200]


@pytest.mark.parametrize("change", [
    lambda d: d["settings"].update(background_diameter_mm=10),
    lambda d: d["settings"].update(background_step_mm=200),
    lambda d: d["settings"].update(anchorage_diameters=39),
    lambda d: d["settings"].update(minimum_zone_fe_count=True),
    lambda d: d["source_sha256"].update(dxf="0"*64),
    lambda d: d.update(direction="top-X"),
])
def test_settings_and_source_mismatch_fail_before_calculation(source, change):
    request = request_for(source)
    change(request)
    with pytest.raises(ValueError):
        analyze_workflow(source, json.dumps(request).encode())


@pytest.mark.parametrize("content", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}', b'[]', b'x'*16385])
def test_unsafe_request_rejected(content):
    with pytest.raises(ValueError):
        parse_request(content)
