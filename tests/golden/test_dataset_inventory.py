"""Сборка переносимого инвентаря слабых инженерских меток."""

from rebar.golden import build_engineer_dataset_inventory
from rebar.reporting import generate_engineer_dataset_report


def test_inventory_without_local_materials_keeps_declared_dataset_shape(tmp_path):
    payload = build_engineer_dataset_inventory(tmp_path)

    assert payload["schema_version"] == 1
    assert payload["summary"] == {
        "reference_case_count": 11,
        "input_set_count": 15,
        "source_available_count": 0,
        "declared_numeric_label_count": 5,
        "verified_numeric_label_count": 0,
        "unreadable_pdf_count": 6,
        "end_to_end_golden_count": 1,
    }
    assert len(payload["cases"]) == 11
    assert all(case["source_status"] == "missing" for case in payload["cases"])


def test_engineer_dataset_report_writes_json_and_human_summary(tmp_path):
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "report"
    data_dir.mkdir()

    json_path, markdown_path = generate_engineer_dataset_report(data_dir, out_dir)

    assert json_path.is_file()
    assert markdown_path.is_file()
    text = markdown_path.read_text(encoding="utf-8")
    assert "Независимых инженерских выдач: **11**" in text
    assert "Четырёхнаправленных входных комплектов: **15**" in text
    assert "plate-zero-k09" in text
