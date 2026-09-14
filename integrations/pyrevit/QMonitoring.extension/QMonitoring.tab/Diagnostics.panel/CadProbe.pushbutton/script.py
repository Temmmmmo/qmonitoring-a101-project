# -*- coding: utf-8 -*-
"""Read-only CAD calibration evidence. Never place bars or modify a view/model."""
from __future__ import print_function, unicode_literals

import datetime
import os
import platform
import traceback

from pyrevit import DB, forms, revit

from qm_revit_cad import collect_cad_report
from qm_revit_probe import PROBE_VERSION, element_id, element_name, write_report_json

__title__ = "CAD Probe"
__doc__ = "Read CAD meshes by two methods, keeping unknown layers diagnostic. No RVT changes."


def main():
    print("QMonitoring CAD Probe {0}; Python {1} ({2})".format(
        PROBE_VERSION, platform.python_version(), platform.python_implementation()))
    doc = revit.doc
    if doc is None or doc.IsFamilyDocument:
        forms.alert("Открой копию тестовой RVT-модели, не редактор семейств.")
        return
    if not forms.alert(
        "Прочитаю геометрию DXF и эталонной плиты в модели {0}.\n"
        "Два способа чтения будут записаны в один отчёт, включая меши без слоя.\n"
        "Для локальной связи также прочитаю DXF для сверки контрольной суммы.\n"
        "Ничего не размещаю, не перемещаю и не сохраняю в RVT.\n"
        "Продолжить на копии?".format(doc.Title),
        title="QMonitoring — привязка DXF", yes=True, no=True, ok=False,
    ):
        return
    choices = {}
    for instance in DB.FilteredElementCollector(doc).OfClass(DB.ImportInstance).WhereElementIsNotElementType():
        label = "{0} | id={1} | {2}".format(
            element_name(doc.GetElement(instance.GetTypeId()), DB), element_id(instance.Id),
            "связь" if instance.IsLinked else "импорт")
        choices[label] = instance
    if not choices:
        forms.alert("В модели нет импортированного или связанного CAD.")
        return
    selected = forms.SelectFromList.show(
        sorted(choices), title="Выбери DXF верхнего армирования X (сначала связь)",
        button_name="Прочитать CAD двумя способами", multiselect=False)
    if selected is None:
        return
    destination = forms.save_file(file_ext="json", default_name="qmonitoring-cad-{0}.json".format(
        datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")), title="Новый отчёт CAD (не RVT)")
    if not destination:
        return
    if os.path.splitext(destination)[1].lower() != ".json" or os.path.exists(destination):
        forms.alert("Выбери новое имя файла .json. Существующие файлы не перезаписываются.")
        return
    report = collect_cad_report(doc, DB, choices[selected], runtime={
        "python": platform.python_version(), "implementation": platform.python_implementation(),
        "runner": "pyRevit"})
    write_report_json(destination, report)
    print("Отчёт сохранён: {0}".format(destination))
    print("Сбор: {0}; замечаний верхнего уровня: {1}".format(report["status"], len(report["issues"])))
    for label, key in (("Symbol", "cad_geometry"), ("Instance", "cad_geometry_instance")):
        geometry = report[key]
        print("{0}: чтение мешей завершено={1}; всего треугольников={2}; "
              "подтверждённых KLEENKA={3}; мешей без слоя={4}".format(
                  label, geometry["mesh_read_complete"], geometry["all_mesh_triangle_count"],
                  geometry["triangle_count"], geometry["unresolved_mesh_count"]))
    print("Сравнение координат двух чтений: {0}".format(report["cad_geometry_comparison"]["status"]))
    print("Контрольная сумма локальной связи: {0}".format(report["linked_source_file"]["status"]))
    print("Сверка с исходным DXF ещё не выполнена. Пришли JSON разработчику.")
    print("partial при неизвестных слоях допустим для диагностики; менять модель не нужно.")
    print("Команда не изменяет RVT и не разрешает размещение арматуры.")


try:
    main()
except Exception:
    print(traceback.format_exc())
    forms.alert("Не удалось завершить чтение CAD. Пришли текст ошибки из окна pyRevit.\n"
                "Команда не изменяет RVT.")
