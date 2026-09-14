"""Export one freshly revalidated full ordinary plate to the existing Revit trial."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rebar.application.layout_snapshot import SELECTIONS, load_layout_trial_snapshot


def export_snapshot(snapshot: Path, output_dir: Path, *, candidate_id: str | None = None,
                    selection: str | None = None, steel_class: str) -> dict:
    """Write three new files; neither an existing directory nor files are reused."""
    if output_dir.exists():
        raise ValueError(f"Output directory already exists; choose a new path: {output_dir}")
    bundle = load_layout_trial_snapshot(snapshot, candidate_id=candidate_id,
        selection=selection, steel_class=steel_class)
    sys.path.insert(0, str(ROOT / "integrations/pyrevit/QMonitoring.extension/lib"))
    from qm_plate_packet import validate_packet
    packet, review = bundle["packet"], bundle["review"]
    validate_packet(packet)
    metrics = packet["expected"]
    comparison = review["engineer_comparison"]
    notes = "\n".join(f"- `{item}`" for item in packet["source_blockers"])
    readme = f"""# Полная раскладка для инженерской проверки

Плита: **{packet['case_id']}**, кандидат **{review['snapshot']['candidate_id']}**.
Источник: `{review['snapshot']['path']}`.
SHA256 исходного расчёта: `{packet['source_report_sha256']}`.

Переданы все четыре направления: низ X/Y и верх X/Y, без удаления зон или КЭ.
{metrics['zone_count']} зон / {metrics['run_count']} Rebar-наборов /
{metrics['physical_bar_count']} физических стержней / {metrics['additional_mass_kg']:.2f} кг добавки.
Потребность заново прочитана из четырёх DXF; исходные файлы и карта сверены с расчётом.
Непокрытых КЭ в исходной модели равномерных зон: {review['original_under_reinforced_cell_count']}.

Инженер: {comparison['mass_kg']:.2f} кг / {comparison['physical_bar_count']} стержней.
Разница: масса {comparison['mass_delta_pct']:+.2f}%, стержни {comparison['bar_delta_pct']:+.2f}%.
Порог +15% по массе: {'пройден' if comparison['mass_threshold_15pct_met'] else 'не пройден'}.
Это сравнение дополнительной арматуры одной плиты, не утверждение о прохождении всех гейтов.
Инженерские строки спецификации ({comparison['specification_rows']}) не равны числу наших зон.

## Как использовать

1. Передать `full-plate-trial.json` и `engineer-review.json` вместе с расширением
   QMonitoring 0.6.0. Для запуска нужна кнопка Diagnostics → Full Plate Trial.
2. Открыть **копию соответствующей рабочей RVT**, выбрать именно эту плиту.
   Не подменять её TEST_SLAB_01. Сопоставить реальные типы {steel_class},
   явно задать XY и глубины осей от граней плиты.
3. Плагин проверит фактический host/проёмы; при выходе стержней за него
   отклонит всю партию. При успешном создании сверит оси до/после Commit,
   покажет временные Rebar и обязательно откатит изменения.
4. Вернуть JSON запуска. При неподтверждённом откате не сохранять копию RVT.

## Что этот пакет не утверждает

Оси, длины, количество и масса сохранены **без пересчёта**. Равномерный @150
не превращён в подтверждённые 100/200 мм; @100 не превращён в схему касания СТО.
Высоты, контакт с фоном, стыки, коллизии и безотходность раскроя требуют отдельной
проверки. 40d — использованная контрольная политика, не универсальное инженерное решение.
Экспорт ничего не разместил в Revit. Полный запуск в Windows ещё не подтверждён.
Постоянное размещение запрещено; `placement_eligible=false`.

Полная проверка и исходные замечания: `engineer-review.json`.
Сохраняемые блокеры:

{notes}
"""
    files = {
        "full-plate-trial.json": json.dumps(packet, ensure_ascii=False, allow_nan=False, indent=2),
        "engineer-review.json": json.dumps(review, ensure_ascii=False, allow_nan=False, indent=2),
        "README.md": readme,
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        with (output_dir / name).open("x", encoding="utf-8") as stream:
            stream.write(content)
            stream.write("\n")
    return {"output_dir": str(output_dir), "candidate_id": review["snapshot"]["candidate_id"],
        **metrics, "mass_delta_pct": comparison["mass_delta_pct"],
        "bar_delta_pct": comparison["bar_delta_pct"], "placement_eligible": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="Local preserve-demand run_audit JSON")
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--candidate-id")
    choice.add_argument("--selection", choices=SELECTIONS)
    parser.add_argument("--steel-class", required=True, help="Explicit class, e.g. A500; never inferred")
    parser.add_argument("--output-dir", type=Path, required=True, help="New directory; no overwrite")
    args = parser.parse_args()
    try:
        result = export_snapshot(args.snapshot, args.output_dir, candidate_id=args.candidate_id,
            selection=args.selection, steel_class=args.steel_class)
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
