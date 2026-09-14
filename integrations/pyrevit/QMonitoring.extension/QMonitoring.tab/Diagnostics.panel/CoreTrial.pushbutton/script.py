# -*- coding: utf-8 -*-
"""One core-generated addition, two @300 runs, mandatory outer rollback."""
from __future__ import print_function, unicode_literals

import datetime
import os
import platform
import traceback

from pyrevit import DB, forms, revit, script
from System.Collections.Generic import List

from qm_core_trial import load_core_input
from qm_revit_probe import PROBE_VERSION, write_report_json
from qm_revit_trial import run_trial

__title__ = "Core Trial"
__doc__ = "One core zone with 100/200 axes. Test COPY, Commit/readback, mandatory group rollback."


def main():
    print("QMonitoring Core Trial {0}; Python {1} ({2})".format(
        PROBE_VERSION, platform.python_version(), platform.python_implementation()))
    doc = revit.doc
    if doc is None or doc.IsFamilyDocument:
        forms.alert("Открой копию тестовой RVT-модели, не редактор семейств.")
        return
    source = forms.pick_file(file_ext="json", title="Выбери samples/core-axis-trial.json из ZIP")
    if not source:
        return
    try:
        data = load_core_input(source)
    except Exception:
        print(traceback.format_exc())
        forms.alert("Вход отклонён, тест не запускался. Нужен samples/core-axis-trial.json,\n"
                    "не прежний отчёт и не single-zone-trial.json. Пришли журнал.")
        return
    core, binding = data["core_export"], data["binding"]
    component = core["components"][0]
    if not forms.alert(
        "Модель: {0}\n\nПодтверди, что это КОПИЯ тестовой модели.\n"
        "Зона ядра: {1}\n"
        "{2} стержней Ø18, готовая длина {3} мм (выпуски уже включены).\n"
        "Два поднабора @300 создают последовательность интервалов 100/200 мм.\n"
        "Смещение локальной системы от угла плиты: X={4}, Y={5} мм.\n"
        "Фон НЕ создаётся. Привязка тестовая, не правило для всей плиты.\n\n"
        "Commit → повторное чтение → ОБЯЗАТЕЛЬНЫЙ ОТКАТ всей группы.\n"
        "Стержней теста в модели не останется. RVT не сохраняется.\n"
        "Неизвестная геометрия, препятствия и ошибки Revit остановят тест.\n\n"
        "Запустить?".format(doc.Title, core["source_zone_id"], component["bar_count"],
                            component["installed_length_mm"], binding["offset_x_mm"], binding["offset_y_mm"]),
        title="QMonitoring — Core Trial с откатом", yes=True, no=True, ok=False,
    ):
        return
    destination = forms.save_file(file_ext="json", default_name="qmonitoring-core-trial-{0}.json".format(
        datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")), title="Выбери новый JSON отчёта")
    if not destination:
        return
    if os.path.splitext(destination)[1].lower() != ".json" or os.path.exists(destination):
        forms.alert("Выбери новый файл .json. Тест ещё не запускался.")
        return
    report = run_trial(doc, DB, lambda: List[DB.Curve](), copy_confirmed=True, core_trial_input=data,
                       runtime={"python": platform.python_version(),
                                "implementation": platform.python_implementation(), "runner": "pyRevit"})
    script.get_output().print_md("## QMonitoring: Core Trial")
    print("Статус: {0}".format(report["status"]))
    print("Commit: {0}".format(report["commit"]["status"]))
    check = report.get("post_commit_comparison", {})
    print("Сверка после Commit: {0}".format(check.get("status", "not_checked")))
    print("Физических стержней: {0}".format(check.get("physical_bar_count", "not_checked")))
    print("Масса по прочитанным осям, кг: {0}".format(check.get("mass_from_readback_kg", "not_checked")))
    print("Откат группы: {0}".format(report["group_rollback"]["status"]))
    print("Восстановление проверено: {0}".format(report.get("restoration", {}).get("verified", False)))
    for issue in report["issues"]:
        print("{0}: {1}".format(issue["stage"], issue["message"]))
    if report["status"] in ("rollback_unconfirmed", "restoration_failed"):
        forms.alert("ВНИМАНИЕ: восстановление НЕ подтверждено. Не сохраняй копию\n"
                    "и не повторяй тест. Пришли JSON и журнал, закрой копию без сохранения.")
    elif report["status"] == "blocked_preflight":
        print("Остановка до транзакций, стержни не создавались.")
    else:
        print("Временные изменения отменены. Это тест API, не инженерная приёмка.")
    write_report_json(destination, report)
    print("Отчёт: {0}".format(destination))
    print("Пришли JSON и скриншот этого окна Core Trial. RVT сохранять не нужно.")


try:
    main()
except Exception:
    print(traceback.format_exc())
    forms.alert("Ошибка теста или записи отчёта. Пришли полный журнал.\n"
                "Если восстановление не подтверждено, закрой копию БЕЗ сохранения.")
