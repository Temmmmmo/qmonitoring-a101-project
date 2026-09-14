"""Synthetic upload-only facts: no private source model or report fixture."""
from copy import deepcopy
import hashlib
import importlib
import json

from fastapi.testclient import TestClient
import pytest
import starlette.formparsers

from rebar.application import revit_snapshot_inspection as inspector
from rebar.web import revit_inspection as endpoint

client = TestClient(importlib.import_module("rebar.web.app").app)


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def report(kind="host"):
    result = {"schema_version": inspector.SCHEMAS[kind], "probe_version": "0.1.0",
        "created_utc": "2026-09-14T10:00:00Z", "read_only": True, "units": "mm",
        "coordinate_system": "revit-internal-origin-and-axes", "placement_eligible": False,
        "engineering_approval": False, "status": "collected", "host_id": 10,
        "document": {"title": "Синтетическая модель", "path": "C:\\private\\model.rvt",
            "is_workshared": True, "modification_flag_unchanged": True,
            "project_information_unique_id": "project-unique"},
        "host": {"element_id": 10, "unique_id": "floor-unique", "name": "Плита", "mark": "П1",
            "bbox_mm": {"min_mm": [0, 0, 0], "max_mm": [5000, 5000, 300]},
            "covers": {key: {"distance_mm": 25} for key in ("top", "bottom", "other")}},
        "read_issues": []}
    if kind == "host":
        result["host_solid"] = {"faces": [{"plane": {}, "edge_loops": []}], "volume_mm3": 7_500_000_000}
    else:
        result.update({"host_unique_id": "floor-unique", "bars": [bar()], "parents": [],
            "worksharing": {"status": "collected", "user_worksets": [], "closed_user_worksets": []},
            "scope": {"unsupported_host_elements": [], "links": [],
                "whole_model_collision_inventory_complete": False},
            "summary": {"host_reinforcement_element_count_read": 1, "read_existing_position_count": 1,
                "exact_geometry_position_count": 1, "excluded_position_count": 1,
                "physical_bar_count": 1, "native_host_inventory_complete": True}})
    return result


def bar():
    line = {"kind": "Line", "is_bound": True, "exact_geometry_supported": True,
        "start_mm": [50, 50, 40], "end_mm": [3950, 50, 40], "mid_mm": [2000, 50, 40],
        "length_mm": 3900, "tessellated_points_mm": [[50, 50, 40], [3950, 50, 40]]}
    return {"element_id": 20, "unique_id": "bar-unique", "host_id": 10, "readback_complete": True,
        "physical_bar_count": 1, "number_of_bar_positions": 2, "quantity": 1,
        "positions": [{"position_index": 0, "position_key": ["bar-unique", 0], "exists": True,
                "curves": [line], "status": "collected"},
            {"position_index": 1, "position_key": ["bar-unique", 1], "exists": False,
                "curves": [], "status": "excluded"}]}


def partial(data, message="Closed worksets"):
    data["status"] = "partial"
    data["read_issues"].append({"section": "worksets", "message": message})
    data["summary"]["native_host_inventory_complete"] = False
    data["summary"]["physical_bar_count"] = None
    return data


def upload(host=None, rebar=None, **kwargs):
    files = {}
    for kind, value in (("host", host), ("rebar", rebar)):
        if value is not None:
            files[kind] = (kind + ".json", value if isinstance(value, bytes) else encode(value), "application/json")
    return client.post("/api/revit/inspect-snapshots", files=files, **kwargs)


def test_pair_inspection_hashes_counts_identity_and_no_live_or_engineering_claims():
    host, rebar = report(), report("rebar")
    response = upload(host, rebar)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["schema_version"] == "qmonitoring-revit-snapshot-inspection/v1"
    assert result["binding"]["status"] == "recorded_identity_matches"
    for key in ("placement_eligible", "engineering_approval", "live_model_checked", "source_inputs_retained", "connected_to_calculation"):
        assert result[key] is False
    assert result["snapshots"]["host"]["sha256"] == hashlib.sha256(encode(host)).hexdigest()
    inventory = result["snapshots"]["rebar"]["inventory"]
    assert inventory["status"] == "complete_as_reported"
    assert inventory["read_existing_position_count"] == inventory["exact_geometry_position_count"] == 1
    assert inventory["excluded_position_count"] == 1
    assert inventory["whole_model_collision_inventory_complete"] is False
    assert "private" not in response.text and "C:\\" not in response.text
    assert "curves" not in response.text
    assert response.headers["cache-control"] == "no-store, max-age=0"


