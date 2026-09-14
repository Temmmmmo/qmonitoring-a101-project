"""Exact strip projections, source ownership and conservative/explicit-Z modes."""

from dataclasses import replace
import math
import random

import pytest
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.ops import unary_union

from rebar.models import Axis, Direction, Layer
from rebar.optimization.contracts.physical import PhysicalBar, PhysicalSourceBar
from rebar.optimization.services.physical_host_fit import (
    PhysicalHostFitLimitError, fit_physical_bar_to_solid_host,
)
from rebar.optimization.services.solid_host import OrthogonalSolidHost, SolidHostSection


def _host(footprint=None, *, upper=None, side=25, top=25, bottom=25):
    footprint = footprint if footprint is not None else box(0, 0, 6000, 6000)
    sections = ((SolidHostSection(0, 200, footprint),) if upper is None else
                (SolidHostSection(0, 100, footprint), SolidHostSection(100, 200, upper)))
    return OrthogonalSolidHost(sections, top, bottom, side,
        sum(s.footprint.area * (s.top_z_mm - s.bottom_z_mm) for s in sections), 6)


def _bar(*, axis=Axis.X, start=0, length=1950, required=(1000, 1500), coordinate=100, diameter=10):
    direction = Direction(Layer.TOP, axis)
    source = PhysicalSourceBar("owner", direction, "A500", diameter, coordinate,
                               (start, start + length), required, 10, 0)
    bar = PhysicalBar("physical", direction, "A500", diameter, coordinate,
                      (start, start + length), (source.id,))
    return bar, (source,)


def _fit(bar, sources, host, **kwargs):
    return fit_physical_bar_to_solid_host(bar, sources, host, conservative_whole_height=True, **kwargs)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_nearest_whole_bar_fit_keeps_length_diameter_axes_mass_and_source_40d(axis):
    bar, sources = _bar(axis=axis)
    result = _fit(bar, sources, _host())
    assert result.status == "fitted" and result.shift_mm == 25
    assert result.admissible_start_intervals_mm == ((25, 600),)
    assert result.bar.installed_interval_mm == (25, 1975)
    assert not result.containment_before and result.containment_after
    assert result.bar.installed_length_mm == bar.installed_length_mm
    assert replace(result.bar, installed_interval_mm=bar.installed_interval_mm) == bar
    assert result.original_bar == bar and sources[0].installed_interval_mm == (0, 1950)
    assert result.full_new_diameter_40d_preserved
    assert not result.global_collisions_checked and not result.placement_eligible
    assert result.outside_solid_volume_after_mm3 is None


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
@pytest.mark.parametrize("opening", ("hole", "notch"))
def test_orthogonal_hole_or_nonrectangular_outer_uses_full_width_strip(axis, opening):
    obstacle = box(2000, 80 if opening == "hole" else 0, 2500, 120)
    footprint = box(0, 0, 6000, 6000).difference(obstacle)
    if axis is Axis.Y:
        from shapely.affinity import affine_transform
        footprint = affine_transform(footprint, (0, 1, 1, 0, 0, 0))
    bar, sources = _bar(axis=axis, start=600)
    result = _fit(bar, sources, _host(footprint))
    assert result.status == "fitted" and result.shift_mm == -575
    assert result.host_start_intervals_mm == ((25, 25), (2525, 4025))
    assert result.admissible_start_intervals_mm == ((25, 25),)
    assert result.containment_after


def test_hole_between_endpoints_is_not_missed_and_required_demand_is_not_clipped():
    host = _host(box(0, 0, 6000, 6000).difference(box(2000, 80, 2500, 120)))
    bar, sources = _bar(start=1000, required=(1900, 2100))
    result = _fit(bar, sources, host)
    assert result.status == "blocked"
    assert result.bar is bar and result.shift_mm == 0
    assert not result.admissible_start_intervals_mm
    assert result.blocked_reason == "no-host-position-preserving-length-and-full-new40d"
    assert not result.containment_before and not result.containment_after
    assert result.source_reference_count == 1


