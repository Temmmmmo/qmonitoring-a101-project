# -*- coding: utf-8 -*-
"""Transfer a complete four-direction packet into a COPY, then always roll back."""
from __future__ import print_function, unicode_literals

import datetime
import hashlib
import os
import platform
import traceback

from pyrevit import DB, forms, revit
from System.Collections.Generic import List

from qm_plate_packet import DIRECTIONS, VERSION, load_packet, material_key
from qm_revit_plate_trial import preserve_setup_host_snapshot, run_plate_trial
from qm_revit_probe import Probe, element_id, text_type, write_report_json
from qm_revit_trial import solid_report
from qm_trial_worksharing import confirm_live_local

__title__ = "Full Plate\nTrial"
__doc__ = "Вся плита: четыре направления, реальные Rebar, сверка и обязательный откат. Только копия RVT."


def numbers(text, count):
    values = [float(part.strip().replace(",", ".")) for part in text.split(";")]
    if len(values) != count:
        raise ValueError("Разделяй числа точкой с запятой; неверное количество значений")
    return values


def main():
    print("QMonitoring Full Plate Trial {0}; Python {1} ({2})".format(
        VERSION, platform.python_version(), platform.python_implementation()))
    doc, uidoc = revit.doc, revit.uidoc
    if doc is None or doc.IsFamilyDocument:
        forms.alert("Открой КОПИЮ проекта Revit 2024 и выдели одну плиту.")
        return
    floors = [doc.GetElement(value) for value in uidoc.Selection.GetElementIds()]
    if len(floors) != 1 or not isinstance(floors[0], DB.Floor):
        forms.alert("До нажатия кнопки выдели одну плиту в открытой копии RVT.")
        return
    floor = floors[0]
    source = forms.pick_file(file_ext="json", title="Полный пакет: full-plate-trial.json")
    if not source:
        return
    destination = forms.save_file(file_ext="json", default_name="qmonitoring-full-plate-{0}.json".format(
        datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")), title="Новый JSON с результатами проверки")
    if not destination:
        return
    if os.path.exists(destination):
        forms.alert("Выбери новое имя отчёта: существующие файлы не перезаписываются.")
        return
    report = {"schema_version": "revit-full-plate-trial-report/v1", "version": VERSION,
              "status": "blocked_setup", "placement_eligible": False, "issues": []}
    try:
        packet = load_packet(source)
        with open(source, "rb") as stream:
            digest = hashlib.sha256(stream.read()).hexdigest()
        report["packet_sha256"] = digest
        report["expected"] = packet["expected"]
        report["source_blockers"] = packet["source_blockers"]
        report["document"] = text_type(doc.Title)
        report["host_id"] = element_id(floor.Id)
        probe = Probe(doc, DB)
        report["read_issues"] = probe.issues
        # Preserve the WORKING host even if type/profile selection blocks creation.
        report["host"] = probe.floor(floor)
        report["host_solid"] = solid_report(probe, floor)
        report["coordinate_system"] = "revit-internal-origin-and-axes"
        report["units"] = "mm"
        if probe.issues:
            raise ValueError("Геометрия выбранной плиты прочитана не полностью; смотри read_issues")
        required = {}
        for direction in packet["directions"]:
            for run in direction["runs"]:
                required[material_key(run)] = run
        available = [(value, probe.bar_type(value)) for value in
            DB.FilteredElementCollector(doc).OfClass(DB.Structure.RebarBarType)]
        report["available_bar_types"] = [data for _, data in available]
        choices, missing = {}, []
        for key, run in sorted(required.items()):
            choices[key] = {}
            for value, data in available:
                if (abs(data["nominal_diameter_mm"] - run["diameter_mm"]) < 0.001
                        and abs(data["model_diameter_mm"] - run["diameter_mm"]) < 0.001):
                    label = "{0} | id={1}".format(data["name"], data["element_id"])
                    choices[key][label] = value
            if not choices[key]:
                missing.append(key)
        if missing:
            report["missing_bar_types"] = missing
            raise ValueError("В модели нет подходящих диаметров (номинальный и модельный должны совпадать): " + "; ".join(missing))
        bar_types = {}
        for key in sorted(required):
            selected = forms.SelectFromList.show(sorted(choices[key]), multiselect=False,
                title="Подтверди класс и тип для {0} (класс не определяется по диаметру)".format(key),
                button_name="Использовать тип")
            if not selected:
                raise ValueError("Сопоставление типов отменено; RVT не менялся")
            bar_types[key] = choices[key][selected]
        xy = forms.ask_for_string(prompt="Перенос DXF → внутренние координаты Revit, мм: X; Y.\n"
            "Только сдвиг, без поворота/масштаба. 0; 0 — только при подтверждённом совпадении начала и осей.",
            title="Явная привязка XY")
        if xy is None:
            raise ValueError("Привязка отменена")
        offset_x, offset_y = numbers(xy, 2)
        depths = forms.ask_for_string(prompt="Глубины ОСЕЙ от соответствующей грани плиты, мм:\n"
            "низ X; низ Y; верх X; верх Y\nЭто не защитный слой. Профиль не выбирается автоматически; "
            "зазоры, фон и контакт составных наборов требуют отдельной проверки.", title="Явный профиль высот")
        if depths is None:
            raise ValueError("Профиль высот не задан")
        placement = {"offset_x_mm": offset_x, "offset_y_mm": offset_y,
            "axis_depths_mm": dict(zip(DIRECTIONS, numbers(depths, 4))), "confirmed": True}
        expected = packet["expected"]
        confirmed = forms.alert("Модель: {0}; плита id={1}\n"
            "ВСЯ раскладка: {2} зон, {3} Rebar-наборов, {4} физических стержней, {5:.2f} кг.\n"
            "XY: {6}; {7} мм. Глубины: {8}\n\n"
            "Подтверди КОПИЮ RVT и эти параметры пробного запуска. При выходе за host/проёмы "
            "будет ошибка, без подрезки и частичной укладки. Фон не создаётся и не удаляется.\n"
            "Исходные инженерные блокеры: {9}. Их запуск не снимает.\n"
            "При успешной сверке покажу стержни в текущем 3D-виде; после окна просмотра "
            "локальная геометрия откатывается. RVT не сохраняется. В совместной модели "
            "возврат центрального владения этим откатом не гарантируется; будет отдельное согласие. Продолжить?".format(
                doc.Title, element_id(floor.Id), expected["zone_count"], expected["run_count"],
                expected["physical_bar_count"], expected["additional_mass_kg"], offset_x, offset_y,
                depths, len(packet["source_blockers"])), yes=True, no=True)
        if not confirmed:
            raise ValueError("Пробный запуск отменён; RVT не менялся")
        view = uidoc.ActiveView if isinstance(uidoc.ActiveView, DB.View3D) and not uidoc.ActiveView.IsTemplate else None

        def preview(ids):
            if view is not None:
                values = List[DB.ElementId]()
                for value in ids:
                    values.Add(DB.ElementId(value))
                uidoc.ShowElements(values)
                uidoc.RefreshActiveView()
                forms.alert("Полная раскладка создана и считана после Commit.\n"
                    "За окном — временные Rebar. Это проверка переноса, не инженерное разрешение.\n"
                    "После ОК локальная раскладка откатится. В совместной модели существующий вид "
                    "не меняется, возврат владения в центральной НЕ гарантируется. Сделай скриншот до ОК.")

        setup_report = report
        report = run_plate_trial(doc, DB, floor, packet, bar_types, placement,
            lambda: List[DB.Curve](), lambda: List[DB.CurveLoop](), copy_confirmed=True,
            preview=preview if view is not None else None, view=view,
            worksharing_consent=lambda state: confirm_live_local(state, forms))
        preserve_setup_host_snapshot(report, setup_report)
        report["packet_sha256"] = digest
        for key in ("available_bar_types", "coordinate_system", "document"):
            report.setdefault(key, setup_report[key])
    except Exception as exc:
        report["issues"].append({"stage": "setup", "message": text_type(exc), "traceback": traceback.format_exc()})
    write_report_json(destination, report)
    print("Отчёт: {0}".format(destination))
    print("Статус: {0}".format(report["status"]))
    for item in report["issues"]:
        print("{0}: {1}".format(item["stage"], item["message"]))
    if report.get("ownership_notice"):
        print(report["ownership_notice"])
        forms.alert(report["ownership_notice"])
    if report["status"] in ("rollback_unconfirmed", "restoration_failed"):
        forms.alert("ОТКАТ НЕ ПОДТВЕРЖДЁН. Не сохраняй и не продолжай работу в этой копии.\n"
                    "Закрой её без сохранения и пришли JSON: " + destination)
    else:
        forms.alert("Статус: {0}\nОтчёт: {1}\nЭто не разрешение на постоянное размещение.".format(report["status"], destination))


if __name__ == "__main__":
    main()
