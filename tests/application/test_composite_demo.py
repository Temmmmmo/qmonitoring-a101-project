import importlib
from pathlib import Path

import pytest

from rebar.application.analyze_direction import load_direction_mosaic
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS

demo = importlib.import_module("rebar.application.composite_demo")


def test_public_demo_uses_real_four_direction_service_and_cleans_temporary_sources(monkeypatch):
    paths, patterns = [], []
    analyze = demo.analyze_composite_plate

    def inspect(sources, settings, **kwargs):
        assert len(sources) == len(settings) == 4
        assert kwargs["maximum_cutting_overhead_pct"] == 5
        assert "host_reference" not in kwargs
        for source, direction in zip(sources, PLATE_DIRECTIONS):
            paths.extend((source.dxf_path, source.shk_path))
            assert source.dxf_path.name.startswith("DEMO_composite_")
            mosaic = load_direction_mosaic(source.dxf_path, shk_path=source.shk_path)
            assert mosaic.direction == direction and len(mosaic.cells) == 96
            patterns.append(tuple(cell.aci for cell in mosaic.cells))
        return analyze(sources, settings, **kwargs)

    monkeypatch.setattr(demo, "analyze_composite_plate", inspect)
    report = demo.analyze_composite_demo()
    assert len(set(patterns)) == 4  # Same mesh, not four copies of one demand map.
    assert all(not path.exists() for path in paths)
    assert report["demo"]["source_kind"] == "synthetic" and not report["demo"]["project_parameters"]
    assert report["front"] and len(report["directions"]) == 4 and not report["placement_eligible"]
    assert report["source_demand_preserved"] and report["host_envelope"] is None
    assert "ДЕМОНСТРАЦИЯ" in report["warning"]
    for point in report["front"]:
        candidates = [d["candidates"][j] for d, j in zip(report["directions"], point["direction_candidate_indexes"])]
        assert all(c["coverage"]["uncovered_cell_count"] == 0 for c in candidates)
        assert all(len(z["components"]) == 2 for c in candidates for z in c["zone_drafts"])
        assert point["additional_mass_kg"] == pytest.approx(sum(c["metrics"]["additional_mass_kg"] for c in candidates))


def test_demo_cleans_temporary_directory_on_failure(monkeypatch):
    roots = []

    def fail(folder):
        roots.append(Path(folder))
        raise ValueError("failure during generation")

    monkeypatch.setattr(demo, "_write_sources", fail)
    with pytest.raises(ValueError, match="during generation"):
        demo.analyze_composite_demo()
    assert roots and not roots[0].exists()
