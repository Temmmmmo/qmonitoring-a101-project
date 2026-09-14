"""Package only the reviewed diagnostics and whole-plate rollback adapter."""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from package_revit_probe import FILES as PROBE_FILES
from package_revit_probe import SOURCE

ROOT = Path(__file__).resolve().parents[1]
BUTTON = "QMonitoring.extension/QMonitoring.tab/Diagnostics.panel/FullPlateTrial.pushbutton"
NEW_FILES = ("QMonitoring.extension/lib/qm_plate_packet.py", "QMonitoring.extension/lib/qm_revit_plate_trial.py",
             "QMonitoring.extension/lib/qm_trial_worksharing.py", "WORKSHARING_README.md",
             f"{BUTTON}/script.py", f"{BUTTON}/bundle.yaml")
MAX_REVIEW_BYTES = 8 * 1024 * 1024
MAX_README_BYTES = 512 * 1024


def _read_bounded(path: Path, limit: int, suffix: str) -> bytes:
    if path.suffix.lower() != suffix:
        raise ValueError(f"Expected a {suffix} file: {path}")
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if not content or len(content) > limit:
        raise ValueError(f"Empty or oversized handoff file: {path}")
    return content


def _validate_review(content: bytes, packet: dict) -> dict:
    from qm_trial_input import _reject_constant, _unique_object
    review = json.loads(content.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                        parse_constant=_reject_constant)
    if (not isinstance(review, dict) or review.get("placement_eligible") is not False
            or review.get("source_report_sha256") != packet["source_report_sha256"]):
        raise ValueError("Review must be rollback-only and bound to the packet source SHA256")
    aliases = {"zone_count": "zone_count", "run_count": "run_count",
        "physical_bar_count": "physical_bar_count", "total_mass_kg": "additional_mass_kg",
        "additional_mass_kg": "additional_mass_kg", "direction_count": None}
    for name in ("expected", "metrics", "source_metrics"):
        if name not in review:
            continue
        values = review[name]
        if not isinstance(values, dict):
            raise ValueError(f"Review {name} must be an object")
        for key, target in aliases.items():
            if key not in values:
                continue
            expected = 4 if target is None else packet["expected"][target]
            actual = values[key]
            if (isinstance(actual, bool) or not isinstance(actual, (int, float))
                    or not math.isfinite(actual)
                    or not math.isclose(actual, expected, rel_tol=0, abs_tol=1e-5)):
                raise ValueError(f"Review {name}.{key} differs from the validated packet")
    return review


def _plain(value: str) -> str:
    """Keep a packet label on one literal line of generated Markdown."""
    return " ".join(value.split()).replace("`", "'").replace("<", "&lt;").replace(">", "&gt;")


