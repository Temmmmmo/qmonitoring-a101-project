# -*- coding: utf-8 -*-
from __future__ import print_function, unicode_literals

import datetime
import os
import traceback

from pyrevit import DB, forms, revit, script
from System.Collections.Generic import List

from qm_mvp_packet import MVP_VERSION, load_packet
from qm_revit_mvp import run_mvp_demo
from qm_revit_probe import write_report_json


def main():
    print("QMonitoring MVP Demo " + MVP_VERSION)
    if revit.doc is None or revit.doc.IsFamilyDocument:
        forms.alert("Открой сохранённую тестовую RVT-модель.")
        return
    path = forms.pick_file(file_ext="json", title="Выбери demo-layout.json из MVP-архива")
    if not path:
        return
    packet = load_packet(path)
    metrics = packet["expected"]
    if not forms.alert(
        "ДЕМОНСТРАЦИЯ, НЕ ДЛЯ СТРОИТЕЛЬСТВА\n\n"
        "{0} зон, {1} стержней, {2:.2f} кг. Только верхний X.\n"
        "Будет создан НОВЫЙ RVT-файл; исходный не сохраняется и не перезаписывается.\n"
        "В копии останутся настоящие Rebar и отдельный 3D-вид.\n\n"
        "Фазы, глубины 34/77/65 и стыки экспериментальные.\n"
        "Край: {3} непокрытых КЭ. Фон и другие направления не создаются.\n"
        "Старый ручной эталон останется, но будет скрыт в новом виде.\n"
        "Это НЕ приёмка инженерного решения.\n\nПродолжить?".format(metrics["zone_count"],
            metrics["physical_bar_count"], metrics["additional_mass_kg"], packet["scope"]["original_uncovered_cell_count"]),
        title="QMonitoring MVP — только демонстрационная копия", yes=True, no=True, ok=False):
        return
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination = forms.save_file(file_ext="rvt", default_name="QM_DEMO_"+stamp+".rvt", title="НОВАЯ демонстрационная RVT-копия")
    if not destination:
        return
    report_path = forms.save_file(file_ext="json", default_name="qmonitoring-mvp-"+stamp+".json", title="НОВЫЙ JSON результата")
    if not report_path:
        return
    if os.path.splitext(report_path)[1].lower() != ".json" or os.path.exists(report_path):
        forms.alert("Отчёт должен быть новым JSON. Операция не запускалась.")
        return

    def activate(view):
        revit.uidoc.ActiveView = view
        revit.uidoc.RefreshActiveView()

    result = run_mvp_demo(revit.doc, DB, lambda: List[DB.Curve](), lambda: List[DB.ElementId](),
                          packet, destination, demo_confirmed=True, activate_view=activate)
    print("Статус: " + result["status"])
    print("Исходный RVT неизменен: {0}".format(result.get("source_file_unchanged")))
    print("Новая копия сохранена: {0}".format(result["demo_saved"]))
    print("Сверка осей: {0}".format(result.get("comparison", {}).get("status", "not_checked")))
    write_report_json(report_path, result)
    print("Отчёт: " + report_path)
    if result["status"] == "demo_saved_and_readback_matches":
        script.get_output().print_md("## ДЕМО сохранено — НЕ ДЛЯ СТРОИТЕЛЬСТВА")
        forms.alert("Демонстрационная копия сохранена, оси и количество совпали.\n"
                    "Пришли JSON результата и скриншот нового 3D-вида.\n"
                    "Исходный RVT не изменён. Инженерная выдача не утверждена.")
    else:
        for issue in result["issues"]:
            print(issue["stage"] + ": " + issue["message"])
        forms.alert("MVP не подтверждён: " + result["status"] + "\n"
                    "Пришли JSON и журнал. Если открылась копия QM_DEMO — закрой её\n"
                    "без дополнительного сохранения. Не повторяй запуск поверх неё.")


try:
    main()
except Exception:
    print(traceback.format_exc())
    forms.alert("Ошибка запуска/записи отчёта. Пришли полный журнал.\n"
                "Незавершённую копию QM_DEMO не сохраняй дополнительно.")