@pytest.mark.parametrize("kind", ["host", "rebar"])
def test_optional_single_snapshot_and_no_disk_writes(kind, monkeypatch):
    monkeypatch.setattr("builtins.open", lambda *a, **k: pytest.fail("Inspector must not open files"))
    result = inspector.inspect_revit_snapshots(**{kind: encode(report(kind))})
    assert result["binding"]["status"] == "not_compared"
    assert result["snapshots"]["host" if kind == "rebar" else "rebar"] is None


@pytest.mark.parametrize("change", ["uid", "id", "document"])
def test_different_host_or_document_is_explicit_mismatch(change):
    host, rebar = report(), report("rebar")
    if change == "uid":
        host["host"]["unique_id"] = "different-floor"
    elif change == "id":
        host["host"]["element_id"] = host["host_id"] = 11
    else:
        host["document"]["project_information_unique_id"] = "different-project"
    result = upload(host, rebar)
    assert result.status_code == 200
    assert result.json()["binding"]["status"] == "mismatch"


def test_same_uid_different_paths_or_missing_project_identity_does_not_prove_same_document():
    host, rebar = report(), report("rebar")
    host["document"].pop("project_information_unique_id")
    host["document"]["path"] = "C:\\different-copy.rvt"
    result = upload(host, rebar).json()["binding"]
    assert result["status"] == "host_matches_document_unverified"
    assert result["document_project_uid_matches"] is None
    assert result["document_path_hash_matches"] is False
    host["host"] = None
    assert upload(host, rebar).json()["binding"]["status"] == "insufficient_identity"


def test_partial_closed_worksets_and_unsupported_native_reinforcement_stay_unknown():
    rebar = partial(report("rebar"))
    closed = {"id": 4, "unique_id": "workset", "name": "Закрытый", "is_open": False}
    rebar["worksharing"]["user_worksets"] = [closed]
    rebar["worksharing"]["closed_user_worksets"] = [closed]
    rebar["scope"]["unsupported_host_elements"] = [{"element_id": 30, "class": "FabricSheet"}]
    rebar["scope"]["links"] = [{"element_id": 40, "geometry_read": False}]
    result = upload(rebar=rebar)
    assert result.status_code == 200, result.text
    summary = result.json()["snapshots"]["rebar"]
    assert summary["inventory"]["status"] == "partial"
    assert summary["inventory"]["physical_bar_count_as_reported"] is None
    assert summary["inventory"]["read_existing_position_count"] == 1
    assert summary["inventory"]["unsupported_host_element_count"] == 1
    assert summary["worksharing"]["closed_worksets"] == ["Закрытый"]


def test_limit_failure_retains_unknown_positions_without_inventing_zero_complete_inventory():
    rebar = partial(report("rebar"), "Read limit exceeded")
    row = rebar["bars"][0]
    row.update(positions=[], readback_complete=False, physical_bar_count=None)
    rebar["summary"].update(read_existing_position_count=0, exact_geometry_position_count=0, excluded_position_count=0)
    result = upload(rebar=rebar)
    assert result.status_code == 200, result.text
    inventory = result.json()["snapshots"]["rebar"]["inventory"]
    assert inventory["unknown_position_count"] == 2
    assert inventory["physical_bar_count_as_reported"] is None