def _readme(packet: dict, version: str, *, has_review: bool, has_case_readme: bool) -> bytes:
    expected = packet["expected"]
    materials = sorted({(run["steel_class"], run["diameter_mm"])
                        for direction in packet["directions"] for run in direction["runs"]})
    types = ", ".join(f"{_plain(steel)} Ø{diameter}" for steel, diameter in materials)
    blockers = "\n".join(f"- `{_plain(item)}`" for item in packet["source_blockers"])
    attachments = []
    if has_review:
        attachments.append("Подробная инженерская проверка: `engineer-review.json`.")
    if has_case_readme:
        attachments.append("Примечания выбранного расчёта: `CASE_README.md`.")
    text = f"""# QMonitoring — полная плита, pyRevit {version}

Пакет: `{_plain(packet['case_id'])}`.
Все четыре направления: низ X/Y и верх X/Y.
**{expected['zone_count']} зон / {expected['run_count']} Rebar-наборов /
{expected['physical_bar_count']} физических стержней / {expected['additional_mass_kg']:.2f} кг дополнительной арматуры.**
Rebar-набор и зона не приравниваются к позиции спецификации.
Требуемые типы: {types}.
SHA256 исходного расчёта: `{packet['source_report_sha256']}`.

Это полный пробный перенос выбранного расчёта, а не разрешение на изготовление
или постоянное размещение. Будут созданы настоящие Rebar, затем локальная операция
обязательно откатится. RVT автоматически не сохраняется и не синхронизируется.
В совместной локальной модели заимствование плиты может остаться после отката:
оно требует отдельного согласия. Подробнее: `WORKSHARING_README.md`.
Никакой конкретный тестовый host или рабочая плита не назначены этим архивом.

## Установка

1. Распакуй ZIP в новую отдельную папку. В pyRevit → Custom Extension Directories
   добавь папку, **внутри которой** лежит `QMonitoring.extension`, не сам script.py
   и не папку .extension. Удали из списка путь к прежней версии QMonitoring.
2. Нажми Reload. Нужна кнопка **QMonitoring → Diagnostics → Full Plate Trial**.
   Другие диагностические кнопки сохранены; их историческая инструкция —
   `REFERENCE_README.md`, она не описывает вложенный полный пакет.

## Запуск на копии рабочей модели

1. Открой соответствующую RVT в Revit 2024: отдельную копию, отсоединённую копию
   **с сохранёнными рабочими наборами** или обычную файловую локальную модель.
   Центральный файл напрямую и облачные/серверные модели пока не поддержаны.
   Рабочие наборы удалять не нужно. Нужна горизонтальная плита с одним объёмным телом.
   Выдели одну нужную плиту. Для просмотра заранее открой 3D-вид без section box.
2. Нажми Full Plate Trial, выбери `full-plate-trial.json` и новое имя выходного JSON.
   Сопоставь каждый требуемый диаметр с настоящим типом и классом в модели.
   Недостающие типы не заменяются похожими и не создаются автоматически.
3. Введи явный перенос DXF → внутренние координаты Revit, в мм: `X; Y`.
   Поддержан только сдвиг. Ноль допустим только при проверенном совпадении осей
   и начала координат; поворот, масштаб и привязка автоматически не подбираются.
4. Введи четыре глубины **осей** от соответствующих граней, в мм:
   `низ X; низ Y; верх X; верх Y`. Это не защитный слой.
   Числовой профиль не назначается автоматически, фон не создаётся.
5. Подтверди копию и параметры. Плагин проверит всю партию против фактического
   Solid плиты с защитным слоем, включая проёмы. При нарушении не создаст ничего:
   стержни не обрезаются, неудобные зоны не пропускаются.
6. После создания сверяются все оси, диаметры, длины, количества и масса до и
   после Commit. Если открыто окно просмотра — сделай скриншот до ОК:
   затем локальное создание Rebar откатится. В совместной модели существующий
   вид не меняется; заимствование плиты автоматически не возвращается.
7. Пришли выходной JSON. Если статус `rollback_unconfirmed` / `restoration_failed`,
   **не сохраняй копию**, закрой без сохранения и пришли журнал.

## Что означает результат

- `blocked_setup`: параметры/типы не заданы; изменений нет. Отчёт выбранной плиты
  всё равно полезен — её геометрия записывается до выбора профиля.
- `blocked_preflight`: партия не помещается или профиль модели не поддержан.
- `failed_rolled_back`: создание/сверка не прошли, операция откатилась.
- `passed_rolled_back`: перенос и считывание совпали, затем партия откатилась.
  Это **не** подтверждение всех инженерных гейтов.

Фон, коллизии, анкеровка, контакт, профиль X/Y и раскрой не становятся допустимыми
после совпадения readback. Исходные замечания сохраняются. Полная новая версия
ещё требует проверки в Windows Revit; локальные тесты не заменяют этот запуск.
В архиве нет кода постоянного сохранения или экспериментальной панели MVP.

{' '.join(attachments)}

Исходные блокеры выбранного пакета:

{blockers or '- В источнике блокеры не перечислены; это не инженерное разрешение.'}
"""
    return text.encode("utf-8")


def build_package(output: Path, packet_path: Path, *, review_path: Path | None = None,
                  case_readme_path: Path | None = None) -> Path:
    sys.path.insert(0, str(SOURCE / "QMonitoring.extension/lib"))
    from qm_plate_packet import VERSION, load_packet
    from qm_revit_probe import serialize_report_utf8
    # Serialize the validated bytes, not a second unvalidated read of the input file.
    packet = load_packet(str(packet_path))
    packet_content = serialize_report_utf8(packet)
    contents = [(name if name != "README.md" else "REFERENCE_README.md", (SOURCE / name).read_bytes())
                for name in (*PROBE_FILES, *NEW_FILES)]
    contents.append(("README.md", _readme(packet, VERSION,
        has_review=review_path is not None, has_case_readme=case_readme_path is not None)))
    contents.append(("full-plate-trial.json", packet_content))
    if review_path is not None:
        review = _validate_review(_read_bounded(review_path, MAX_REVIEW_BYTES, ".json"), packet)
        contents.append(("engineer-review.json", serialize_report_utf8(review)))
    if case_readme_path is not None:
        content = _read_bounded(case_readme_path, MAX_README_BYTES, ".md")
        content.decode("utf-8-sig")  # A binary attachment must not masquerade as case notes.
        contents.append(("CASE_README.md", content))
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "x", compression=ZIP_DEFLATED) as archive:
        for name, content in contents:
            archive.writestr(name, content)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--review", type=Path, help="Bound rollback-only JSON, stored as engineer-review.json")
    parser.add_argument("--case-readme", type=Path, help="UTF-8 Markdown, stored as CASE_README.md")
    args = parser.parse_args()
    print(build_package(args.output, args.packet, review_path=args.review, case_readme_path=args.case_readme))


if __name__ == "__main__":
    main()
