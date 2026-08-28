"""JSON/Markdown-отчёт об инженерском датасете и доступных метках."""

from __future__ import annotations

import json
from pathlib import Path

from rebar.golden import build_engineer_dataset_inventory


def _metric(item: dict[str, object], key: str) -> str:
    metrics = item.get("actual_metrics")
    if not isinstance(metrics, dict):
        return "—"
    value = metrics.get(key)
    if value is None:
        return "—"
    if key == "total_mass_kg":
        return f"{float(value):.2f}"
    return str(value)


def _markdown(payload: dict[str, object]) -> str:
    summary = payload["summary"]
    assert isinstance(summary, dict)
    cases = payload["cases"]
    assert isinstance(cases, list)
    limitations = payload["limitations"]
    assert isinstance(limitations, list)

    rows = []
    for item in cases:
        assert isinstance(item, dict)
        input_sets = item["input_sets"]
        assert isinstance(input_sets, list)
        verification = str(item["metrics_verification_status"])
        status = {
            "verified_against_pdf": "PDF сверен",
            "not_machine_readable": "нужен OCR",
            "not_checked": "нет источника",
            "failed": "ошибка сверки",
        }.get(verification, verification)
        rows.append(
            "| {case_id} | {title} | {inputs} | {status} | {bars} | {mass} |".format(
                case_id=str(item["case_id"]).replace("|", "\\|"),
                title=str(item["title"]).replace("|", "\\|"),
                inputs=len(input_sets),
                status=status,
                bars=_metric(item, "physical_bar_count"),
                mass=_metric(item, "total_mass_kg"),
            )
        )

    limitations_md = "\n".join(f"- {item}" for item in limitations)
    return f"""# Инвентарь инженерских решений

Отчёт автоматически проверяет переносимый manifest против локальных материалов.

## Итоги

- Независимых инженерских выдач: **{summary['reference_case_count']}**.
- Четырёхнаправленных входных комплектов: **{summary['input_set_count']}**.
- Числовых plate-level меток, повторно сверенных с PDF: **{summary['verified_numeric_label_count']}**.
- Полных end-to-end golden-case: **{summary['end_to_end_golden_count']}**.
- PDF, которым нужен OCR или ручная транскрипция: **{summary['unreadable_pdf_count']}**.

## Выдачи

| ID | Инженерская выдача | Входов | Состояние метрик | Стержней, шт. | Масса, кг |
|---|---|---:|---|---:|---:|
{chr(10).join(rows)}

## Что это значит для обучения

Пять числовых результатов уже можно использовать как слабые наблюдения предпочтения на
уровне всей плиты. Они не задают прямоугольную геометрию и не являются подписанной
«Точкой 3». Сначала для каждого совместимого входа нужно построить допустимый Парето-фронт,
а затем измерить, какой кандидат ближе к инженерским массе и числу физических стержней.
Только после этого появляется честная цель для калибровки веса или ранжирования.

## Ограничения

{limitations_md}
"""


def generate_engineer_dataset_report(data_dir: Path, out_dir: Path) -> tuple[Path, Path]:
    """Сверить manifest и сохранить ``engineer_dataset.json`` + ``README.md``."""

    payload = build_engineer_dataset_inventory(data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "engineer_dataset.json"
    markdown_path = out_dir / "README.md"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(_markdown(payload), encoding="utf-8")
    return json_path, markdown_path
