"""Package a provenance-preserving physical bar plan for an always-rollback trial."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
from zipfile import ZIP_DEFLATED, ZipFile

from package_revit_plate_trial import NEW_FILES as PLATE_FILES
from package_revit_plate_trial import _plain, _read_bounded
from package_revit_probe import FILES as PROBE_FILES
from package_revit_probe import SOURCE

BUTTON = "QMonitoring.extension/QMonitoring.tab/Diagnostics.panel/PhysicalPlanTrial.pushbutton"
NEW_FILES = ("QMonitoring.extension/lib/qm_physical_packet.py",
             "QMonitoring.extension/lib/qm_revit_physical_trial.py",
             f"{BUTTON}/script.py", f"{BUTTON}/bundle.yaml")
MAX_REVIEW_BYTES = 16 * 1024 * 1024


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite JSON number")
    return result


def _validate_review(content: bytes, packet: dict, packet_content: bytes) -> dict:
    from qm_trial_input import _reject_constant, _unique_object

    review = json.loads(content.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                        parse_constant=_reject_constant, parse_float=_finite_float)
    if (not isinstance(review, dict) or review.get("placement_eligible") is not False
            or review.get("source_report_sha256") != packet["source_report_sha256"]
            or review.get("raw_report_sha256") != packet["raw_report_sha256"]
            or review.get("packet_sha256") != hashlib.sha256(packet_content).hexdigest()
            or review.get("expected") != packet["expected"]
            or review.get("manual_joint_tasks") != packet["manual_joint_tasks"]):
        raise ValueError("Review must match the exact physical packet, provenance, metrics and unresolved joints")
    return review


def _readme(packet: dict, version: str) -> bytes:
    expected = packet["expected"]
    pairs = len(packet["manual_joint_tasks"])
    types = sorted({(run["steel_class"], run["diameter_mm"])
                    for direction in packet["directions"] for run in direction["runs"]})
    materials = ", ".join(f"{_plain(steel)} Ø{diameter}" for steel, diameter in types)
    host_warning = ("**Офлайн-проверка рабочей плиты уже обнаружила нарушения контура/проёмов.** "
        "Создание этой полной партии должно быть заблокировано. Этот архив не является готовым размещением; "
        "повторный запуск в Revit не исправит геометрию.\n"
        if "working-host-fit-incomplete" in packet["source_blockers"] else "")
    text = f"""# QMonitoring — проверка физической раскладки, {version}

Комплект: `{_plain(packet['case_id'])}`. Все четыре направления, без удаления
неудобных участков. **{expected['physical_bar_count']} стержней /
{expected['additional_mass_kg']:.2f} кг дополнительной арматуры.**

Исходных параметрических зон: {expected['source_zone_count']}.
Групп одинаковой физической геометрии: {expected['execution_group_count']}.
Регулярных Rebar-наборов: {expected['run_count']}.
Типоразмеров прямых стержней: {expected['position_count']}.
Эти четыре единицы счёта не взаимозаменяемы. Объединённый физический стержень
сохраняет ссылки на все исходные зоны, которые он обеспечивает.
Требуемые типы: {materials}.

{host_warning}
**Остаётся {pairs} нерешённых пересечений пар стержней.** Они перечислены
в `engineer-review.json` и передаются в отчёт Revit, не объявляются допустимыми
стыками. Рабочие host, привязка и высоты ещё не подтверждены. Успешный readback
подтверждает передачу геометрии, а не выполнение всех инженерных требований.

## Установка и запуск

1. Распакуй архив в новую папку. В pyRevit → Custom Extension Directories добавь
   папку, **внутри которой** находится `QMonitoring.extension`. Старый путь
   QMonitoring убери из списка и нажми Reload.
2. Открой соответствующую модель в Revit 2024: отдельную копию, отсоединённую
   копию **с сохранёнными рабочими наборами** или обычную файловую локальную модель.
   Рабочие наборы удалять не нужно. Центральный файл напрямую и облачные/серверные
   модели пока не поддержаны; подробности в `WORKSHARING_README.md`. Выдели нужную
   плиту и заранее открой 3D-вид без section box.
3. Нужна новая кнопка **QMonitoring → Diagnostics → Physical Plan Trial**.
   Выбери `physical-bar-plan-trial.json` и новое имя выходного JSON.
   Старые Full Plate Trial / Reference Probe — другие сценарии.
4. Чтобы сначала прислать только геометрию плиты, нажми **Нет** в первом
   предупреждении о диагностическом создании. Геометрия уже прочитана,
   отчёт сохранится, RVT не меняется. Cancel на выборе типа также безопасен.
5. Для пробного создания всей партии сопоставь реальные типы стержней, задай
   проверенный перенос DXF → внутренние координаты Revit `X; Y` в мм и глубины
   **осей** `низ X; низ Y; верх X; верх Y` от соответствующих граней.
   Нулевой перенос и высоты не подбираются автоматически. Глубина оси — не
   защитный слой. Фон и существующая арматура не создаются и не удаляются.
6. Подтверди копию, параметры и известные нерешённые пересечения. При выходе
   тела стержня с защитным слоем за фактическую плиту / в проём **вся партия
   отклоняется**: подрезки и частичного пропуска нет.
7. Если создание прошло, сделай скриншот до закрытия окна просмотра. Все Rebar
   будут считаны до и после Commit, затем локальное создание обязательно откатится.
   В совместной локальной модели заимствование плиты требует отдельного согласия
   и может остаться после отката; плагин не выполняет возврат владения автоматически.
   Существующий вид в совместной модели не меняется. Плагин не сохраняет и не
   синхронизирует RVT, не выпускает рабочее решение.
8. Пришли выходной JSON и скриншот. Если статус `rollback_unconfirmed` или
   `restoration_failed`, **не сохраняй копию**, закрой её без сохранения.

`blocked_setup` / `blocked_preflight` — полезный результат диагностики, но не
подтверждение создания. `passed_rolled_back` — перенос всей партии совпал,
изменения откатились; исходные инженерные замечания при этом остаются.
Новая кнопка проверена локально; этот запуск нужен для проверки Windows Revit.

Файлы `physical-bar-plan-trial.json` и `engineer-review.json` передавать вместе.
Исходный расчёт SHA256: `{packet['source_report_sha256']}`.
Нормализованная геометрия SHA256: `{packet['raw_report_sha256']}`.
В ZIP нет кода постоянного сохранения и экспериментальной панели MVP.
"""
    return text.encode("utf-8")


def build_package(output: Path, packet_path: Path, *, review_path: Path) -> Path:
    sys.path.insert(0, str(SOURCE / "QMonitoring.extension/lib"))
    from qm_physical_packet import VERSION, load_packet
    from qm_revit_probe import serialize_report_utf8

    packet = load_packet(str(packet_path))
    packet_content = serialize_report_utf8(packet)
    review = _validate_review(_read_bounded(review_path, MAX_REVIEW_BYTES, ".json"),
                              packet, packet_content)
    contents = [(name if name != "README.md" else "REFERENCE_README.md", (SOURCE / name).read_bytes())
                for name in (*PROBE_FILES, *PLATE_FILES, *NEW_FILES)]
    contents.extend((("README.md", _readme(packet, VERSION)),
                     ("physical-bar-plan-trial.json", packet_content),
                     ("engineer-review.json", serialize_report_utf8(review))))
    names = [name for name, _ in contents]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate archive entry")
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "x", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in contents:
            archive.writestr(name, content)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(build_package(args.output, args.packet, review_path=args.review))
    except (ValueError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
