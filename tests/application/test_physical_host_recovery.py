from copy import deepcopy
from dataclasses import replace
import hashlib
import math

import pytest

from rebar.application.physical_host_recovery import fit_physical_layout_to_host
from rebar.application.physical_layout_recovery import _bytes, recover_physical_layout_from_report
from rebar.optimization.contracts.physical import PhysicalNormalizationConfig
from test_physical_bar_trial import physical_source_case
from test_physical_layout_recovery import _deduplicate


def box_snapshot(lower=-800, upper=4000, dx=0, dy=0):
    lo, hi = [lower+dx, lower+dy, 0], [upper+dx, upper+dy, 300]
    faces = []
    for axis in range(3):
        other = [i for i in range(3) if i != axis]
        for sign in (-1, 1):
            points = []
            for a, b in ((0, 0), (1, 0), (1, 1), (0, 1)):
                p = lo.copy()
                p[axis] = hi[axis] if sign == 1 else lo[axis]
                p[other[0]] = hi[other[0]] if a else lo[other[0]]
                p[other[1]] = hi[other[1]] if b else lo[other[1]]
                points.append(p)
            normal = [0, 0, 0]
            normal[axis] = sign
            faces.append({"plane": {"normal": normal, "origin_mm": points[0]}, "edge_loops": [[
                {"kind": "Line", "start_mm": a, "end_mm": b, "length_mm": math.dist(a, b)}
                for a, b in zip(points, points[1:]+points[:1])]]})
    floor = {"element_id": 42, "bbox_mm": {"min_mm": lo, "max_mm": hi},
             "covers": {side: {"distance_mm": 25} for side in ("top", "bottom", "other")},
             "top_faces": [faces[-1]], "bottom_faces": [faces[-2]]}
    return {"schema_version": "revit-full-plate-trial-report/v1", "units": "mm",
        "coordinate_system": "revit-internal-origin-and-axes", "placement_eligible": False,
        "status": "blocked_setup", "host_id": 42, "read_issues": [], "host": floor,
        "host_solid": {"faces": faces, "volume_mm3": (upper-lower)**2 * 300}}


@pytest.fixture(scope="module")
def original():
    return physical_source_case()


@pytest.fixture
def recovered(original, monkeypatch):
    monkeypatch.setattr("rebar.application.physical_layout_recovery.normalize_physical_bars", _deduplicate)
    return recover_physical_layout_from_report(original[1], original[0],
        normalization_config=PhysicalNormalizationConfig(), stock_time_limit_s=5)


def run_fit(recovered, original, snapshot=None, **kwargs):
    return fit_physical_layout_to_host(recovered, original[0], snapshot or box_snapshot(),
        **{**dict(offset_x_mm=0, offset_y_mm=0, binding_source="explicit synthetic test only",
                 conservative_whole_height=True, source_report_sha256="b"*64, stock_time_limit_s=5), **kwargs})


def test_surplus_is_fitted_with_full_independent_revalidation(original, recovered):
    before = deepcopy(recovered)
    result = run_fit(recovered, original)
    assert recovered == before
    assert result.host_review["blocked_before"] == 32
    assert result.host_review["blocked_after"] == 0
    assert result.host_review["accepted_moved_bar_count"] == 32
    assert result.recovery.status == "prepared_rollback_only"
    assert result.recovery.packet["expected"]["physical_bar_count"] == 32
    assert result.recovery.packet["expected"]["additional_mass_kg"] == recovered.packet["expected"]["additional_mass_kg"]
    assert result.recovery.review["source_bar_reference_count"] == 64
    assert result.recovery.review["source_axis_and_new40d_status"] == "pass"
    assert result.recovery.review["stock_cutting"]["status"] == "pass"
    assert all(c["status"] == "pass" for c in result.recovery.review["source_original_coverage"])
    assert not result.recovery.packet["placement_eligible"]
    assert not result.host_review["actual_cross_direction_3d_checked"]
    assert result.host_review["after"]["bar_check"]["packet_source_certificates_checked"]
    assert result.recovery.packet["raw_report_sha256"] == hashlib.sha256(result.recovery.normalization_report_bytes).hexdigest()
    assert result.recovery.review["packet_sha256"] == hashlib.sha256(result.recovery.packet_bytes).hexdigest()
    assert result.host_review_bytes == _bytes(result.host_review)


def test_unfit_bars_remain_in_complete_inventory_without_clipping(original, recovered):
    result = run_fit(recovered, original, box_snapshot(0, 1500))
    assert result.recovery.status == "blocked_working_host"
    assert result.host_review["blocked_after"] == 32
    assert result.host_review["accepted_moved_bar_count"] == 0
    assert result.recovery.normalization_report["accepted"] == recovered.normalization_report["accepted"]
    assert "working-host-fit-incomplete" in result.recovery.packet["source_blockers"]
    assert result.recovery.packet["expected"] == recovered.packet["expected"]


def test_source_to_host_translation_is_reversed_without_changing_source_axes(original, recovered):
    first = run_fit(recovered, original)
    shifted = run_fit(recovered, original, box_snapshot(dx=12345, dy=-7654), offset_x_mm=12345, offset_y_mm=-7654)
    assert shifted.recovery.normalization_report["accepted"] == first.recovery.normalization_report["accepted"]
    assert shifted.host_review["blocked_after"] == 0


def test_four_explicit_depths_check_body_cover_but_not_cross_direction_collision(original, recovered):
    depths = {d: 50 for d in ("bottom-X", "bottom-Y", "top-X", "top-Y")}
    result = run_fit(recovered, original, axis_depths_mm=depths, conservative_whole_height=False)
    assert result.host_review["after"]["bar_check"]["totals"]["outside_solid_with_cover"] == 0
    assert result.host_review["check_criterion"] == "outside_solid_with_cover"
    assert not result.host_review["actual_cross_direction_3d_checked"]


@pytest.mark.parametrize("kwargs", [
    {"conservative_whole_height": False}, {"conservative_whole_height": 1},
    {"axis_depths_mm": {}}, {"axis_depths_mm": {}, "conservative_whole_height": False},
    {"offset_x_mm": float("nan")}, {"binding_source": ""}, {"source_report_sha256": "guessed"},
])
def test_no_inferred_profile_or_binding(original, recovered, kwargs):
    with pytest.raises(ValueError):
        run_fit(recovered, original, **kwargs)


def test_mutated_packet_and_forged_hash_chain_do_not_authorize_repair(original, recovered):
    packet = deepcopy(recovered.packet)
    packet["expected"]["physical_bar_count"] -= 1
    for corrupt in (replace(recovered, packet=packet), replace(recovered, packet=packet, packet_bytes=_bytes(packet))):
        with pytest.raises(ValueError):
            run_fit(corrupt, original)


def test_new_same_plane_pair_rejects_only_affected_moves(original, recovered, monkeypatch):
    import rebar.application.physical_host_recovery as app
    calls = 0
    def pairs(_packet):
        nonlocal calls
        calls += 1
        bars = recovered.normalization_report["accepted"]["raw_bars_by_direction"]["bottom-X"]
        return {("bottom-X", bars[0]["id"], bars[1]["id"])} if calls == 2 else set()
    monkeypatch.setattr(app, "_pairs", pairs)
    result = run_fit(recovered, original)
    assert result.host_review["proposal_accepted"] is False
    assert result.host_review["accepted_moved_bar_count"] == 30
    assert result.host_review["blocked_after"] == 2
    assert len(result.host_review["rejected_moved_bar_ids"]) == 2
    assert result.recovery.status == "blocked_working_host"
