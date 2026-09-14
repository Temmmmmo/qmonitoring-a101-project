# -*- coding: utf-8 -*-
"""Full graphic plan in a NEW drafting view, never reinforcement placement."""
from __future__ import print_function, unicode_literals

import datetime
import os
import platform
import traceback

from pyrevit import DB, forms, revit
from System.Collections.Generic import List

from qm_revit_plan_preview import (DISCLAIMER, REPORT_SCHEMA, VERSION, build_preview_primitives,
    create_plan_preview, load_preview_input)
from qm_revit_probe import element_id, text_type, write_report_json
from qm_trial_input import number

__title__ = "Plan\nPreview"
__doc__ = "Все четыре направления текущей раскладки в НОВОМ чертёжном виде. Линии, не Rebar; проблемные стержни показаны, не пропущены."


def main():
    print("QMonitoring Plan Preview {0}; Python {1} ({2})".format(
        VERSION, platform.python_version(), platform.python_implementation()))
    doc, uidoc = revit.doc, revit.uidoc
    if doc is None or doc.IsFamilyDocument:
        forms.alert("Открой рабочий проект Revit 2024 и выдели одну плиту. Рабочие наборы удалять не нужно.")
        return
    selection = [doc.GetElement(identifier) for identifier in uidoc.Selection.GetElementIds()]
    if len(selection) != 1 or not isinstance(selection[0], DB.Floor):
        forms.alert("Перед запуском выдели одну рабочую плиту. Типы арматуры и глубины для графического просмотра не нужны.")
        return
    source = forms.pick_file(file_ext="json", title="Полный физический план или черновик обхода отверстий")
    if not source:
        return
    destination = forms.save_file(file_ext="json", default_name="qmonitoring-plan-preview-{0}.json".format(
        datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")), title="Новый JSON результата графического просмотра")
    if not destination:
        return
    if os.path.exists(destination):
        forms.alert("Выбери новое имя: существующие отчёты не перезаписываются.")
        return
    report = {"schema_version": REPORT_SCHEMA, "version": VERSION, "status": "blocked_setup",
        "placement_eligible": False, "engineering_approval": False, "created_view_id": None, "issues": []}
    try:
        packet, digest = load_preview_input(source)
        report["packet_sha256"] = digest
        hint = packet.get("source_to_revit_xy_mm")
        default = "{0}; {1}".format(*hint) if hint is not None else ""
        entered = forms.ask_for_string(default=default, prompt="Сдвиг из координат DXF в ВНУТРЕННИЕ координаты Revit, мм: X; Y.\n"
            "Для проверенного рабочего снимка 122250: 0; 0. Для другой модели это не автоматическая привязка.\n"
            "Только графический просмотр; Z из DXF не используется.", title="Подтверди графическую привязку XY")
        if entered is None:
            raise ValueError("Привязка отменена; новый вид не создавался")
        values = [float(part.strip().replace(",", ".")) for part in entered.split(";")]
        if len(values) != 2:
            raise ValueError("Нужны два числа через точку с запятой: X; Y")
        for value in values:
            number(value, -100000000, 100000000)
        primitives = build_preview_primitives(packet, *values)
        expected = primitives["summary"]
        confirmed = forms.alert("{0}\n\nМодель: {1}; плита id={2}.\n"
            "Покажу ВСЕ четыре направления: {3} стержней, {4:.2f} кг расчётной массы, {5} исходных зон.\n"
            "Создам ОДИН НОВЫЙ чертёжный вид с линиями, текстом и контурами этой плиты. Это НЕ физическая арматура.\n"
            "Проблемные стержни останутся на схеме красными; непройденные проверки — оранжевыми.\n"
            "Ни плиту, ни существующие виды, типы или активный рабочий набор не изменяю.\n"
            "Рабочие наборы сохраняются. Заимствование плиты, Save и Sync не вызываются.\n"
            "Новый вид останется в локальном документе и может попасть в центральную модель при ТВОЕЙ последующей синхронизации.\n"
            "XY: {6}; {7} мм. Для удаления результата можно отменить одну операцию или удалить только новый вид.\n\n"
            "Создать этот графический вид?".format(DISCLAIMER, doc.Title, element_id(selection[0].Id),
                expected["physical_bar_count"], expected["additional_mass_kg"], expected["source_zone_count"], *values),
            yes=True, no=True)
        if confirmed is not True:
            raise ValueError("Создание вида отменено; RVT не менялся")
        with forms.ProgressBar(title="QMonitoring: {value}/{max_value}", cancellable=True) as progress_bar:
            def progress(stage, value, total):
                progress_bar.update_progress(value, total)
                return not progress_bar.cancelled
            report = create_plan_preview(doc, DB, selection[0], primitives,
                lambda: List[DB.CurveLoop](), confirmed=True, progress=progress)
        report["packet_sha256"] = digest
        report["packet_sha256_representation"] = "exact-validated-input-bytes"
        report["document"] = text_type(doc.Title)
        if report["status"] == "graphic_preview_created":
            try:
                view = doc.GetElement(DB.ElementId(report["created_view_id"]))
                uidoc.ActiveView = view
                uidoc.RefreshActiveView()
                for ui_view in uidoc.GetOpenUIViews():
                    if ui_view.ViewId == view.Id:
                        ui_view.ZoomToFit()
                        break
            except Exception as exc:
                report["issues"].append({"stage": "open_new_view", "message": text_type(exc)})
    except Exception as exc:
        report["issues"].append({"stage": "setup", "message": text_type(exc), "traceback": traceback.format_exc()})
    try:
        write_report_json(destination, report)
        print("JSON: "+destination)
    except Exception as exc:
        print("Не удалось сохранить JSON: "+text_type(exc))
        forms.alert("JSON не сохранён: {0}\nСтатус: {1}; созданный вид id={2}.\n"
            "Если вид создан, он остаётся в документе. Пришли текст ошибки; не повторяй запуск для сохранения отчёта.".format(
                text_type(exc), report["status"], report.get("created_view_id")))
        return
    print("Статус: {0}; новый графический вид id={1}".format(report["status"], report.get("created_view_id")))
    for issue in report["issues"]:
        print("{0}: {1}".format(issue["stage"], issue["message"]))
    if report["status"] == "graphic_preview_created":
        forms.alert("Создан НОВЫЙ чертёжный вид: {0}\n"
            "Четыре направления, вся партия без пропусков. Это линии, НЕ Rebar и НЕ инженерная выдача.\n"
            "Сделай скриншот и пришли JSON: {1}".format(report["view_name"], destination))
    elif report["status"] == "rollback_unconfirmed":
        forms.alert("Откат НЕ подтверждён. Не сохраняй и не синхронизируй эту копию до проверки.\nПришли JSON: "+destination)
    else:
        forms.alert("Вид не создан / создание отменено: {0}\nПришли JSON: {1}".format(report["status"], destination))


if __name__ == "__main__":
    main()
