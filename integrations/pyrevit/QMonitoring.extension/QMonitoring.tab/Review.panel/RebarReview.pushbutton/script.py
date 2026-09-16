# -*- coding: utf-8 -*-
"""Whole graphic party -> native straight Rebar review. Revit 2024 / pyRevit."""
from __future__ import print_function, unicode_literals

import datetime
import os
import platform
import traceback

from pyrevit import DB, forms, revit
from System.Collections.Generic import List

from qm_plate_packet import DIRECTIONS
from qm_rebar_review import (REPORT_SCHEMA, VERSION, material_key, validated_graphics)
from qm_revit_plan_preview import load_preview_input
from qm_revit_probe import Probe, element_id, text_type, write_report_json
from qm_revit_rebar_review import run_rebar_review

__title__ = "Rebar Review\nMVP"
__doc__ = "Вся прямая партия native Rebar в копии: Commit, readback и отдельное keep. Не инженерный выпуск."


def numbers(text,count):
    values = [float(part.strip().replace(",",".")) for part in text.split(";")]
    if len(values) != count:
        raise ValueError("Разделяй числа точкой с запятой; неверное количество значений")
    return values


def confirm_review_worksharing(state):
    workset = state["before"]["host_workset"]
    return forms.alert("Это ЖИВАЯ ЛОКАЛЬНАЯ workshared-модель. Запрошу выбранную Floor id={0}; "
        "Revit может заимствовать связанные элементы. Новые Rebar попадут в workset «{1}» (id={2}).\n"
        "После проверки ты сможешь откатить ВСЮ партию либо ОСТАВИТЬ её локально. "
        "Rollback/Keep не возвращает центральное владение автоматически. Save/Sync/Relinquish не вызываются.\n"
        "Безопаснее detached COPY с Preserve Worksets. Разрешить checkout в этой локальной копии?".format(
            state["before"]["elements"][0]["element_id"],workset["name"],workset["id"]),yes=True,no=True) is True


