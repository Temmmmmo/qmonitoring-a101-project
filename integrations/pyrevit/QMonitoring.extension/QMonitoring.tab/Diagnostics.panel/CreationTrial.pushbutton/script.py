# -*- coding: utf-8 -*-
"""Create, read and ALWAYS roll back one trial set. Never saves the RVT."""
from __future__ import print_function, unicode_literals

import datetime
import os
import platform
import traceback

from pyrevit import DB, forms, revit, script
from System.Collections.Generic import List

from qm_revit_probe import PROBE_VERSION, write_report_json
from qm_revit_trial import run_trial

__title__ = "Creation Trial"
__doc__ = "One temporary Rebar set on the reference COPY. Mandatory rollback, no RVT save."


def main():
    print("QMonitoring Creation Trial {0}; Python {1} ({2})".format(
        PROBE_VERSION, platform.python_version(), platform.python_implementation()))
    doc = revit.doc
    if doc is None or doc.IsFamilyDocument:
        forms.alert("Открой копию тестовой RVT-модели, не редактор семейств.")
        return
    if not forms.alert(
        "Открыта модель: {0}\n\n"
        "Подтверди, что это КОПИЯ тестовой модели.\n"
        "Плита TEST_SLAB_01 (407801), эталон AR_TEST_TOP_X_001 (407878).\n\n"
        "Тест: 9 стержней Ø25 длиной 3900 мм, шаг 96,875 мм.\n"
        "Участок начинается в 1000 мм по X/Y от нижнего левого угла плиты.\n"
        "Занятый участок, проёмы или неподдерживаемая геометрия остановят тест.\n\n"
        "Стержни создаются ВРЕМЕННО. После считывания будет ОБЯЗАТЕЛЬНЫЙ ОТКАТ,\n"
        "даже при успехе. В модели ничего оставлять не будем; RVT не сохраняется.\n"
        "Эталон не перемещается, DXF не используется для размещения.\n\n"
        "Запустить на этой копии?".format(doc.Title),
        title="QMonitoring — тест с откатом", yes=True, no=True, ok=False,
    ):
        return
    destination = forms.save_file(
        file_ext="json", default_name="qmonitoring-creation-trial-{0}.json".format(
            datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")),
        title="Выбери новый JSON для отчёта теста")
    if not destination:
        return
    if os.path.splitext(destination)[1].lower() != ".json" or os.path.exists(destination):
        forms.alert("Выбери новый файл .json. Тест ещё не запускался.")
        return
    report = run_trial(doc, DB, lambda: List[DB.Curve](), copy_confirmed=True, runtime={
        "python": platform.python_version(), "implementation": platform.python_implementation(),
        "runner": "pyRevit"})
    output = script.get_output()
    output.print_md("## QMonitoring: результат тестового создания")
    print("Статус: {0}".format(report["status"]))
    print("Откат: {0}".format(report["rollback"]["status"]))
    if "readback" in report:
        print("Прочитано физических стержней: {0}".format(len(report["readback"]["bars"])))
    print("Сверка осей: {0}".format(report.get("comparison", {}).get("status", "not_checked")))
    for issue in report["issues"]:
        print("{0}: {1}".format(issue["stage"], issue["message"]))
    if report["status"] in ("rollback_unconfirmed", "restoration_failed"):
        forms.alert("ВНИМАНИЕ: восстановление состояния модели НЕ подтверждено.\n"
                    "Не сохраняй эту тестовую копию и не повторяй тест в ней.\n"
                    "Пришли JSON и журнал; затем закрой копию без сохранения.")
    elif report["status"] == "blocked_preflight":
        print("Тест остановлен до создания. Пришли JSON; модель не изменялась.")
    else:
        print("Временные изменения отменены. Стержней теста в модели не осталось.")
        print("Это тест API без Commit, не инженерная приёмка и не заполнение плиты.")
    # This happens AFTER rollback/restoration verification, never inside a transaction.
    write_report_json(destination, report)
    print("Отчёт сохранён: {0}".format(destination))
    print("Пришли этот JSON и скриншот окна результата. Сохранять RVT не нужно.")


try:
    main()
except Exception:
    print(traceback.format_exc())
    forms.alert("Не удалось завершить тест или сохранить JSON. Пришли полный журнал pyRevit.\n"
                "Проверь строку статуса отката. Если его подтверждения нет,\n"
                "закрой тестовую копию БЕЗ сохранения. RVT скрипт не сохраняет.")
