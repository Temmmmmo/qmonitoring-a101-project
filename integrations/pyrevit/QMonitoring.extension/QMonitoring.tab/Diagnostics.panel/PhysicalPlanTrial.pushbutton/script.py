# -*- coding: utf-8 -*-
"""Full physical-bar diagnostic trial with explicit unresolved joints; always rollback."""
from __future__ import print_function, unicode_literals

import datetime
import os
import platform
import traceback

from pyrevit import DB, forms, revit
from System.Collections.Generic import List

from qm_physical_packet import DIRECTIONS, VERSION, load_packet_with_sha256, material_key
from qm_revit_physical_trial import REPORT_SCHEMA, new_report, run_physical_trial
from qm_revit_plate_trial import preserve_setup_host_snapshot
from qm_revit_probe import Probe, element_id, text_type, write_report_json
from qm_revit_trial import solid_report
from qm_trial_worksharing import confirm_live_local

__title__ = "Physical Plan\nTrial"
__doc__ = "Полный физический план, не готовые LayoutZone: нерешённые пересечения, явные XY/высоты, проверка и обязательный откат."


def numbers(text, count):
    values = [float(part.strip().replace(",", ".")) for part in text.split(";")]
    if len(values) != count:
        raise ValueError("Разделяй числа точкой с запятой; неверное количество значений")
    return values