def test_tiny_positive_hole_is_not_discarded_by_area_filter_or_endpoint_sampling():
    # Its area is below the independent numerical area tolerance, but the exact
    # construction must still retain the hole's exclusion interval.
    host = _host(box(0, 0, 6000, 6000).difference(box(2000, 99.999999, 2000.000001, 100.000001)))
    bar, sources = _bar(start=1000, required=(1900, 2100))
    result = _fit(bar, sources, host)
    assert result.status == "blocked"
    assert result.host_start_intervals_mm == ((25, 25), (2025.000001, 4025))


def test_transverse_tangency_to_hole_boundary_is_allowed_without_sampling():
    host = _host(box(0, 0, 6000, 6000).difference(box(2000, 130, 2500, 180)))
    bar, sources = _bar(start=600)
    result = _fit(bar, sources, host)
    assert result.status == "unchanged" and result.bar == bar
    assert result.containment_before and result.containment_after


def test_ledge_requires_relevant_z_sections_or_explicit_whole_height_intersection():
    bottom = box(0, 0, 6000, 6000)
    top = bottom.difference(box(2000, 0, 2500, 120))
    host = _host(bottom, upper=top)
    bar, sources = _bar(start=600)
    low = fit_physical_bar_to_solid_host(bar, sources, host, axis_z_mm=50)
    high = fit_physical_bar_to_solid_host(bar, sources, host, axis_z_mm=150)
    common = _fit(bar, sources, host)
    assert low.status == "unchanged" and low.checked_section_indexes == (0,)
    assert high.status == "fitted" and high.checked_section_indexes == (1,)
    assert common.status == "fitted" and common.checked_section_indexes == (0, 1)
    assert low.outside_solid_volume_after_mm3 == high.outside_solid_volume_after_mm3 == 0
    assert common.outside_solid_volume_after_mm3 is None
    assert not low.actual_3d_placement_approved and not common.actual_3d_placement_approved


def test_top_bottom_covers_participate_in_z_section_selection():
    bottom = box(0, 0, 6000, 6000)
    host = _host(bottom, upper=bottom.difference(box(2000, 0, 2500, 120)), top=40, bottom=10)
    bar, sources = _bar(start=600)
    result = fit_physical_bar_to_solid_host(bar, sources, host, axis_z_mm=80)
    assert result.checked_section_indexes == (0, 1)  # envelope Z=65..125
    assert result.status == "fitted" and result.bar.installed_interval_mm == (25, 1975)


def test_outside_z_returns_original_with_explicit_diagnostics():
    bar, sources = _bar(start=25)
    result = fit_physical_bar_to_solid_host(bar, sources, _host(), axis_z_mm=20)
    assert result.status == "blocked" and result.bar is bar
    assert result.blocked_reason == "vertical-envelope-outside-host"
    assert result.outside_solid_volume_after_mm3 > 0


def test_disconnected_sections_have_no_fictitious_bbox_bridge():
    shape = MultiPolygon((box(0, 0, 1800, 2000), box(2200, 0, 6000, 2000)))
    bar, sources = _bar(start=1000, required=(1900, 2100))
    result = _fit(bar, sources, _host(shape))
    assert result.status == "blocked"
    assert result.host_start_intervals_mm == ((2225, 4025),)


def test_all_source_owners_and_NEW_diameter_40d_are_preserved():
    bar, sources = _bar(start=500, length=2340, required=(1000, 2000))
    source2 = replace(sources[0], id="owner-two", required_interval_mm=(1100, 1900))
    bar = replace(bar, diameter_mm=12, source_bar_ids=(sources[0].id, source2.id))
    result = _fit(bar, (*sources, source2), _host())
    assert result.source_start_window_mm == (140, 520)
    assert result.source_reference_count == 2 and result.bar.diameter_mm == 12
    assert result.bar.source_bar_ids == bar.source_bar_ids and result.containment_after
    with pytest.raises(ValueError, match="NEW-diameter 40d"):
        _fit(replace(bar, installed_interval_mm=(600, 2940)), (*sources, source2), _host())


def test_unfit_stock_length_is_never_shortened_to_host_extent():
    bar, sources = _bar(start=0, length=5850, required=(2000, 3000))
    result = _fit(bar, sources, _host(box(0, 0, 5800, 3000)))
    assert result.status == "blocked" and result.bar.installed_length_mm == 5850
    assert result.bar is bar


