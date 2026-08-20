"""Проверка переносимого локального отчёта."""

import json

from rebar.reporting import generate_comparison_report


def test_report_embeds_svg_and_exports_common_json(tmp_path, splittable_mosaic):
    report = generate_comparison_report(
        splittable_mosaic,
        tmp_path,
        max_details=2,
        min_width_cells=1,
    )

    document = report.read_text(encoding="utf-8")
    payload = json.loads((tmp_path / "solutions.json").read_text(encoding="utf-8"))
    assert "<svg" in document
    assert "<line" in document
    assert 'class="bar-axes"' in document
    assert 'class="installed-envelopes"' in document
    assert 'class="partition-atoms"' in document
    assert 'class="partition-domain"' in document
    assert "прямоугольник разбиения" in document
    assert "<img" not in document
    assert "Пересечения зон" in document
    assert payload["constraints"]["allow_overlaps"] is True
    assert [solution["algorithm"] for solution in payload["solutions"]] == [
        "spatial-partition-greedy",
        "bbox",
        "bsp",
        "greedy",
        "greedy-priority",
        "agglomerative",
        "row-run-greedy",
        "strip-profile-dp",
    ]
    assert payload["units"] == "mm"
    assert payload["schema_version"] == 2
    assert payload["solutions"][0]["meta"]["merge_trajectory"]
    assert payload["solutions"][0]["meta"]["trajectory_pareto_front"]
    assert payload["solutions"][0]["zones"][0]["first_bar_coordinate_mm"] is not None
