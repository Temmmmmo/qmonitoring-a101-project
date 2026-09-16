"""Original four-DXF ingestion and source preference are explicit and reproducible."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from rebar.application import assistant_inputs as scenario
from rebar.application.analyze_plate import PlateDirectionSource


def _candidate(identifier, mass, bars, *, valid=True, missing=0):
    metrics = SimpleNamespace(total_mass_kg=mass, physical_bar_count=bars,
                              under_reinforced_cell_count=missing)
    return SimpleNamespace(id=identifier, solution=SimpleNamespace(valid=valid, metrics=metrics))


def _front(*candidates):
    return SimpleNamespace(front=SimpleNamespace(candidates=candidates))


def test_source_selection_is_not_a_final_mass_or_engineering_gate():
    a, b = _candidate("minimum", 90, 120), _candidate("balanced", 110, 100)
    analysis = _front(a, b, _candidate("invalid", 1, 1, valid=False), _candidate("hole", 2, 1, missing=1))
    assert scenario.select_source_candidate(analysis).id == "minimum"
    assert scenario.select_source_candidate(analysis, maximum_source_bars=100).id == "balanced"
    with pytest.raises(ValueError, match="limits were not relaxed"):
        scenario.select_source_candidate(analysis, maximum_source_bars=100, maximum_source_mass_kg=100)


def test_source_selection_ties_and_empty_front_are_explicit():
    assert scenario.select_source_candidate(_front(_candidate("b", 1, 1), _candidate("a", 1, 1))).id == "a"
    with pytest.raises(ValueError):
        scenario.select_source_candidate(_front())
    with pytest.raises(ValueError):
        scenario.select_source_candidate(SimpleNamespace(front=None))


@pytest.mark.parametrize("kwargs", [{"maximum_source_bars": True}, {"maximum_source_bars": 0},
    {"maximum_source_bars": 1.5}, {"maximum_source_mass_kg": False}, {"maximum_source_mass_kg": float("nan")},
    {"maximum_source_mass_kg": -1}])
def test_invalid_source_constraints_rejected(kwargs):
    with pytest.raises(ValueError):
        scenario.select_source_candidate(_front(_candidate("one", 10, 2)), **kwargs)


@pytest.mark.parametrize("kwargs", [{"population": True}, {"population": 129}, {"population": 3},
    {"generations": 0}, {"generations": 101}, {"seed": -1}, {"seed": 1.1}])
def test_genetic_public_budget_bounds(kwargs):
    with pytest.raises(ValueError):
        scenario.assistant_genetic_config(**kwargs)


def test_genetic_control_matches_uniform_recovery_experiment():
    config = scenario.assistant_genetic_config()
    assert (config.population_size, config.generations, config.random_seed) == (8, 3, 7)
    assert config.operator_policy == "uniform"
    assert config.pool_polish == "milp" and config.pool_polish_solves == 6
    assert config.pool_polish_time_s == 10 and config.recombination_variants == 1500


@pytest.fixture
def source_case(tmp_path, monkeypatch):
    paths = []
    for name in ("bottom-X", "bottom-Y", "top-X", "top-Y"):
        path = tmp_path / (name + ".dxf")
        path.write_bytes(b"DXF parser boundary: " + name.encode())
        paths.append(path)
    sources = tuple(PlateDirectionSource(path, mapping_id="explicit-map") for path in paths)
    # Keep real metric dataclasses for serialization; isolate the expensive GA.
    from rebar.optimization.contracts.plate import PlateMetrics
    metrics = PlateMetrics(direction_count=4, zone_count=4, physical_bar_count=8,
        total_mass_kg=100.0, total_bar_length_mm=40000.0, under_reinforced_cell_count=0,
        demanded_cell_count=4, covered_demanded_cell_count=4, overcovered_cell_count=0,
        overcovered_area_mm2=0.0, objective_value=100.0)
    selected = SimpleNamespace(id="plate:1", solution=SimpleNamespace(valid=True, metrics=metrics))
    analysis = _front(selected)
    analysis.problem = SimpleNamespace(case_id="own-case")
    calls = []

    def fake(sources, **kwargs):
        calls.append((sources, kwargs))
        return analysis

    monkeypatch.setattr(scenario, "analyze_plate", fake)
    return sources, analysis, calls


def test_fresh_four_sources_keep_hashes_and_preserve_stock_profile(source_case):
    sources, _, calls = source_case
    selected = scenario.analyze_assistant_sources(sources, case_id="own-case")
    assert len(selected.provenance["source_files"]) == 4
    assert selected.provenance["candidate_id"] == "plate:1"
    assert selected.provenance["placement_eligible"] is False
    assert calls[0][1]["single_cell_policy"] == "preserve"
    assert calls[0][1]["cutting_profile"] == "plate-11700"
    assert calls[0][1]["min_width_cells"] == 2
    assert calls[0][1]["algorithm_names"] == ("genetic-source-recovery",)
    scenario.verify_source_records(selected.provenance["source_files"])


def test_source_variants_use_one_search_and_preserve_each_candidates_provenance(source_case):
    from dataclasses import replace
    sources, analysis, calls = source_case
    first = analysis.front.candidates[0]
    alternatives = [SimpleNamespace(id='alt-' + str(i), solution=SimpleNamespace(valid=True,
        metrics=replace(first.solution.metrics, total_mass_kg=110+i, physical_bar_count=7-i))) for i in range(3)]
    analysis.front.candidates = (first, *alternatives)
    result = scenario.analyze_assistant_sources(sources, case_id='own-case', maximum_variants=3)
    assert len(calls) == 1
    assert len(result.alternatives) == 2
    assert result.provenance['candidate_id'] == 'plate:1'
    assert result.alternatives[0].provenance['candidate_id'] == 'alt-2'
    assert len({v.provenance['candidate_id'] for v in (result, *result.alternatives)}) == 3
    assert all(not v.provenance['placement_eligible'] for v in result.alternatives)


@pytest.mark.parametrize('value', [True, 0, 6, 1.5])
def test_variant_budget_rejected_before_search(source_case, value):
    sources, _, calls = source_case
    with pytest.raises(ValueError, match='maximum_variants'):
        scenario.analyze_assistant_sources(sources, case_id='own-case', maximum_variants=value)
    assert not calls


def test_source_drift_during_optimizer_is_rejected(source_case, monkeypatch):
    sources, analysis, _ = source_case

    def changed(*_a, **_kw):
        sources[0].dxf_path.write_bytes(b"modified externally")
        return analysis

    monkeypatch.setattr(scenario, "analyze_plate", changed)
    with pytest.raises(ValueError, match="Source changed"):
        scenario.analyze_assistant_sources(sources, case_id="own-case")


@pytest.mark.parametrize("change", ["duplicate", "missing", "empty_case"])
def test_invalid_source_list_never_starts_ga(source_case, change):
    sources, _, calls = source_case
    if change == "duplicate":
        sources = (*sources[:3], sources[0])
    elif change == "missing":
        sources = sources[:3]
    with pytest.raises(ValueError):
        scenario.analyze_assistant_sources(sources, case_id="" if change == "empty_case" else "case")
    assert not calls


def test_shared_legend_is_hashed_once(source_case, tmp_path):
    sources, _, _ = source_case
    shk = tmp_path / "общая.shk"
    shk.write_text("source legend boundary", encoding="utf-8")
    selected = scenario.analyze_assistant_sources(tuple(PlateDirectionSource(s.dxf_path,
        shk_path=shk) for s in sources), case_id="own-case")
    assert len(selected.provenance["source_files"]) == 5
    before = deepcopy(selected.provenance)
    scenario.verify_source_records(selected.provenance["source_files"])
    assert selected.provenance == before


@pytest.mark.parametrize("change", ["none", "wrong_pdf_totals", "pdf_drift"])
def test_known_case_checks_pdf_and_uses_explicit_matching_mapping(source_case, tmp_path, monkeypatch, change):
    sources, _, _ = source_case
    case = scenario.get_engineer_reference_case("k09-typical-3-14")
    pdf = tmp_path / "engineer.pdf"
    pdf.write_bytes(b"PDF extraction boundary")
    files = SimpleNamespace(engineer_pdf=pdf,
        dxf_by_input_set={case.input_sets[0].id: {str(i): s.dxf_path for i, s in enumerate(sources)}})
    monkeypatch.setattr(scenario, "resolve_engineer_reference_files", lambda *_a: files)
    reference = SimpleNamespace(total_mass_kg=case.expected_mass_kg + (1 if change == "wrong_pdf_totals" else 0),
        bar_count=case.expected_bar_count, position_count=case.expected_position_count)
    monkeypatch.setattr(scenario, "extract_engineer_reference_metrics", lambda *_a: reference)
    original = scenario.analyze_assistant_sources
    seen = []

    def analyze(values, **kwargs):
        seen.append((values, kwargs))
        if change == "pdf_drift":
            pdf.write_bytes(b"PDF modified while optimizing")
        # The expensive algorithm is already isolated by source_case; this limit
        # is checked independently here before restoring the small fixture count.
        assert kwargs["maximum_source_bars"] == case.expected_bar_count
        return original(values, **kwargs)

    monkeypatch.setattr(scenario, "analyze_assistant_sources", analyze)
    if change != "none":
        with pytest.raises(ValueError, match="PDF totals|Source changed"):
            scenario.analyze_assistant_case(case.id, tmp_path)
        assert bool(seen) == (change == "pdf_drift")
        return
    selected = scenario.analyze_assistant_case(case.id, tmp_path)
    assert selected.engineer_reference["mass_kg"] == case.expected_mass_kg
    assert selected.provenance["mode"] == "fresh-matched-case"
    assert len(selected.provenance["source_files"]) == 5
    assert all(source.mapping_id == scenario.CASE_MAPPINGS[case.id] for source in seen[0][0])
