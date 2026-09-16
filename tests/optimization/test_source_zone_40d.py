import pytest

from rebar.reporting.source_graphics import source_zone_40d_certificate


def graphics(axis="X", bounds=(100, 0, 1400, 100), demand=(500, 0, 1000, 100), diameter=10, length=1300):
    rows = [{"direction": {"layer": "bottom", "axis": axis}, "zone_drafts": [{
        "source_zone_id": "Z", "demand_bbox_mm": list(demand), "components": [{"component_index": 0,
        "diameter_mm": diameter, "installed_length_mm": length, "bar_axis_bbox_mm": list(bounds)}]}]}]
    for layer, other_axis in (("bottom", "X"), ("bottom", "Y"), ("top", "X"), ("top", "Y")):
        if (layer, other_axis) != ("bottom", axis):
            rows.append({"direction": {"layer": layer, "axis": other_axis}, "zone_drafts": []})
    return {"schema_version": "source-isofields-zones/v1", "source_stage": "original-parametric-zones-before-physical-normalization", "units": "mm", "directions": rows}


def test_source_40d_certificate_accepts_x_y_and_longer_catalogue_length():
    x = source_zone_40d_certificate(graphics())
    y = source_zone_40d_certificate(graphics("Y", (0, 100, 100, 1500), (0, 500, 100, 1100), length=1400))
    longer = source_zone_40d_certificate(graphics(bounds=(0, 0, 1600, 100), length=1600))
    assert x["status"] == y["status"] == longer["status"] == "pass"
    assert x["component_count"] == y["component_count"] == longer["component_count"] == 1
    assert x["logical_demand_coverage"] == "not_checked"


def test_source_40d_certificate_records_short_end_and_changes_with_packet():
    good = source_zone_40d_certificate(graphics())
    bad = source_zone_40d_certificate(graphics(bounds=(101, 0, 1400, 100), length=1299))
    assert bad["status"] == "fail" and bad["violation_count"] == 1
    assert bad["violations"][0]["left_extension_mm"] == 399
    assert good["source_packet_sha256"] != bad["source_packet_sha256"]


def test_source_40d_certificate_rejects_inconsistent_installed_length():
    certificate = source_zone_40d_certificate(graphics(length=1299))
    assert certificate["status"] == "fail"
    assert certificate["violations"][0]["installed_length_mm"] == 1299


def test_source_40d_certificate_rejects_nonfinite_geometry():
    with pytest.raises(ValueError, match="finite"):
        source_zone_40d_certificate(graphics(diameter=float("nan")))


def test_source_40d_certificate_rejects_boolean_bbox_coordinate():
    with pytest.raises(ValueError, match="finite ordered"):
        source_zone_40d_certificate(graphics(bounds=(True, 0, 1400, 100)))


@pytest.mark.parametrize("mutate", [
    lambda value: value.pop("schema_version"),
    lambda value: value.update(units="m"),
    lambda value: value.update(directions=value["directions"][:3]),
    lambda value: value["directions"].append(value["directions"][0]),
])
def test_source_40d_certificate_rejects_incomplete_or_duplicate_packet(mutate):
    value = graphics()
    mutate(value)
    with pytest.raises(ValueError):
        source_zone_40d_certificate(value)


def test_source_40d_certificate_marks_empty_packet_not_checked():
    value = graphics()
    value["directions"][0]["zone_drafts"] = []
    certificate = source_zone_40d_certificate(value)
    assert certificate["status"] == "not_checked"
    assert certificate["reason"] == "no_components"
    assert len(certificate["source_packet_sha256"]) == 64


def test_source_40d_certificate_marks_zone_without_components_not_checked():
    value = graphics()
    value["directions"][0]["zone_drafts"][0]["components"] = []
    certificate = source_zone_40d_certificate(value)
    assert certificate["status"] == "not_checked"
    assert certificate["reason"] == "zone_without_components"
    assert certificate["policy_id"]
    assert len(certificate["source_packet_sha256"]) == 64
