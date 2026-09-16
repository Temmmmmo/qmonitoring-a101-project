from copy import deepcopy

from rebar.application.layout_variants import combine_layout_variants, build_layout_variants
from types import SimpleNamespace
import pytest


def _report(mass, bars, positions, offset):
    point = {'additional_mass_kg': mass, 'physical_bar_count': bars, 'position_count': positions,
             'direction_candidate_indexes': [0]*4}
    return {'schema_version': 'composite-plate-analysis/v1', 'selected_index': 0, 'front': [point],
        'directions': [{'candidates': [{'physical_bars': [{'id': str(offset), 'diameter_mm': 12,
             'steel_class': 'A500', 'coordinate_mm': offset, 'longitudinal_mm': [0, 1000]}]}]} for _ in range(4)],
        'source_graphics': {'marker': offset}, 'blocking_check_ids': ['check-' + str(offset)],
        'graphic_bar_plan_draft': {'marker': offset}, 'placement_eligible': False}


def test_complete_variants_remain_independent_and_labels_use_final_not_source_metrics():
    reports = [_report(100, 20, 8, 0), _report(120, 10, 9, 100), _report(130, 15, 4, 200)]
    before = deepcopy(reports)
    result = combine_layout_variants(reports)
    variants = result['layout_variants']
    assert len(variants) == 3 and len({v['geometry_sha256'] for v in variants}) == 3
    assert [v['label'] for v in variants] == ['Меньше массы', 'Меньше стержней', 'Меньше позиций']
    assert variants[0]['report'] is None
    assert variants[1]['report'] == reports[1]
    assert variants[2]['report']['blocking_check_ids'] == ['check-200']
    result['layout_variants'][1]['report']['source_graphics']['marker'] = 'changed'
    assert reports == before


def test_duplicate_bar_geometry_is_not_a_variant_even_with_different_ids_and_checks():
    first = _report(100, 20, 8, 0)
    duplicate = deepcopy(first)
    duplicate['directions'][0]['candidates'][0]['physical_bars'][0]['id'] = 'different-source'
    duplicate['directions'][0]['candidates'][0]['physical_bars'][0]['provenance'] = {'different': True}
    assert len(combine_layout_variants([first, duplicate])['layout_variants']) == 1


def test_incomplete_candidate_not_relabelled_as_complete_variant():
    first = _report(100, 20, 8, 0)
    failed = {'front': [], 'selected_index': None}
    assert len(combine_layout_variants([failed, first])['layout_variants']) == 1
    assert combine_layout_variants([failed]) == failed


def test_rejected_and_duplicate_candidates_backfilled_without_erasing_incumbent():
    selections = tuple(SimpleNamespace(provenance={'candidate_id': str(i)}) for i in range(5))
    first = _report(100, 20, 8, 0)
    payloads = [first, ValueError('explicit failed coverage'), deepcopy(first),
                _report(120, 10, 9, 100), _report(130, 15, 4, 200)]
    calls = []
    def evaluate(selection):
        index = int(selection.provenance['candidate_id'])
        calls.append(index)
        if isinstance(payloads[index], ValueError):
            raise payloads[index]
        return payloads[index]
    result = build_layout_variants(selections, evaluate)
    assert calls == [0, 1, 2, 3, 4]
    assert len(result['layout_variants']) == 3
    assert result['variant_rejections'] == [{'candidate_id': '1', 'reason': 'explicit failed coverage'}]
    assert result['source_variant_budget'] == 5


def test_no_successful_candidate_still_fails_instead_of_fabricating_result():
    def fail(selection):
        raise ValueError('native contour is incompatible')
    with pytest.raises(ValueError, match='native contour is incompatible'):
        build_layout_variants((SimpleNamespace(provenance={'candidate_id': 'first'}),), fail)


@pytest.mark.parametrize('incomplete', [{'front': []}, {'front': [{}], 'selected_index': None},
    {'front': [{'direction_candidate_indexes': [0]}], 'selected_index': 0,
     'directions': [{'candidates': [{}]}]}])
def test_incomplete_evaluation_is_rejected_not_presented_as_physical_variant(incomplete):
    selection = SimpleNamespace(provenance={'candidate_id': 'first'})
    with pytest.raises(ValueError, match='Complete physical geometry not returned'):
        build_layout_variants((selection,), lambda _: incomplete)