def main():
    print("QMonitoring Rebar Review MVP {0}; Python {1} ({2})".format(
        VERSION,platform.python_version(),platform.python_implementation()))
    doc,uidoc = revit.doc,revit.uidoc
    report = {"schema_version":REPORT_SCHEMA,"version":VERSION,"status":"blocked_setup",
        "placement_eligible":False,"engineering_approval":False,"issues":[]}
    destination = None
    try:
        if doc is None or doc.IsFamilyDocument:
            raise ValueError("Открой КОПИЮ рабочего проекта Revit 2024")
        floors = [doc.GetElement(value) for value in uidoc.Selection.GetElementIds()]
        if len(floors) != 1 or not isinstance(floors[0],DB.Floor):
            raise ValueError("До запуска выдели ровно одну native Floor активного документа")
        floor = floors[0]
        source = forms.pick_file(file_ext="json",title="Полная прямая партия: graphic-bar-plan-draft/pruned/repaired.json")
        if not source:
            return
        destination = forms.save_file(file_ext="json",default_name="qmonitoring-rebar-review-{0}.json".format(
            datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")),title="Новое имя обязательного отчёта Rebar Review")
        if not destination:
            raise ValueError("Без пути нового JSON-отчёта создание Rebar запрещено")
        if os.path.exists(destination):
            raise ValueError("Существующий отчёт не перезаписывается")
        packet,digest = load_preview_input(source)
        offset = forms.ask_for_string(prompt="DXF → Revit, мм: X; Y. Только перенос; масштаб/поворот не угадываются.",
            title="Явная XY-привязка")
        if offset is None:
            raise ValueError("XY-привязка отменена")
        offset_x,offset_y = numbers(offset,2)
        primitives = validated_graphics(packet,offset_x,offset_y)
        report.update({"source_input_sha256":digest,"source_schema":packet["schema_version"],
            "case_id":packet["case_id"],"expected":primitives["summary"]})
        probe = Probe(doc,DB)
        available = [(value,probe.bar_type(value)) for value in
            DB.FilteredElementCollector(doc).OfClass(DB.Structure.RebarBarType)]
        required = {}
        for bar in primitives["bars"]:
            required[material_key(bar)] = bar
        selected = {}
        report["available_bar_types"] = [row for _,row in available]
        for key,bar in sorted(required.items()):
            choices = {}
            for value,data in available:
                if (abs(data["nominal_diameter_mm"]-bar["diameter_mm"]) < .001
                        and abs(data["model_diameter_mm"]-bar["diameter_mm"]) < .001):
                    choices["{0} | id={1}".format(data["name"],data["element_id"])] = value
            if not choices:
                raise ValueError("Нет точного RebarBarType по nominal/model D для "+key)
            label = forms.SelectFromList.show(sorted(choices),multiselect=False,
                title="Подтверди сталь/тип для "+key,button_name="Использовать этот тип")
            if not label:
                raise ValueError("Выбор типа отменён")
            selected[key] = choices[label]
        depth_text = forms.ask_for_string(prompt="Глубины ОСЕЙ от нижней/верхней native-грани, мм:\n"
            "низ X; низ Y; верх X; верх Y. Это НЕ cover и не высота DXF.",title="Явные оси четырёх направлений")
        if depth_text is None:
            raise ValueError("Глубины осей отменены")
        depths = dict(zip(DIRECTIONS,numbers(depth_text,4)))
        checks = primitives["trim_graphics"]["checks"]
        conditional = checks.get("conditional_collisions_3d", checks["collisions_3d"])
        collision_notice = "Backend conditional 3D={0}; proven={1}; uncertain={2}; actual RVT NOT CHECKED.".format(
            conditional["status"],conditional["proven_pair_count"],conditional["uncertain_pair_count"])
        confirmed = forms.alert("КОПИЯ RVT: {0}; Floor id={1}.\n"
            "Создам ВСЮ прямую партию: {2} отдельных native Rebar; расчётное время зависит от размера (до 5000).\n"
            "Coverage={3}; 40d={4}; stock={5}. Fail/not_checked НЕ снимаются.\n{6}\n"
            "MVP проверяет native ПЛОСКИЙ внешний контур и толщину. Отверстия, cover, перепады и фон ИСКЛЮЧЕНЫ.\n"
            "Не удаляю/не меняю существующую арматуру, не Save/Sync. После Commit будет строгий readback и ОТДЕЛЬНЫЙ вопрос keep.\n"
            "Подтверди, что это локальная/отсоединённая КОПИЯ для диагностического review.".format(
                doc.Title,element_id(floor.Id),len(primitives["bars"]),checks["coverage"],
                checks["anchorage_40d"],checks["stock_cutting"],collision_notice),yes=True,no=True)
        if not confirmed:
            raise ValueError("Копия/полный диагностический запуск не подтверждены")

        def preview(ids):
            values = List[DB.ElementId]()
            for value in ids:
                values.Add(DB.ElementId(value))
            uidoc.ShowElements(values)
            uidoc.RefreshActiveView()

        def keep(result):
            return forms.alert("Post-Commit readback совпал для ВСЕХ {0} Rebar.\n"
                "Coverage={1}; 40d={2}; stock={3}; holes/cover/background NOT CHECKED.\n{4}\n"
                "ОСТАВИТЬ диагностическую арматуру в этой КОПИИ? Это не выпуск и не Save/Sync.\n"
                "Нет = откатить всю созданную партию.".format(len(result["created_element_ids"]),
                    checks["coverage"],checks["anchorage_40d"],checks["stock_cutting"],collision_notice),yes=True,no=True)

        report = run_rebar_review(doc,DB,floor,primitives,selected,depths,lambda:List[DB.Curve](),
            keep,copy_confirmed=True,worksharing_consent=confirm_review_worksharing,preview=preview)
        report.update({"source_input_sha256":digest,"source_file_name":os.path.basename(source),
            "coordinate_offset_xy_mm":[offset_x,offset_y]})
    except Exception as exc:
        report["issues"].append({"stage":"setup","message":text_type(exc),"traceback":traceback.format_exc()})
    if destination:
        try:
            write_report_json(destination,report)
            print("Обязательный отчёт: "+destination)
        except Exception as exc:
            print("ОШИБКА ЗАПИСИ ОТЧЁТА: "+text_type(exc))
            if report.get("status") == "kept_diagnostic_rebar_review":
                forms.alert("АРМАТУРА ОСТАВЛЕНА, НО JSON НЕ ЗАПИСАН.\nIDs: {0}\n"
                    "Выполни Undo либо закрой КОПИЮ без сохранения; пришли traceback из pyRevit.".format(
                        ", ".join(str(v) for v in report.get("kept_element_ids",[]))))
                raise
    if report["status"] == "kept_diagnostic_rebar_review":
        forms.alert("В КОПИИ оставлено {0} диагностических Rebar. Модель НЕ сохранена/не синхронизирована.\n"
            "Это НЕ инженерное разрешение. Отчёт: {1}\n{2}".format(
                len(report["kept_element_ids"]),destination,report.get("ownership_notice","")))
    elif report["status"] in ("rollback_unconfirmed","restoration_failed"):
        forms.alert("ОТКАТ НЕ ПОДТВЕРЖДЁН. Закрой КОПИЮ без сохранения и пришли JSON: "+text_type(destination))
    else:
        forms.alert("Rebar не оставлен. Статус: {0}. Отчёт: {1}".format(report["status"],destination))


if __name__ == "__main__":
    main()
