import hashlib
import importlib
import json
from pathlib import Path

from rebar.application.analyze_composite_plate import analyze_composite_plate
from test_analyze_composite_plate import settings
from test_composite_host_review import reference_sample


def test_experiment_keeps_all_zones_and_rechecks_full_source(composite_plate_sources, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    experiment = importlib.import_module("experiment_host_stock_fit").experiment
    analysis = analyze_composite_plate(composite_plate_sources, settings(), maximum_candidates=32,
        solver_time_limit_s=1, cutting_profile="plate-11700-batch", maximum_cutting_overhead_pct=0)
    assert analysis["front"]
    digest = hashlib.sha256(json.dumps(analysis).encode()).hexdigest()
    result = experiment(analysis, reference_sample(), composite_plate_sources[0].dxf_path.parent, 0, digest)
    assert not result["placement_eligible"]
    assert len(result["directions"]) == 4
    assert sum(d["zone_count"] for d in result["directions"]) == analysis["front"][0]["zone_count"]
    assert sum(d["physical_bar_count"] for d in result["directions"]) == analysis["front"][0]["physical_bar_count"]
    assert all(d["coverage_and_metrics_unchanged"] for d in result["directions"])
