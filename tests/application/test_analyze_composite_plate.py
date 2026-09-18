from dataclasses import replace
import json

import pytest

from rebar.application.analyze_composite_plate import CompositeDirectionSettings, analyze_composite_plate
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS
from rebar.optimization.services.composite_mesh_domain import composite_mesh_domain
from rebar.optimization.adapters.mosaic import build_demand_map
from rebar.application.analyze_direction import load_direction_mosaic
from shapely.geometry import shape


def settings():
    return tuple(CompositeDirectionSettings(d, 0, 150, 50, "A500", "Synthetic fixture only") for d in PLATE_DIRECTIONS)


def test_full_dxf_plate_keeps_all_components_and_unions_positions(composite_plate_sources):
    sources = composite_plate_sources
    before = [source.dxf_path.read_bytes() for source in sources]
    progress = []
    report = analyze_composite_plate(sources, settings(), maximum_candidates=32, solver_time_limit_s=2,
                                     cutting_profile="continuous", progress_callback=lambda percent, stage: progress.append((percent, stage)))
    assert [percent for percent, _ in progress] == sorted(percent for percent, _ in progress)
    assert [percent for percent, stage in progress if stage.startswith("Читаем DXF")] == [0, 5, 10, 15]
    assert [percent for percent, stage in progress if stage.startswith("Считаны DXF")] == [5, 10, 15, 20]
    assert [percent for percent, stage in progress if stage.startswith("Найдены варианты")] == [35, 45, 55, 65]
    assert progress[-1][1] == "Расчёт готов, формируем отчёт"
    assert report["direction_count"] == 4 and report["front"]
    assert report["source_demand_preserved"] and not report["placement_eligible"]
    assert [source.dxf_path.read_bytes() for source in sources] == before
    assert report["averaging"] == "not_applied"
    assert report["zone_boundary_policy"] == "zone_footprints_clipped_to_union_of_source_kleenka_cells"
    assert "stock-cutting-manufacturing-assumptions" in report["blocking_check_ids"]
    assert all("Исходный спрос; раскладка отсутствует" in d["input_svg"] for d in report["directions"])
    for point in report["front"]:
        options = [report["directions"][i]["candidates"][j] for i, j in enumerate(point["direction_candidate_indexes"])]
        types = {(p["shape"], p["steel_class"], p["diameter_mm"], p["length_mm"]) for c in options for p in c["bar_schedule"]}
        assert point["position_count"] == len(types) == len(point["bar_schedule"])
        assert point["physical_bar_count"] == sum(c["metrics"]["physical_bar_count"] for c in options)
        assert point["additional_mass_kg"] == pytest.approx(sum(c["metrics"]["additional_mass_kg"] for c in options))
        assert all(c["coverage"]["uncovered_cell_count"] == 0 for c in options)
        assert all(len(z["components"]) == 2 for c in options for z in c["zone_drafts"])
        assert all(z["components"][0]["placement"]["pattern"]["offsets_mm"] == [100, 200] for c in options for z in c["zone_drafts"])
        assert all("<svg" in c["svg"] for c in options)
        for source, option in zip(sources, options):
            mesh = composite_mesh_domain(build_demand_map(load_direction_mosaic(source.dxf_path,
                shk_path=source.shk_path, mapping_id=source.mapping_id)))
            assert len(option["zone_footprints"]) == len(option["zone_drafts"])
            assert all(mesh.covers(shape(zone["geometry_mm"])) for zone in option["zone_footprints"])
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("bad", ["three", "duplicate", "settings", "mesh", "host", "width", "position_limit"])
def test_bad_full_plate_never_becomes_a_partial_success(composite_plate_sources, bad):
    sources, profile = composite_plate_sources, settings()
    kwargs = {}
    if bad == "three":
        sources = sources[:3]
    elif bad == "duplicate":
        sources = (*sources[:3], sources[0])
    elif bad == "settings":
        profile = profile[:3]
    elif bad == "mesh":
        import ezdxf
        doc = ezdxf.readfile(sources[0].dxf_path)
        face = doc.modelspace().query('3DFACE[layer=="KLEENKA"]')[0]
        doc.modelspace().delete_entity(face)
        doc.saveas(sources[0].dxf_path)
    elif bad == "host":
        kwargs["coordinate_policy"] = "guessed"
    elif bad == "width":
        kwargs["min_width_cells"] = 1
    else:
        kwargs["maximum_positions"] = True
    with pytest.raises(ValueError):
        analyze_composite_plate(sources, profile, maximum_candidates=1, solver_time_limit_s=1, **kwargs)


def test_unsolved_direction_cannot_produce_three_direction_mass(composite_plate_sources):
    report = analyze_composite_plate(composite_plate_sources, settings(), maximum_candidates=1, solver_time_limit_s=1)
    assert not report["front"] and report["selected_index"] is None
    assert report["status"] == "no_full_plate_solution_found"
    assert "full-plate-solution-not-found" in report["blocking_check_ids"]


@pytest.mark.parametrize("field,value", [("background_origin_mm", None), ("first_300_offset_mm", True),
    ("second_offset_mm", float("nan")), ("source", ""), ("steel_class", 0)])