@pytest.mark.parametrize("kwargs", ({}, {"axis_z_mm": 100, "conservative_whole_height": True},
    {"axis_z_mm": True}, {"axis_z_mm": math.nan}, {"conservative_whole_height": 1}))
def test_no_implicit_z_or_implicit_conservative_mode(kwargs):
    bar, sources = _bar()
    with pytest.raises(ValueError):
        fit_physical_bar_to_solid_host(bar, sources, _host(), **kwargs)


@pytest.mark.parametrize("bad", ("missing", "duplicate", "axis", "class", "short_source", "source_touch"))
def test_corrupt_or_changed_source_certificate_is_rejected(bad):
    bar, sources = _bar()
    if bad == "missing":
        bar = replace(bar, source_bar_ids=("unknown",))
    elif bad == "duplicate":
        sources = sources * 2
    elif bad == "axis":
        bar = replace(bar, transverse_axis_mm=101)
    elif bad == "class":
        bar = replace(bar, steel_class="A400")
    elif bad == "short_source":
        sources = (replace(sources[0], installed_interval_mm=(900, 1600)),)
    else:
        bar = replace(bar, transverse_axis_mm=10, diameter_mm=12)
        sources = (replace(sources[0], transverse_axis_mm=10),)
    with pytest.raises(ValueError):
        _fit(bar, sources, _host())


def test_unsupported_host_geometry_and_resource_exhaustion_do_not_become_bbox_pass():
    bar, sources = _bar()
    with pytest.raises(ValueError, match="axis aligned"):
        _fit(bar, sources, _host(Polygon(((0, 0), (6000, 0), (6000, 6000), (1, 6000)))))
    with pytest.raises(PhysicalHostFitLimitError, match="vertex"):
        _fit(bar, sources, _host(), maximum_vertices=3)
    holes = unary_union((box(2000, 80, 2100, 120), box(2300, 80, 2400, 120)))
    with pytest.raises(PhysicalHostFitLimitError, match="interval"):
        _fit(bar, sources, _host(box(0, 0, 6000, 6000).difference(holes)), maximum_intervals=1)


def test_interval_membership_matches_independent_polygon_envelopes_on_random_orthogonal_hosts():
    rng = random.Random(20260914)
    for trial in range(30):
        outer = box(0, 0, 10000, 6000)
        holes = []
        for _ in range(8):
            left = rng.randrange(1000, 9000, 100)
            lower = rng.choice((0, 80, 130, 200))
            holes.append(box(left, lower, left + 50, lower + 40))
        material = outer.difference(unary_union(holes))
        bar, sources = _bar(start=600)
        result = _fit(bar, sources, _host(material))
        starts = [rng.uniform(25, 600) for _ in range(30)]
        starts.extend(v for interval in result.admissible_start_intervals_mm for v in interval)
        for start in starts:
            envelope = box(start - 25, 70, start + 1950 + 25, 130)
            actual = material.covers(envelope)
            listed = any(a <= start <= b for a, b in result.admissible_start_intervals_mm)
            assert actual == listed, (trial, start, result.admissible_start_intervals_mm)


@pytest.mark.parametrize("field,value", (("id", []), ("id", " "),
    ("transverse_axis_mm", True), ("direction", Direction("top", Axis.X))))
def test_invalid_typed_source_values_are_explicitly_rejected(field, value):
    bar, sources = _bar(coordinate=1 if field == "transverse_axis_mm" else 100)
    sources = (replace(sources[0], **{field: value}),)
    with pytest.raises(ValueError, match="source|original"):
        _fit(bar, sources, _host())


@pytest.mark.parametrize("field,value", (("volume_mm3", True), ("volume_mm3", math.nan),
    ("volume_mm3", 1), ("face_count", True), ("face_count", 5),
    ("top_cover_mm", 175)))
def test_inconsistent_typed_host_metadata_does_not_bypass_complete_solid_checks(field, value):
    bar, sources = _bar()
    with pytest.raises(ValueError, match="host"):
        _fit(bar, sources, replace(_host(), **{field: value}))