def test_parent_is_not_double_counted_and_supported_arc_record_is_accepted():
    rebar = report("rebar")
    rebar["parents"] = [{"element_id": 30, "unique_id": "area", "host_id": 10, "child_ids": [20]}]
    rebar["bars"][0]["system_id"] = 30
    curve = rebar["bars"][0]["positions"][0]["curves"][0]
    curve.update(kind="Arc", center_mm=[0, 0, 0], radius_mm=50, normal=[0, 0, 1],
        x_direction=[1, 0, 0], y_direction=[0, 1, 0], start_parameter=0, end_parameter=1,
        parameter_units="radians")
    result = upload(rebar=rebar)
    assert result.status_code == 200, result.text
    inventory = result.json()["snapshots"]["rebar"]["inventory"]
    assert inventory["host_reinforcement_element_count_read"] == inventory["physical_bar_count_as_reported"] == 1
    assert inventory["parent_system_count"] == 1


@pytest.mark.parametrize("value", [b'{"schema_version":"one","schema_version":"two"}',
    b'{"extra":{"x":0,"x":1}}', b'{"extra":NaN}', b'{"extra":Infinity}', b'{"extra":-Infinity}',
    b'{"extra":1e999}', b'{"extra":-1e999}', b'{"extra":"\xff"}', b'{"extra":"\\ud800"}',
    b'{"extra":9223372036854775808}', b'[]', b'null', b'{}', b'', b'{', b'\xff'])
def test_reject_unsafe_or_wrong_json_even_in_unused_fields(value):
    response = upload(host=value)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("field,value", [("schema_version", "physical-bar-plan-trial/v1"),
    ("placement_eligible", True), ("placement_eligible", 0), ("engineering_approval", True),
    ("read_only", False), ("read_only", 1), ("units", "ft"), ("coordinate_system", "shared"),
    ("status", "approved"), ("host_id", True)])
def test_reject_wrong_envelope(field, value):
    host = report()
    host[field] = value
    assert upload(host=host).status_code == 422


@pytest.mark.parametrize("change", ["duplicate-bar", "duplicate-position", "position-key", "bar-host",
    "host-uid", "host-id", "quantity", "missing-curves", "excluded-curves", "nonexact-collected",
    "curve-dimension", "negative-length", "summary", "false-completeness", "parent-missing",
    "duplicate-parent", "parent-children", "closed-list", "physical-count", "boolean-summary"])
def test_reject_inconsistent_inventory(change):
    data = report("rebar")
    row = data["bars"][0]
    if change == "duplicate-bar":
        data["bars"].append(deepcopy(row))
    elif change == "duplicate-position":
        row["positions"][1]["position_index"] = 0
    elif change == "position-key":
        row["positions"][0]["position_key"] = ["different", 0]
    elif change == "bar-host":
        row["host_id"] = 11
    elif change == "host-uid":
        data["host_unique_id"] = "different"
    elif change == "host-id":
        data["host"]["element_id"] = 11
    elif change == "quantity":
        row["quantity"] = 2
    elif change == "missing-curves":
        row["positions"][0]["curves"] = []
    elif change == "excluded-curves":
        row["positions"][1]["curves"] = row["positions"][0]["curves"]
    elif change == "nonexact-collected":
        row["positions"][0]["curves"][0].update(kind="Spline", exact_geometry_supported=False)
    elif change == "curve-dimension":
        row["positions"][0]["curves"][0]["start_mm"] = [0, 1]
    elif change == "negative-length":
        row["positions"][0]["curves"][0]["length_mm"] = -1
    elif change == "summary":
        data["summary"]["read_existing_position_count"] = 2
    elif change == "false-completeness":
        data["read_issues"] = [{"section": "foo", "message": "unread"}]
    elif change == "parent-missing":
        row["system_id"] = 30
    elif change == "duplicate-parent":
        parent = {"element_id": 30, "unique_id": "area", "host_id": 10, "child_ids": [20]}
        row["system_id"] = 30
        data["parents"] = [parent, parent]
    elif change == "parent-children":
        data["parents"] = [{"element_id": 30, "unique_id": "area", "host_id": 10, "child_ids": [20, 20]}]
    elif change == "closed-list":
        data["worksharing"]["closed_user_worksets"] = [{"name": "not listed"}]
    elif change == "physical-count":
        data["summary"]["physical_bar_count"] = 2
    else:
        data["summary"]["exact_geometry_position_count"] = True
    response = upload(rebar=data)
    assert response.status_code == 422, response.text


