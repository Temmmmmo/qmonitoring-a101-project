# -*- coding: utf-8 -*-
"""Read explicitly selected working Floor + CAD. No transactions or placement."""
from __future__ import print_function, unicode_literals

import datetime
import os
import platform
import traceback

from Autodesk.Revit.Exceptions import OperationCanceledException
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from pyrevit import DB, forms, revit

from qm_revit_probe import element_id, element_name, write_report_json
from qm_working_host_probe import VERSION, collect_working_host_report

__title__ = "Working Host\nProbe"
__doc__ = "Рабочая плита + DXF: один отчёт только чтения. Без тестовой модели, арматуры и изменений RVT."


class FloorFilter(ISelectionFilter):
    def __init__(self, document):
        self.document = document

    def AllowElement(self, element):
        return isinstance(element, DB.Floor) and element.Document == self.document

    def AllowReference(self, reference, point):
        return False


def choose_floor(document, uidoc):
    selected = list(uidoc.Selection.GetElementIds())
    if len(selected) == 1:
        floor = document.GetElement(selected[0])
        if isinstance(floor, DB.Floor) and floor.Document == document:
            return floor
    reference = uidoc.Selection.PickObject(ObjectType.Element, FloorFilter(document), "Выбери рабочую плиту (Floor)")
    floor = document.GetElement(reference.ElementId)
    if not isinstance(floor, DB.Floor) or floor.Document != document:
        raise ValueError("Выбрана не рабочая Floor в текущей модели")
    return floor


def main():
    print("QMonitoring Working Host Probe {0}; Python {1} ({2})".format(
        VERSION, platform.python_version(), platform.python_implementation()))
    document, uidoc = revit.doc, revit.uidoc
    if document is None or document.IsFamilyDocument:
        forms.alert("Открой рабочую модель Revit 2024 (лучше копию), не редактор семейств.")
        return
    floor = choose_floor(document, uidoc)
    choices = {}
    for instance in DB.FilteredElementCollector(document).OfClass(DB.ImportInstance).WhereElementIsNotElementType():
        name = "{0} | id={1} | {2}".format(element_name(document.GetElement(instance.GetTypeId()), DB),
            element_id(instance.Id), "связь" if instance.IsLinked else "импорт")
        choices[name] = instance
    if not choices:
        forms.alert("В этой модели нет связанного или импортированного CAD. Ничего не изменено.")
        return
    selected = forms.SelectFromList.show(sorted(choices), multiselect=False,
        title="Выбери DXF для ЭТОЙ рабочей плиты", button_name="Прочитать выбранную плиту и DXF")
    if selected is None:
        return
    destination = forms.save_file(file_ext="json", default_name="qmonitoring-working-host-{0}.json".format(
        datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")), title="Новый JSON рабочей плиты и DXF")
    if not destination:
        return
    if os.path.splitext(destination)[1].lower() != ".json" or os.path.exists(destination):
        forms.alert("Выбери новое имя .json: существующие файлы не перезаписываются.")
        return
    if not forms.alert("Модель: {0}; выбранная плита id={1}; CAD: {2}.\n"
        "Прочитаю грани/проёмы/защитные слои плиты, геометрию и преобразования CAD.\n"
        "Для локальной связи — SHA256 файла. Можно дополнительно указать исходный DXF.\n"
        "Ничего не создаю, не передвигаю и не сохраняю в RVT.\n"
        "Это сбор данных, а НЕ подтверждение привязки или укладки. Продолжить?".format(
            document.Title, element_id(floor.Id), selected), yes=True, no=True):
        return
    local_dxf = forms.pick_file(file_ext="dxf", title="Необязательно: исходный локальный DXF для SHA256; Отмена — пропустить")
    report = collect_working_host_report(document, DB, floor, choices[selected], runtime={
        "python": platform.python_version(), "implementation": platform.python_implementation(), "runner": "pyRevit"},
        source_dxf_path=local_dxf)
    write_report_json(destination, report)
    print("Отчёт: {0}".format(destination))
    print("Сбор: {0}; плита id={1}; CAD id={2}".format(report["status"], report["host_id"], report["cad_id"]))
    print("Подтверждённых KLEENKA-треугольников: {0}".format(report["cad_geometry"]["triangle_count"]))
    print("SHA256 связи: {0}; выбранного файла: {1}".format(
        report["linked_source_file"]["status"], report["selected_source_file"]["status"]))
    forms.alert("Отчёт сохранён: {0}\nСтатус чтения: {1}.\n"
        "Пришли JSON разработчику. partial тоже полезен: ничего исправлять вручную не нужно.\n"
        "Команда не меняет RVT. Привязка DXF, host/Z и инженерная укладка НЕ подтверждены.".format(destination, report["status"]))


if __name__ == "__main__":
    try:
        main()
    except OperationCanceledException:
        print("Выбор отменён; RVT не менялся.")
    except Exception:
        print(traceback.format_exc())
        forms.alert("Не удалось завершить чтение рабочей плиты/DXF. Пришли текст ошибки из pyRevit. RVT не менялся.")
