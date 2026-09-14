# -*- coding: utf-8 -*-
"""Selected native Floor reinforcement inventory. READ ONLY."""
from __future__ import print_function, unicode_literals

import datetime
import os
import platform
import traceback

from Autodesk.Revit.Exceptions import OperationCanceledException
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from pyrevit import DB, forms, revit

from qm_revit_probe import element_id, write_report_json
from qm_working_rebar_probe import VERSION, collect_working_rebar_report

__title__ = "Working Rebar\nProbe"
__doc__ = "Только чтение существующей арматуры выбранной плиты. Ничего не создаёт и не меняет в RVT."


class FloorFilter(ISelectionFilter):
    def __init__(self, document):
        self.document = document

    def AllowElement(self, element):
        return isinstance(element, DB.Floor) and element.Document == self.document

    def AllowReference(self, reference, point):
        return False


def choose_floor(document, uidoc):
    ids = list(uidoc.Selection.GetElementIds())
    if len(ids) == 1:
        floor = document.GetElement(ids[0])
        if isinstance(floor, DB.Floor) and floor.Document == document:
            return floor
    selected = uidoc.Selection.PickObject(ObjectType.Element, FloorFilter(document), "Выбери рабочую плиту (Floor)")
    floor = document.GetElement(selected.ElementId)
    if not isinstance(floor, DB.Floor) or floor.Document != document:
        raise ValueError("Нужна плита из текущего документа, не из связи")
    return floor


def main():
    print("QMonitoring Working Rebar Probe {0}; Python {1} ({2})".format(
        VERSION, platform.python_version(), platform.python_implementation()))
    document, uidoc = revit.doc, revit.uidoc
    if document is None or document.IsFamilyDocument:
        forms.alert("Открой рабочую модель Revit, не редактор семейств.")
        return
    floor = choose_floor(document, uidoc)
    destination = forms.save_file(file_ext="json", default_name="qmonitoring-working-rebar-{0}.json".format(
        datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")), title="Новый JSON существующей арматуры плиты")
    if not destination:
        return
    if os.path.splitext(destination)[1].lower() != ".json" or os.path.exists(destination):
        forms.alert("Выбери новое имя .json: существующие файлы не перезаписываются.")
        return
    if not forms.alert("Модель: {0}; плита id={1}.\n"
        "Прочитаю существующие Rebar/RebarInSystem, их реальные оси, формы, типы и количество.\n"
        "Не создаю и не меняю арматуру, виды или рабочие наборы. RVT не сохраняю и не синхронизирую.\n"
        "Связанные модели и арматура других несущих элементов в этот сбор не входят.\n"
        "Это сбор фактов, не проверка расчёта и не согласование инженерного решения. Продолжить?".format(
            document.Title, element_id(floor.Id)), yes=True, no=True):
        return
    report = collect_working_rebar_report(document, DB, floor, runtime={"python": platform.python_version(),
        "implementation": platform.python_implementation(), "runner": "pyRevit"})
    write_report_json(destination, report)
    print("Отчёт: {0}".format(destination))
    print("Сбор: {0}; прочитано существующих положений: {1}; с полной геометрией: {2}; замечаний: {3}".format(
        report["status"], report["summary"]["read_existing_position_count"],
        report["summary"]["exact_geometry_position_count"], len(report["read_issues"])))
    forms.alert("Отчёт сохранён: {0}\nСбор: {1}. Пришли JSON разработчику, даже если статус partial.\n"
        "Рабочие наборы сохраняются; автоматически ничего открывать или исправлять не нужно.\n"
        "RVT не менялся. Это не разрешение на размещение арматуры.".format(destination, report["status"]))


if __name__ == "__main__":
    try:
        main()
    except OperationCanceledException:
        print("Выбор отменён; RVT не менялся.")
    except Exception:
        print(traceback.format_exc())
        forms.alert("Не удалось завершить чтение. Пришли полный текст ошибки pyRevit. Команда не меняла RVT.")