def main():
    print("QMonitoring Physical Plan Trial {0}; Python {1} ({2})".format(
        VERSION, platform.python_version(), platform.python_implementation()))
    doc, uidoc = revit.doc, revit.uidoc
    if doc is None or doc.IsFamilyDocument:
        forms.alert("Открой КОПИЮ рабочего проекта Revit 2024 и выдели одну плиту.")
        return
    floors = [doc.GetElement(value) for value in uidoc.Selection.GetElementIds()]
    if len(floors) != 1 or not isinstance(floors[0], DB.Floor):
        forms.alert("До нажатия кнопки выдели одну рабочую плиту в открытой копии RVT.")
        return
    floor = floors[0]
    source = forms.pick_file(file_ext="json", title="Физический план: physical-bar-plan-trial.json")
    if not source:
        return
    destination = forms.save_file(file_ext="json", default_name="qmonitoring-physical-plan-{0}.json".format(
        datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")), title="Новый JSON диагностики физического плана")
    if not destination:
        return
    if os.path.exists(destination):
        forms.alert("Выбери новое имя отчёта: существующие файлы не перезаписываются.")
        return
    report = {"schema_version": REPORT_SCHEMA, "version": VERSION,
              "status": "blocked_setup", "placement_eligible": False, "engineering_approval": False, "issues": []}
    try:
        packet, packet_sha256 = load_packet_with_sha256(source)
        report = new_report(packet)
        # Hash exactly the validated bytes from the same bounded read, not a later file version.
        report["packet_sha256"] = packet_sha256
        report["packet_sha256_representation"] = "exact-validated-input-bytes"
        report["document"] = text_type(doc.Title)
        report["host_id"] = element_id(floor.Id)
        probe = Probe(doc, DB)
        report["read_issues"] = probe.issues
        # Cancel at the warning/types/profile still returns the actual working host and complete packet.
        report["host"] = probe.floor(floor)
        report["host_solid"] = solid_report(probe, floor)
        report["coordinate_system"] = "revit-internal-origin-and-axes"
        report["units"] = "mm"
        unresolved = len(packet["manual_joint_tasks"])
        proceed = forms.alert("Это ДИАГНОСТИКА физического плана, не готовая инженерная раскладка.\n"
            "Нерешённых пересечений стержней: {0}. Они сохранены в плане и не исправляются запуском.\n"
            "Рабочие границы/проёмы, привязка и 3D-профиль высот пока не подтверждены.\n"
            "Вся пробная геометрия будет удалена обязательным откатом. Продолжить?\n"
            "Нет — сохраню только прочитанную плиту и исходный план в JSON.".format(unresolved), yes=True, no=True)
        if not proceed:
            raise ValueError("Диагностическое создание отменено; геометрия рабочей плиты сохранена в JSON, RVT не менялся")
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
                if (abs(data["nominal_diameter_mm"]-run["diameter_mm"]) < 0.001
                        and abs(data["model_diameter_mm"]-run["diameter_mm"]) < 0.001):
                    choices[key]["{0} | id={1}".format(data["name"], data["element_id"])] = value
            if not choices[key]:
                missing.append(key)
        if missing:
            report["missing_bar_types"] = missing
            raise ValueError("Нет типов с нужными номинальным и модельным диаметрами: " + "; ".join(missing))
        bar_types = {}
        for key in sorted(required):
            selected = forms.SelectFromList.show(sorted(choices[key]), multiselect=False,
                title="Подтверди класс и тип для {0}; класс не определяется по диаметру".format(key),
                button_name="Использовать тип")
            if not selected:
                raise ValueError("Сопоставление типов отменено; RVT не менялся")
            bar_types[key] = choices[key][selected]
        xy = forms.ask_for_string(prompt="Перенос DXF → внутренние координаты Revit, мм: X; Y.\n"
            "Только сдвиг; 0; 0 допустимо лишь при подтверждённом совпадении начала и осей.", title="Явная привязка XY")
        if xy is None:
            raise ValueError("Привязка отменена")
        offset_x, offset_y = numbers(xy, 2)
        depths = forms.ask_for_string(prompt="Глубины ОСЕЙ от соответствующей грани, мм:\n"
            "низ X; низ Y; верх X; верх Y. Это не защитный слой.\n"
            "Глубины автоматически не назначаются. 3D-касания фона и нерешённые стыки этим не согласуются.",
            title="Явный диагностический профиль высот")
        if depths is None:
            raise ValueError("Профиль высот не задан")
        placement = {"offset_x_mm": offset_x, "offset_y_mm": offset_y,
            "axis_depths_mm": dict(zip(DIRECTIONS, numbers(depths, 4))), "confirmed": True}
        expected = packet["expected"]
        confirmed = forms.alert("Модель: {0}; плита id={1}.\n"
            "Источниковых зон: {2}; физических групп исполнения: {3}; Rebar-наборов: {4}.\n"
            "Физических стержней: {5}; масса {6:.2f} кг; типоразмеров прямых стержней: {7}.\n"
            "Нерешённых пересечений: {8}; XY {9}; {10} мм; глубины {11}.\n\n"
            "Подтверди КОПИЮ RVT и эти диагностические параметры.\n"
            "Выход за host/проёмы блокирует ВСЮ партию: без обрезки и пропуска стержней.\n"
            "Фон не создаётся и не удаляется; инженерные блокеры не снимаются.\n"
            "Commit → сверка → просмотр → обязательный откат локальной геометрии. "
            "RVT не сохраняется. В совместной модели возврат центрального владения этим откатом "
            "не гарантируется; будет отдельное согласие. Продолжить?".format(
                doc.Title, element_id(floor.Id), expected["source_zone_count"], expected["execution_group_count"],
                expected["run_count"], expected["physical_bar_count"], expected["additional_mass_kg"],
                expected["position_count"], unresolved, offset_x, offset_y, depths), yes=True, no=True)
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
                forms.alert("Физический план временно создан и считан после Commit.\n"
                    "Нерешённых пересечений: {0}; 3D-укладка и инженерное разрешение не подтверждены.\n"
                    "Сделай скриншот. После ОК локальные стержни откатятся. В совместной модели "
                    "существующий вид не меняется, возврат владения в центральной НЕ гарантируется.".format(unresolved))

        setup_report = report
        report = run_physical_trial(doc, DB, floor, packet, bar_types, placement,
            lambda: List[DB.Curve](), lambda: List[DB.CurveLoop](), copy_confirmed=True,
            preview=preview if view is not None else None, view=view,
            worksharing_consent=lambda state: confirm_live_local(state, forms))
        preserve_setup_host_snapshot(report, setup_report)
        for key in ("packet_sha256", "packet_sha256_representation", "available_bar_types", "coordinate_system", "document"):
            report.setdefault(key, setup_report[key])
    except Exception as exc:
        report["issues"].append({"stage": "setup", "message": text_type(exc), "traceback": traceback.format_exc()})
    write_report_json(destination, report)
    print("Отчёт: {0}".format(destination))
    print("Статус диагностики: {0}; нерешённых пересечений: {1}".format(
        report["status"], report.get("unresolved_intersection_pair_count", "пакет не прочитан")))
    for item in report["issues"]:
        print("{0}: {1}".format(item["stage"], item["message"]))
    if report.get("ownership_notice"):
        print(report["ownership_notice"])
        forms.alert(report["ownership_notice"])
    if report["status"] in ("rollback_unconfirmed", "restoration_failed"):
        forms.alert("ОТКАТ НЕ ПОДТВЕРЖДЁН. Не сохраняй и не продолжай работу в этой копии.\n"
                    "Закрой без сохранения и пришли JSON: " + destination)
    else:
        forms.alert("Диагностика: {0}\nНерешённых пересечений: {1}.\n"
            "Host/Z и инженерная 3D-укладка не считаются согласованными.\n"
            "Отчёт: {2}\nЭто НЕ разрешение на постоянное размещение.".format(
                report["status"], report.get("unresolved_intersection_pair_count", "не определено"), destination))


if __name__ == "__main__":
    main()
