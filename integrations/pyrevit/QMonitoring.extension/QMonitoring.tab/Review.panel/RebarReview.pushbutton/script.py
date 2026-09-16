# -*- coding: utf-8 -*-
"""Whole graphic party -> native straight Rebar review. Revit 2024 / pyRevit."""
from __future__ import print_function, unicode_literals

import datetime
import os
import platform
import traceback
import tempfile
import uuid
import json

from pyrevit import DB, forms, revit
from System.Collections.Generic import List

from qm_plate_packet import DIRECTIONS
from qm_rebar_review import (REPORT_SCHEMA, VERSION, material_key, validated_graphics,
    flat_outer_host, select_axis_depth_policy)
from qm_revit_plan_preview import load_preview_input
from qm_revit_probe import Probe, element_id, text_type, write_report_json
from qm_revit_rebar_review import run_rebar_review
from qm_revit_presentation_view import create_presentation_view

__title__ = "Раскладка\nарматуры"
__doc__ = "Создать арматуру в копии проекта, проверить и отдельно оставить результат. Требует проверки конструктора."


def alert(message,**options):
    return forms.alert(message,title="QMonitoring",**options)


def check_label(value):
    return {"pass":"пройдено в исходном расчёте","fail":"есть замечания",
        "not_checked":"не проверено"}.get(value,text_type(value))


def friendly_stop_reason(report):
    """Technical evidence stays intact in console/JSON, not a long user dialog."""
    issues = report.get("issues",[])
    issue = issues[0] if issues else {}
    stage, message = issue.get("stage","setup"),text_type(issue.get("message",""))
    if "existing compatible straight RebarShape" in message:
        return "В проекте нет подходящей прямой формы арматуры. Загрузите её и повторите запуск."
    if stage == "setup" and message and all(ord(char) < 128 for char in message):
        return "Не удалось подготовить раскладку. Проверьте выбранную плиту и JSON; подробная причина сохранена в отчёте."
    if stage == "setup" and message:
        return message.replace("КОПИЮ","копию").replace("native Floor","плиту Revit")
    reasons = {
        "preflight":"Не удалось проверить исходные настройки. Проверьте копию проекта, выбранную плиту и типы арматуры.",
        "worksharing_authorization":"Не удалось получить доступ к элементам. Проверьте рабочие наборы в локальной копии.",
        "create_whole_party":"Не удалось создать всю партию. Проверьте прямую форму и типы арматуры; подробности в отчёте.",
        "constrain_new_axes":"Не удалось согласовать оси арматуры в Revit. Для демонстрации можно выбрать режим презентации с замечаниями.",
        "pre_commit_readback":"Сверка созданной арматуры не завершилась. Проверьте замечания в отчёте перед повторным запуском.",
        "post_commit_readback":"Итоговая сверка выявила замечания, не допускаемые выбранным режимом. Проверьте отчёт.",
        "explicit_keep_confirmation":"Оставление не подтверждено. Созданная партия удалена откатом; можно повторить запуск.",
        "commit":"Revit не подтвердил создание партии. Подробная причина сохранена в отчёте.",
        "optional_presentation_view":"Не удалось безопасно завершить создание 3D-вида. Проверьте отчёт перед повторным запуском."}
    return reasons.get(stage,"Не удалось завершить раскладку. Проверьте отчёт перед повторным запуском.")


def numbers(text,count):
    values = [float(part.strip().replace(",",".")) for part in text.split(";")]
    if len(values) != count:
        raise ValueError("Разделяй числа точкой с запятой; неверное количество значений")
    return values


def confirm_review_worksharing(state):
    workset = state["before"]["host_workset"]
    return alert("Это локальная копия совместного проекта. Запрошу доступ к выбранной плите (ID {0}); "
        "Revit может заимствовать связанные элементы. Новая арматура попадёт в рабочий набор «{1}» (ID {2}).\n"
        "После проверки можно оставить результат или откатить всю партию. Владение элементами "
        "не возвращается автоматически; сохранения и синхронизации нет.\n"
        "Безопаснее отсоединённая копия с сохранёнными рабочими наборами. Разрешить доступ в этой локальной копии?".format(
            state["before"]["elements"][0]["element_id"],workset["name"],workset["id"]),yes=True,no=True) is True


