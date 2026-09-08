# -*- coding: utf-8 -*-
"""pyRevit entry point. Read the reference model and save one local JSON report."""
from __future__ import print_function, unicode_literals

import datetime
import os
import platform
import traceback

from pyrevit import DB, forms, revit, script

from qm_revit_probe import (
    PROBE_VERSION,
    collect_report,
    element_id,
    element_name,
    write_report_json,
)

__title__ = "Reference Probe"
__doc__ = "Read-only reference-model diagnostics. No model changes or network requests."


def main():
    # Keep runtime identification in the log even when collection fails before JSON.
    print("QMonitoring Reference Probe {0}; Python {1} ({2})".format(
        PROBE_VERSION, platform.python_version(), platform.python_implementation()))
    doc = revit.doc
    if doc is None or doc.IsFamilyDocument:
        forms.alert("Открой копию тестовой RVT-модели, не редактор семейств.")
        return
    if not forms.alert(
        "Будет прочитана текущая модель: {0}\n\n"
        "Плита: TEST_SLAB_01 (407801)\n"
        "Область: AR_TEST_TOP_X_001 (407878)\n\n"
        "Скрипт не меняет и не сохраняет RVT. Он создаст отдельный JSON с геометрией.\n"
        "Продолжить на копии тестовой модели?".format(doc.Title),
        title="QMonitoring — диагностика", yes=True, no=True, ok=False,
    ):
        return
    imports = list(DB.FilteredElementCollector(doc).OfClass(DB.ImportInstance)
                   .WhereElementIsNotElementType())
    cad = None
    if imports:
        choices = {}
        for instance in imports:
            cad_type = doc.GetElement(instance.GetTypeId())
            label = "{0} | id={1} | {2}".format(
                element_name(cad_type, DB), element_id(instance.Id),
                "связь" if instance.IsLinked else "импорт")
            choices[label] = instance
        selected = forms.SelectFromList.show(
            sorted(choices), title="Выбери DXF верхнего армирования X",
            button_name="Прочитать выбранный CAD", multiselect=False,
        )
        if selected is None:
            return
        cad = choices[selected]
    report = collect_report(doc, DB, cad, runtime={
        "python": platform.python_version(), "implementation": platform.python_implementation(),
        "runner": "pyRevit",
    })
    filename = "qmonitoring-reference-{0}.json".format(
        datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    destination = forms.save_file(file_ext="json", default_name=filename,
                                  title="Сохранить диагностический отчёт (не RVT)")
    if not destination:
        return
    if os.path.splitext(destination)[1].lower() != ".json":
        forms.alert("Отчёт можно сохранить только в новый файл .json.")
        return
    # Shared with the standalone IronPython smoke test; never overwrites a file.
    write_report_json(destination, report)
    output = script.get_output()
    output.print_md("## QMonitoring: отчёт сохранён")
    print(destination)
    print("Сбор данных: {0}".format(report["status"]))
    area = report.get("area") or {}
    print("Физических стержней: {0}".format(area.get("physical_bar_count")))
    print("Сверка чисел эталона: {0}".format(
        area.get("reference_comparison", {}).get("status", "not_checked")))
    print("Замечаний чтения: {0}".format(len(report["issues"])))
    print("RVT не изменён. Пришли JSON разработчику; это не разрешение на размещение.")


try:
    main()
except Exception:
    # A failure is visible, never replaced by fabricated reference geometry.
    print(traceback.format_exc())
    forms.alert("Не удалось завершить диагностику. Пришли текст ошибки из окна pyRevit.\n"
                "Если файл уже существует, повтори запуск и выбери новое имя.\n"
                "Скрипт не изменяет RVT.")
