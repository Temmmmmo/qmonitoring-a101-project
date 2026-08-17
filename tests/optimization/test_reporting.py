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
    assert "<img" not in document
    assert [solution["algorithm"] for solution in payload["solutions"]] == ["bbox", "bsp"]
    assert payload["units"] == "mm"
