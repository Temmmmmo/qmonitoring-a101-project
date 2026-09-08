# -*- coding: utf-8 -*-
"""One explicit JSON zone: inner Commit, readback, mandatory outer RollBack."""
from __future__ import print_function, unicode_literals

import datetime
import os
import platform
import traceback

from pyrevit import DB, forms, revit, script
from System.Collections.Generic import List

from qm_revit_probe import PROBE_VERSION, write_report_json
from qm_revit_trial import run_trial
from qm_trial_input import load_trial_input

__title__ = "JSON Trial"
__doc__ = "Test COPY only. Commit inside a mandatory rollback group. Never saves the RVT."


def main():
    print("QMonitoring JSON Trial {0}; Python {1} ({2})".format(
        PROBE_VERSION, platform.python_version(), platform.python_implementation()))
    doc = revit.doc
    if doc is None or doc.IsFamilyDocument:
        forms.alert("Открой копию тестовой RVT-модели, не редактор семейств.")
        return
    source = forms.pick_file(file_ext="json", title="Выбери samples/single-zone-trial.json из нового ZIP")
    if not source:
        return
    try:
        data = load_trial_input(source)
    except Exception:
        print(traceback.format_exc())
        forms.alert("Входной JSON отклонён, тест не запускался.\n"
                    "Нужен samples/single-zone-trial.json из ZIP, не предыдущий отчёт.\n"
                    "Пришли журнал, если выбран именно этот файл.")
        return
    zone = data["zone"]
    if not forms.alert(
        "Открыта модель: {0}\n\nПодтверди, что это КОПИЯ тестовой модели.\n"
        "Из JSON: {1} стержней Ø25, длина {2} мм, точный шаг {3} мм.\n"
        "Первая ось: смещение X={4}, Y={5} мм от минимального угла плиты.\n"
        "Высота — от верхней грани плиты и защитного слоя.\n\n"
        "Будет Commit внутренней транзакции и повторное считывание осей.\n"
        "Затем ОБЯЗАТЕЛЬНЫЙ ОТКАТ всей группы, даже при успехе.\n"
        "Стержней теста в модели не останется. RVT не сохраняется.\n"
        "Предупреждения/ошибки Revit отклоняют тест, не исправляются автоматически.\n\n"
        "Запустить?".format(doc.Title, zone["bar_count"], zone["length_mm"], zone["spacing_mm"],
                            zone["first_axis_offset_x_mm"], zone["first_axis_offset_y_mm"]),
        title="QMonitoring — JSON, Commit и откат", yes=True, no=True, ok=False,
    ):
        return
    destination = forms.save_file(
        file_ext="json", default_name="qmonitoring-json-trial-{0}.json".format(
            datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")), title="Выбери новый JSON отчёта")
    if not destination:
        return
    if os.path.splitext(destination)[1].lower() != ".json" or os.path.exists(destination):
        forms.alert("Выбери новый файл .json. Тест ещё не запускался.")
        return
    report = run_trial(doc, DB, lambda: List[DB.Curve](), copy_confirmed=True, trial_input=data,
                       runtime={"python": platform.python_version(),
                                "implementation": platform.python_implementation(), "runner": "pyRevit"})
    script.get_output().print_md("## QMonitoring: JSON Trial")
    print("Статус: {0}".format(report["status"]))
    print("Commit: {0}".format(report["commit"]["status"]))
    print("Сверка после Commit: {0}".format(
        report.get("post_commit_comparison", {}).get("status", "not_checked")))
    print("Откат всей группы: {0}".format(report["group_rollback"]["status"]))
    print("Восстановление проверено: {0}".format(report.get("restoration", {}).get("verified", False)))
    for issue in report["issues"]:
        print("{0}: {1}".format(issue["stage"], issue["message"]))
    if report["status"] in ("rollback_unconfirmed", "restoration_failed"):
        forms.alert("ВНИМАНИЕ: восстановление модели НЕ подтверждено.\n"
                    "Не сохраняй копию и не повторяй тест в ней.\n"
                    "Пришли JSON и журнал, затем закрой копию без сохранения.")
    elif report["status"] == "blocked_preflight":
        print("Остановка до транзакций, стержни не создавались.")
    else:
        print("Временные изменения отменены. Это проверка API, не инженерная приёмка.")
    # File I/O only after transaction/group cleanup and restoration checks.
    write_report_json(destination, report)
    print("Отчёт сохранён: {0}".format(destination))
    print("Пришли JSON и скриншот этого окна pyRevit. Сохранять RVT не нужно.")


try:
    main()
except Exception:
    print(traceback.format_exc())
    forms.alert("Не удалось завершить тест или сохранить отчёт. Пришли полный журнал.\n"
                "Если восстановление не подтверждено, закрой копию БЕЗ сохранения.")