def test_resource_budgets_and_combined_size_are_explicit(monkeypatch):
    data = report()
    data["unused"] = [[[[[0]]]]]
    monkeypatch.setattr(inspector, "MAX_JSON_DEPTH", 4)
    assert upload(host=data).status_code == 422
    monkeypatch.setattr(inspector, "MAX_JSON_DEPTH", 64)
    monkeypatch.setattr(inspector, "MAX_JSON_NODES", 10)
    assert upload(host=data).status_code == 422
    monkeypatch.setattr(inspector, "MAX_JSON_NODES", 2_000_000)
    monkeypatch.setattr(inspector, "MAX_SNAPSHOT_BYTES", len(encode(report())) + len(encode(report("rebar"))) - 1)
    assert upload(report(), report("rebar")).status_code == 413


def test_request_limit_before_multipart_parse_with_length_and_stream(monkeypatch):
    monkeypatch.setattr(endpoint, "MAX_REQUEST_BYTES", 64)
    monkeypatch.setattr(starlette.formparsers, "MultiPartParser", lambda *a, **k: pytest.fail("Do not parse oversized body"))
    assert upload(host=report()).status_code == 413
    result = client.post("/api/revit/inspect-snapshots", headers={"Content-Type": "multipart/form-data; boundary=b"},
        content=iter([b"x" * 33, b"x" * 33]))
    assert result.status_code == 413


@pytest.mark.parametrize("files", [[("host", ("a.json", b"{}")), ("host", ("b.json", b"{}"))],
    [("host", ("a.json", b"{}")), ("rebar", ("b.json", b"{}")), ("extra", ("c.json", b"{}"))],
    [("private", ("a.json", b"{}"))], [("host", ("model.rvt", b"{}"))]])
def test_extra_duplicate_non_json_fields_rejected_and_spooled_files_closed(files, monkeypatch):
    created = []
    original = starlette.formparsers.SpooledTemporaryFile

    def capture(*args, **kwargs):
        file = original(*args, **kwargs)
        created.append(file)
        return file

    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", capture)
    response = client.post("/api/revit/inspect-snapshots", files=files)
    assert response.status_code == 422
    assert created and all(file.closed for file in created)


def test_missing_files_text_fields_wrong_content_type_and_schema_slot_rejected():
    assert client.post("/api/revit/inspect-snapshots").status_code == 422
    assert client.post("/api/revit/inspect-snapshots", json=report()).status_code == 422
    assert client.post("/api/revit/inspect-snapshots", files={"host": (None, "text")}).status_code == 422
    assert upload(host=report("rebar")).status_code == 422
    assert upload(rebar=report()).status_code == 422


def test_inspector_ui_is_separate_and_uses_safe_text_not_html_injection():
    html = client.get("/revit").text
    assert 'id="snapshot-form"' in html and 'name="host"' in html and 'name="rebar"' in html
    assert "32 MiB" in html
    js = client.get("/static/revit-inspection.js")
    assert js.status_code == 200 and "textContent" in js.text and "innerHTML" not in js.text
    assert "/api/revit/inspect-snapshots" in js.text and 'method: "POST"' in js.text
    assert "localStorage" not in js.text and "sessionStorage" not in js.text


def test_sha_changes_for_whitespace_and_issue_display_cap_does_not_hide_total():
    data = report()
    data["status"] = "partial"
    data["read_issues"] = [{"section": "test", "message": str(index)} for index in range(53)]
    one = inspector.inspect_revit_snapshots(host=encode(data))["snapshots"]["host"]
    two = inspector.inspect_revit_snapshots(host=encode(data) + b"\n")["snapshots"]["host"]
    assert one["sha256"] != two["sha256"]
    assert one["read_issue_count"] == 53 and len(one["read_issues"]) == 50
    assert one["read_issues_omitted_from_display"] == 3