def test_no_default_phases_or_class_coercion(field, value):
    with pytest.raises(ValueError):
        replace(settings()[0], **{field: value})


def test_batch_profile_rebuilds_every_direction_and_does_not_mix_failed_cutting_front(composite_plate_sources):
    report = analyze_composite_plate(composite_plate_sources, settings(), maximum_candidates=32, solver_time_limit_s=5,
        cutting_profile="plate-11700-batch", maximum_cutting_overhead_pct=100)
    assert report["front"] and report["length_balance_attempts"]
    assert report["front_scope"] == "zero-waste-candidates"
    assert all(point["stock_cutting"]["status"] == "pass" for point in report["front"])
    assert report["diagnostic_front_before_cutting"] and not report["placement_eligible"]
    assert all(a["telemetry"]["geometry_checked"] for a in report["length_balance_attempts"] if a["status"] == "balanced")
    for point in report["front"]:
        options = [report["directions"][i]["candidates"][j] for i, j in enumerate(point["direction_candidate_indexes"])]
        assert all(c["coverage"]["uncovered_cell_count"] == 0 for c in options)
        assert point["physical_bar_count"] == sum(c["metrics"]["physical_bar_count"] for c in options)
        assert point["additional_mass_kg"] == pytest.approx(sum(c["metrics"]["additional_mass_kg"] for c in options))
        assert point["additional_mass_kg"] <= point["mass_before_balancing_kg"] * 2 + 1e-6
        assert len(point["bar_schedule"]) == point["position_count"]


@pytest.mark.parametrize("obstacle", ["outer_edge", "opening"])
def test_host_failure_keeps_every_direction_and_never_removes_edge_or_opening_demand(composite_plate_sources, obstacle):
    from rebar.application.composite_host_review import HOST_COORDINATE_POLICY
    from test_composite_host_review import rectangle, reference_sample

    reference = reference_sample()
    floor = reference["floor"]
    if obstacle == "outer_edge":
        floor["bbox_mm"] = {"min_mm": [0, 0, -300], "max_mm": [6000, 4000, 0]}
    for side, z in (("top", 0), ("bottom", -300)):
        loops = floor[side + "_faces"][0]["edge_loops"]
        if obstacle == "outer_edge":
            loops[:] = [rectangle((0, 0, 6000, 4000), z)]
        else:
            loops.append(rectangle((1000, 1000, 3000, 3000), z))
    report = analyze_composite_plate(composite_plate_sources, settings(), maximum_candidates=32, solver_time_limit_s=1,
        host_reference=reference, coordinate_policy=HOST_COORDINATE_POLICY)
    assert report["source_demand_preserved"] and not report["front"]
    assert len(report["directions"]) == 4 and not report["placement_eligible"]
    assert all(d["source_cell_count"] == 96 for d in report["directions"])
    from xml.etree import ElementTree

    for direction in report["directions"]:
        drawing = ElementTree.fromstring(direction["input_svg"])
        cells = drawing.findall(".//{*}polygon")
        assert len(cells) == 96  # Failed demand remains visible, not just the empty result.
        if obstacle == "outer_edge":
            actual = {int(cell.attrib["data-cell-id"]) for cell in cells if "stroke" in cell.attrib}
            expected = {cell["cell_id"] for cell in direction["telemetry"]["host_demand_feasibility"]["cells"]}
            assert actual == expected and actual
        else:
            assert len(drawing.findall(".//{*}rect")) == 2  # Host + the actual opening.
    if obstacle == "outer_edge":
        assert all(d["telemetry"]["host_demand_feasibility"]["requires_engineering_decision"] for d in report["directions"])
    else:
        # The initial impossibility proof concerns longitudinal OUTER edges only;
        # openings are checked against the actual candidate axes, not that proof.
        assert all(d["telemetry"]["host_rejected_candidates"] > 0 for d in report["directions"])


def test_sto_contact_note_is_exported_outside_the_unchanged_revit_v2_packet(composite_plate_sources):
    import struct

    labels = [b"s300d18"] + [f"s300d18+s100d{d}".encode() for d in (20, 22, 25, 28, 32)]
    composite_plate_sources[0].shk_path.write_bytes(b"".join(
        struct.pack("<ffHB", i + 1, i + 2, i, len(label)) + label for i, label in enumerate(labels)))
    report = analyze_composite_plate(composite_plate_sources, settings(), maximum_candidates=32, solver_time_limit_s=2)
    assert report["front"] and "coplanar-background-contact-and-depths" in report["blocking_check_ids"]
    for direction in report["directions"]:
        for candidate in direction["candidates"]:
            assert candidate["installation_notes"]
            assert all("2.7.9" in note["note"] for note in candidate["installation_notes"])
            assert all("placement_note" not in c for zone in candidate["zone_drafts"] for c in zone["components"])


@pytest.mark.parametrize("overhead", [None, True, "5", float("nan"), -1, 101])
def test_invalid_cutting_overhead_is_rejected_before_loading_inputs(composite_plate_sources, overhead):
    with pytest.raises(ValueError, match="прироста массы"):
        analyze_composite_plate(composite_plate_sources, settings(), maximum_cutting_overhead_pct=overhead)
