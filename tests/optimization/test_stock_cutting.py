from dataclasses import replace
from types import SimpleNamespace

import pytest

from rebar.optimization.services.bar_schedule import BarScheduleGroup, build_bar_schedule
from rebar.optimization.services.stock_cutting import check_stock_cutting


def schedule(*pairs, steel_class="A500", diameter=18):
    return build_bar_schedule(BarScheduleGroup(str(i), diameter, length, count, steel_class)
                              for i, (length, count) in enumerate(pairs))


@pytest.mark.parametrize("pairs,stocks", [(((3900, 9),), 3), (((4875, 2), (6825, 2)), 2),
                                        (((11700, 1), (5850, 2), (2925, 4)), 3)])
def test_exact_batch_every_piece_once_no_extra_bars(pairs, stocks):
    report = check_stock_cutting(schedule(*pairs))
    assert report["status"] == "pass" and report["stock_bar_count"] == stocks
    assert report["waste_mm"] == 0 and report["extra_uninstalled_pieces"] == 0
    assert not report["geometry_changed"] and report["kerf_mm"] == 0
    counts = {}
    for group in report["groups"]:
        for pattern in group["patterns"]:
            assert sum(c["length_mm"] * c["pieces_per_stock_bar"] for c in pattern["cuts"]) == 11700
            for cut in pattern["cuts"]:
                counts[cut["length_mm"]] = counts.get(cut["length_mm"], 0) + cut["pieces_per_stock_bar"] * pattern["stock_bar_count"]
    assert counts == dict(pairs)


@pytest.mark.parametrize("pairs,reason", [(((1460, 8),), "batch_total_not_multiple_of_stock"),
    (((1670, 7),), "batch_total_not_multiple_of_stock"), (((3900, 4),), "batch_total_not_multiple_of_stock"),
    (((7800, 3),), "no_exact_cut_pattern"), (((11701, 1),), "installed_bar_exceeds_stock_or_length_resolution")])
def test_catalog_lengths_or_total_divisibility_alone_do_not_prove_zero_waste(pairs, reason):
    result = check_stock_cutting(schedule(*pairs))
    assert result["status"] == "fail" and result["groups"][0]["reason"] == reason
    assert result["waste_mm"] is None and result["stock_bar_count"] is None


def test_diameters_and_classes_never_mix_and_unknown_class_is_not_approved():
    rows = build_bar_schedule([BarScheduleGroup("a", 18, 4875, 1, "A500"),
                               BarScheduleGroup("b", 25, 6825, 1, "A500")])
    assert check_stock_cutting(rows)["status"] == "fail"
    rows = build_bar_schedule([BarScheduleGroup("a", 18, 4875, 1, "A500"),
                               BarScheduleGroup("b", 18, 6825, 1, "A400")])
    assert check_stock_cutting(rows)["status"] == "fail"
    assert check_stock_cutting(schedule((3900, 3), steel_class=""))["status"] == "not_checked"


def test_search_resource_limit_is_not_infeasibility():
    result = check_stock_cutting(schedule((3900, 3), (5850, 2)), maximum_nodes=1)
    assert result["status"] == "not_checked" and result["groups"][0]["reason"] == "pattern_search_limit"


@pytest.mark.parametrize("x", [None, [0.5], [0], [2], [float("nan")], [-1], [1, 0]])
def test_forged_or_incomplete_solver_vector_is_not_accepted(monkeypatch, x):
    import numpy as np
    import scipy.optimize

    monkeypatch.setattr(scipy.optimize, "milp", lambda *a, **k: SimpleNamespace(status=1, x=None if x is None else np.array(x)))
    assert check_stock_cutting(schedule((3900, 3)))["status"] == "not_checked"


@pytest.mark.parametrize("field,value", [("shape", "bent"), ("physical_bar_count", True),
                                         ("physical_bar_count", 0), ("length_mm", float("inf"))])
def test_invalid_schedule_rejected(field, value):
    row = schedule((3900, 3))[0]
    with pytest.raises(ValueError):
        check_stock_cutting((replace(row, **{field: value}),))


def test_empty_additional_demand_does_not_invent_stock():
    result = check_stock_cutting(())
    assert result["status"] == "pass" and result["stock_bar_count"] == 0