def main():
    print("QMonitoring Rebar Review MVP {0}; Python {1} ({2})".format(
        VERSION,platform.python_version(),platform.python_implementation()))
    doc,uidoc = revit.doc,revit.uidoc
    report = {"schema_version":REPORT_SCHEMA,"version":VERSION,"status":"blocked_setup",
        "placement_eligible":False,"engineering_approval":False,"issues":[]}
    # A pre-dialog setup failure must still have a concrete, non-overwriting
    # report destination and an immediately visible traceback.
    destination = os.path.join(tempfile.gettempdir(), "qmonitoring-rebar-review-setup-{0}.json".format(uuid.uuid4().hex))
    try:
        if doc is None or doc.IsFamilyDocument:
            raise ValueError("Откройте копию рабочего проекта Revit 2024 и повторите запуск")
        floors = [doc.GetElement(value) for value in uidoc.Selection.GetElementIds()]
        if len(floors) != 1 or not isinstance(floors[0],DB.Floor):
            raise ValueError("Выделите одну плиту Revit в текущем проекте и повторите запуск")
        floor = floors[0]
        source = forms.pick_file(file_ext="json",title="Выберите JSON раскладки с сайта")
        if not source:
            raise ValueError("Выбор JSON отменён; арматура не создавалась")
        requested_destination = forms.save_file(file_ext="json",default_name="qmonitoring-rebar-review-{0}.json".format(
            datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")),title="Куда сохранить отчёт проверки")
        if not requested_destination:
            raise ValueError("Выберите путь нового отчёта перед созданием арматуры")
        if os.path.exists(requested_destination):
            raise ValueError("Существующий отчёт не перезаписывается")
        destination = requested_destination
        packet,digest = load_preview_input(source)
        offset = forms.ask_for_string(prompt="Смещение раскладки относительно проекта, мм: X; Y.\nДля проверенной плиты К09: 0;0. Масштаб и поворот не меняются.",
            title="Привязка к проекту")
        if offset is None:
            raise ValueError("Привязка к проекту отменена; можно повторить запуск")
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
                raise ValueError("Нет типа арматуры с точным диаметром для "+key+". Загрузите подходящий тип и повторите запуск.")
            label = forms.SelectFromList.show(sorted(choices),multiselect=False,
                title="Выберите тип арматуры для "+key,button_name="Использовать этот тип")
            if not label:
                raise ValueError("Выбор типа отменён")
            selected[key] = choices[label]
        native_host = flat_outer_host(probe.floor(floor))
        selected_rows = dict((key,probe.bar_type(value)) for key,value in selected.items())

        def choose_depths(proposal,error):
            report["automatic_depth_proposal_error"] = text_type(error) if error is not None else None
            if proposal is not None:
                values = ";".join(text_type(proposal["computed_depths_mm"][d]) for d in DIRECTIONS)
                message = "Предлагаемые глубины осей: {0} мм\nниз X; низ Y; верх X; верх Y.\n".format(values)
            else:
                message = "Автоматический профиль не подходит этой плите. Выберите другие настройки или отмените запуск.\n"
            message += ("X ближе к граням, Y глубже. Отступ тела от грани 40 мм, зазор между слоями 4 мм.\n"
                "Это расстояния до осей, не защитный слой и не норматив. Требует проверки конструктора.")
            options = (["Использовать предлагаемый профиль"] if proposal is not None else [])+["Другие настройки","Отмена"]
            choice = alert(message,options=options,ok=False)
            report["axis_depth_policy"] = dict(proposal) if proposal is not None else {"schema_version":"revit-axis-depth-policy/v1","mode":"not_selected"}
            report["computed_axis_depths_mm"] = proposal["computed_depths_mm"] if proposal is not None else None
            if choice == "Другие настройки":
                report["axis_depth_policy"].update(mode="manual",profile_id=None,user_confirmed=False,
                    actual_depths_mm=None,automatic_gap_policy_applied=False,auto_proposal_error=error)
            return "auto" if choice == "Использовать предлагаемый профиль" else "manual" if choice == "Другие настройки" else None

        def ask_manual(proposal):
            return forms.ask_for_string(prompt="Глубины осей от граней плиты, мм:\n"
                "низ X; низ Y; верх X; верх Y. Это расстояние до оси, не защитный слой. Зазор 4 мм для ручных настроек не подтверждается автоматически.",
                title="Дополнительная ручная настройка")

        depth_policy = select_axis_depth_policy(primitives,native_host,selected_rows,choose_depths,ask_manual)
        depths = depth_policy["actual_depths_mm"]
        report.update(axis_depth_policy=depth_policy,axis_depths_mm=depths,
            computed_axis_depths_mm=depth_policy["computed_depths_mm"])
        checks = primitives["trim_graphics"]["checks"]
        conditional = checks.get("conditional_collisions_3d", checks["collisions_3d"])
        collision_notice = "Исходная пространственная проверка: {0}; подтверждённых пар {1}, неопределённых {2}. Коллизии Revit при выбранных глубинах не проверены.".format(
            check_label(conditional["status"]),conditional["proven_pair_count"],conditional["uncertain_pair_count"])
        presentation_label = "Для презентации — показать с замечаниями"
        strict_label = "Строгая сверка осей"
        mode_choice = alert("Выберите режим. В презентации измеренные сдвиги можно оставить с замечаниями после отдельного подтверждения.\n"
            "Результат требует проверки конструктора. Замечания сохранятся в отчёте.",
            options=[presentation_label,strict_label,"Отмена"],ok=False)
        if mode_choice not in (presentation_label,strict_label):
            raise ValueError("Выбор режима отменён; арматура не создавалась")
        review_mode = "presentation" if mode_choice == presentation_label else "strict"
        report["review_mode"] = review_mode
        confirmed = alert("В копии «{0}» будет создано {1} прямых стержней. Операция может занять время.\n"
            "Существующая арматура не меняется. Сохранения и синхронизации нет.\n"
            "После сверки можно отдельно оставить результат. Требует проверки конструктора; подробные проверки будут в отчёте.\n"
            "Подтвердите, что это локальная или отсоединённая копия.".format(
                doc.Title,len(primitives["bars"])),options=["Создать арматуру","Отмена"],ok=False) == "Создать арматуру"
        if not confirmed:
            raise ValueError("Копия/полный диагностический запуск не подтверждены")

        captured_view = {}
        def preview(ids):
            if review_mode == "presentation":
                directions = dict((value,bar["direction"]) for value,bar in zip(ids,primitives["bars"]))
                captured_view.update(create_presentation_view(doc,DB,floor,ids,directions))
                return
            values = List[DB.ElementId]()
            for value in ids:
                values.Add(DB.ElementId(value))
            uidoc.ShowElements(values)
            uidoc.RefreshActiveView()

        def keep(result):
            summary = result.get("presentation_deviations",{})
            native_notice = "Полная строгая сверка осей совпала."
            if review_mode == "presentation":
                native_notice = "С замечаниями: {0} стержней; максимальный сдвиг {1:.3f} мм; фактическая масса {2:.3f} кг.".format(
                    summary["deviating_bar_count"],summary["max_endpoint_delta_mm"],summary["actual_mass_from_axes_kg"])
            return alert(native_notice+"\nПеречитаны данные всех {0} созданных стержней.\n"
                "Требует проверки конструктора. Подробные проверки остаются в отчёте.\n"
                "Оставить результат в копии? Сохранения и синхронизации нет.".format(
                    len(result["created_element_ids"])),options=["Оставить","Откатить"],ok=False) == "Оставить"

        report = run_rebar_review(doc,DB,floor,primitives,selected,depths,lambda:List[DB.Curve](),
            keep,copy_confirmed=True,worksharing_consent=confirm_review_worksharing,preview=preview,axis_depth_policy=depth_policy,review_mode=review_mode)
        report.update({"source_input_sha256":digest,"source_file_name":os.path.basename(source),
            "coordinate_offset_xy_mm":[offset_x,offset_y],"source_check_notice":collision_notice})
        if review_mode == "presentation":
            report["presentation_view"] = captured_view or {"status":"not_created","error":report.get("preview_error")}
            if report.get("kept_element_ids") and captured_view.get("view_id"):
                captured_view["status"] = "kept_presentation_view"
                try:
                    uidoc.ActiveView = doc.GetElement(DB.ElementId(captured_view["view_id"]))
                    for ui_view in uidoc.GetOpenUIViews():
                        if element_id(ui_view.ViewId) == captured_view["view_id"]:
                            ui_view.ZoomToFit()
                    captured_view["activation"] = "activated_after_whole_party_keep"
                except Exception as exc:
                    captured_view["activation_error"] = {"message":text_type(exc),"traceback":traceback.format_exc()}
                    print("Арматура оставлена, но 3D-вид не удалось открыть: "+text_type(exc))
            elif captured_view:
                captured_view["status"] = "rolled_back_with_whole_party" if report["status"] == "failed_rolled_back" else "whole_party_rollback_unconfirmed"
    except Exception as exc:
        report["issues"].append({"stage":"setup","message":text_type(exc),"traceback":traceback.format_exc()})
        print("ПРИЧИНА ОТКАЗА: "+text_type(exc))
        print(traceback.format_exc())
    if destination:
        report_path_notice = destination
        try:
            write_report_json(destination,report)
            print("Обязательный отчёт: "+destination)
        except Exception as exc:
            report_path_notice = "JSON НЕ ЗАПИСАН: "+destination
            print("ОШИБКА ЗАПИСИ ОТЧЁТА: "+text_type(exc))
            print(traceback.format_exc())
            print(json.dumps(report,ensure_ascii=False,sort_keys=True,default=text_type))
            if report.get("status") in ("kept_diagnostic_rebar_review","kept_presentation_rebar_with_deviations"):
                alert("Арматура оставлена, но отчёт не удалось записать.\nID стержней: {0}\n"
                    "Выполните Undo или закройте копию без сохранения. Подробная ошибка — в выводе pyRevit.".format(
                        ", ".join(str(v) for v in report.get("kept_element_ids",[]))))
                raise
    if report["status"] in ("kept_diagnostic_rebar_review","kept_presentation_rebar_with_deviations"):
        finish_notice = "Требует проверки конструктора."
        if report.get("review_mode") == "presentation":
            summary = report["presentation_deviations"]
            view_info = report.get("presentation_view",{})
            view_notice = "3D-вид открыт." if view_info.get("activation") == "activated_after_whole_party_keep" else "3D-вид не открыт; подробности в JSON."
            finish_notice = "Требует проверки конструктора.\nС замечаниями: {0} стержней; максимальный сдвиг {1:.3f} мм; фактическая масса {2:.3f} кг.\n{3}".format(
                summary["deviating_bar_count"],summary["max_endpoint_delta_mm"],summary["actual_mass_from_axes_kg"],view_notice)
        alert("В копии оставлено {0} стержней.\n{1}\n"
            "Отчёт: {2}\nМодель не сохранена и не синхронизирована. Можно посмотреть результат и сделать снимок вида.\n{3}".format(
                len(report["kept_element_ids"]),finish_notice,destination,
                "Проверьте владение элементами рабочего набора; оно не возвращается автоматически."
                if report.get("worksharing",{}).get("mode") == "live_local" else ""))
    elif report["status"] in ("rollback_unconfirmed","restoration_failed"):
        alert("Не удалось подтвердить откат. Закройте копию без сохранения и передайте отчёт: "+text_type(destination))
    else:
        alert("Арматура не оставлена.\n{0}\nОтчёт: {1}".format(friendly_stop_reason(report),report_path_notice))


if __name__ == "__main__":
    main()
