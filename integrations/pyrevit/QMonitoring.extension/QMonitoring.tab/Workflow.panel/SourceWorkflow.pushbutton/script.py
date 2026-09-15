# -*- coding: utf-8 -*-
"""TZ 8.1: explicit source-zone detail/annotation family binding on current plan."""
from __future__ import print_function, unicode_literals

import datetime
import hashlib
import os
import traceback

from pyrevit import DB, forms, revit

from qm_revit_plan_preview import load_preview_input
from qm_revit_probe import element_id, text_type, write_report_json
from qm_workflow_81 import (VERSION, DISCLAIMER, DIRECTIONS, ALGORITHMS, LENGTH_FIELDS,
    ZONE_FIELDS, make_request, settings, source_components, symbol_catalog, prefix_candidates, analysis_caption)
from qm_workflow_81_native import preflight, inspect_parameters, place_source_families
from qm_workflow_81_transport import (read_source, calculation_request, server_endpoint,
    post_calculation, decode_analysis, MAX_RESPONSE_BYTES)

__title__ = "Доп. поля\nWorkflow 8.1"
__doc__ = "Настройки расчёта и параметрические исходные зоны на текущем плане. Не физический Rebar."


def ask(prompt, default=""):
    answer = forms.ask_for_string(default=default, prompt=prompt, title="QMonitoring · Workflow 8.1")
    if answer is None:
        raise ValueError("Операция отменена; размещение не подтверждено")
    return answer


def select(values, title):
    answer = forms.SelectFromList.show(values, title=title, multiselect=False)
    if answer is None:
        raise ValueError("Выбор отменён")
    return answer


def select_family(doc, view, role):
    catalog = symbol_catalog(doc, DB, role)
    if not catalog:
        raise ValueError("Нет подходящего загруженного семейства: {0}. Загрузите через Revit семейство "
            "элементов узлов (для зоны) / типовых аннотаций (для подписи); типовые/3D/Rebar семейства не подменяются.".format(role))
    prefix = ask("Префикс имени семейства {0}. Один совпавший тип выбирается автоматически; "
        "несколько — предлагаются списком. Пустой префикс показывает все.".format(role))
    matches = prefix_candidates(catalog, prefix)
    if not matches["matches"]:
        raise ValueError("По префиксу нет подходящих семейств: "+prefix)
    labels = {}
    for row in matches["matches"]:
        labels["{0} : {1} | id={2}".format(row["family"], row["type"], row["id"])] = row
    if matches["automatic_id"] is not None:
        chosen = matches["matches"][0]
    else:
        chosen = labels[select(sorted(labels), "Семейство "+role)]
    if not forms.alert("Прочитаю параметры типа {0} : {1} через ВРЕМЕННЫЙ экземпляр на текущем плане.\n"
            "Он будет полностью отменён до продолжения. Тип может временно активироваться.\n"
            "Рабочие наборы не переключаю; Checkout/Save/Sync не вызываются.\nПродолжить?".format(
                chosen["family"], chosen["type"]), yes=True, no=True):
        raise ValueError("Чтение семейства отменено")
    inspection = inspect_parameters(doc, DB, view, chosen["id"], role, confirmed=True)
    fields = ZONE_FIELDS if role == "zone" else ("annotation",)
    binding, used = {}, set()
    for field in fields:
        if field == "length_mm" and chosen["placement"] == "CurveBasedDetail":
            binding[field] = "curve-length"
            continue
        expected = "length_mm" if field in LENGTH_FIELDS else ("integer" if field == "bar_count" else "text")
        options = {}
        for parameter in inspection["parameters"]:
            if parameter["kind"] == expected and parameter["id"] not in used:
                options["{0} | id={1} | {2}".format(parameter["name"], parameter["id"], expected)] = parameter["id"]
        if not options:
            raise ValueError("У семейства нет отдельного доступного параметра экземпляра для "+field)
        binding[field] = options[select(sorted(options), "Явная привязка: "+field)]
        used.add(binding[field])
    return {"symbol_id": chosen["id"], "prefix": prefix, "inspection": inspection, "binding": binding}


def collect_settings():
    return settings(
        background_diameter_mm=float(ask("Диаметр фона, мм", "10").replace(",", ".")),
        background_step_mm=float(ask("Шаг фона, мм", "300").replace(",", ".")),
        anchorage_diameters=float(ask("Удлинение за границу потребности, в диаметрах; минимум 40d", "40").replace(",", ".")),
        minimum_zone_fe_count=int(select(["2", "3"], "Минимальная ширина зоны, КЭ")),
        algorithm=select(list(ALGORITHMS), "Стратегия; genetic-pareto — исходный выбор"),
        mass_preference=float(ask("Предпочтение массы 0..1 (0.5 — баланс массы и числа стержней, не гарантия «Точки 3»)", "0.5").replace(",", ".")))


def request_settings(packet, digest, direction, doc, view):
    config = collect_settings()
    kind = select(["DXF", "PNG"], "Формат подложки текущего вида")
    confirmed = forms.alert("Подложка {0} уже загружена и приведена к осям штатными средствами Revit?\n"
        "Этот запрос НЕ обрабатывает PNG и НЕ считывает геометрию подложки автоматически.\n"
        "Изменённые параметры НЕ применяются к уже рассчитанным зонам.\n"
        "Сохраню запрос для нового расчёта; RVT не изменится.".format(kind), yes=True, no=True)
    return make_request(digest, direction, config,
        {"kind": kind, "alignment_confirmed": confirmed is True, "status": "user-confirmed-not-readback"},
        {"title": text_type(doc.Title), "project_information_unique_id": text_type(doc.ProjectInformation.UniqueId),
         "view_id": element_id(view.Id), "view_unique_id": text_type(view.UniqueId), "case_id": packet["case_id"]})


def calculate(direction):
    path = forms.pick_file(file_ext="dxf", title="Настоящий DXF выбранного направления; PNG пока не поддержан")
    if not path:
        raise ValueError("DXF не выбран")
    sources = {"dxf": read_source(path, ".dxf")}
    mapping = select(["SHK файл", "k09-above-3-d10-v1", "k09-minus-2-d12-v1", "plate-zero-d12-v1"], "Шкала именно этого DXF")
    if mapping == "SHK файл":
        path = forms.pick_file(file_ext="shk", title="Шкала выбранного DXF")
        if not path:
            raise ValueError("Шкала не выбрана")
        sources["shk"], mapping = read_source(path, ".shk"), "auto"
    config = collect_settings()
    profile = {"background_origin_mm": float(ask("Фаза фоновой сетки, глобальная поперечная координата, мм", "0").replace(",", ".")),
        "first_300_offset_mm": float(ask("Смещение добавки @300 относительно фона, мм", "100").replace(",", ".")),
        "contact_side": select(["left", "right"], "Для @100: сторона касания фона в исходном coplanar профиле; Z НЕ проверен"),
        "steel_class": ask("Класс стали для исходной ведомости", "A500")}
    request_bytes = calculation_request(direction, config, profile, mapping, sources)
    endpoint = server_endpoint(ask("URL твоего доверенного сервера приложения. По умолчанию сервера НЕТ.\n"
        "Удалённый сервер — только HTTPS; локальный может быть http://127.0.0.1:8000"))
    token = None
    if select(["Без токена", "Прочитать Bearer из локального файла"], "Авторизация твоего сервера") != "Без токена":
        token_path = forms.pick_file(file_ext="txt", title="Файл содержит только Bearer token; не попадёт в отчёт")
        if not token_path:
            raise ValueError("Токен не выбран")
        with open(token_path, "rb") as stream:
            token = stream.read(8193).decode("utf-8-sig").strip()
        if not token or len(token) > 8192:
            raise ValueError("Пустой или слишком длинный токен")
    filenames = []
    for source in sources.values():
        filenames.append(source["filename"])
    if forms.alert("Отправить реальные файлы {0}\nна {1}?\n\n"
            "Будут переданы сами DXF/SHK и настройки, НЕ RVT. Сервер выполняет новый расчёт; "
            "ответ сверяется с SHA исходных файлов и точного запроса.\n"
            "Одно направление, одна добавка на уровень; фон должен совпасть со шкалой. "
            "Оси @150 — 100/200 по СТО, @100 — явно выбранное касание; фазы/Z не являются инженерным одобрением.\n"
            "Расчёт может занять до нескольких минут. Транзакция Revit ещё НЕ открыта; "
            "перед размещением будет отдельный просмотр и подтверждение.\n\nСформировать Доп. Поля?".format(
                ", ".join(filenames), endpoint), yes=True, no=True) is not True:
        raise ValueError("Передача исходников отменена")
    result = post_calculation(endpoint, request_bytes, sources, confirmed=True, bearer_token=token)
    source_components(result, direction)
    destination = forms.save_file(file_ext="json", default_name="qmonitoring-workflow-analysis-{0}.json".format(
        datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")), title="Сохранить новый результат расчёта до размещения")
    if not destination:
        raise ValueError("Сохранение результата отменено; размещение не начато")
    write_report_json(destination, result)
    with open(destination, "rb") as stream:
        digest = hashlib.sha256(stream.read()).hexdigest()
    metrics = result["metrics"]
    if forms.alert("Новый расчёт: {0} исходных зон; {1} стержней ДО физической обработки; {2:.2f} кг; {3} позиций.\n"
            "{4}\n\nИсходные зоны сохранены: {5}\n"
            "Перейти к выбору семейств и параметров?".format(metrics["source_zone_count"], metrics["physical_bar_count"],
                metrics["additional_mass_kg"], metrics["position_count"], analysis_caption(result), destination),
            yes=True, no=True) is not True:
        raise ValueError("Результат сохранён; размещение отменено")
    return result, digest


def main():
    doc = revit.doc
    if doc is None:
        forms.alert("Открой проект и целевой план армирования.")
        return
    destination = None
    try:
        view = doc.ActiveView
        preflight(doc, DB, view)
        direction = select(list(DIRECTIONS), "Направление: НИЗ bottom / ВЕРХ top, глобальные X/Y")
        action = select(["Сформировать Доп. Поля — НОВЫЙ расчёт DXF", "Разместить готовые ИСХОДНЫЕ зоны семействами",
                         "Настроить и сохранить запрос НОВОГО расчёта"], "Workflow 8.1")
        if action.startswith("Сформировать"):
            packet, digest = calculate(direction)
        else:
            source = forms.pick_file(file_ext="json", title="Исходные изополя/зоны или qmonitoring-workflow-analysis/v1")
            if not source:
                return
            with open(source, "rb") as stream:
                content = stream.read(MAX_RESPONSE_BYTES+1)
            if len(content) > MAX_RESPONSE_BYTES:
                raise ValueError("JSON превышает 32 MiB")
            try:
                packet = decode_analysis(content)
                digest = hashlib.sha256(content).hexdigest()
            except ValueError:
                packet, digest = load_preview_input(source)
        source_components(packet, direction)
        destination = forms.save_file(file_ext="json", default_name="qmonitoring-workflow-{0}.json".format(
            datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")), title="Новое имя отчёта / запроса")
        if not destination:
            return
        if os.path.exists(destination):
            raise ValueError("Существующие отчёты не перезаписываются")
        if action.startswith("Настроить"):
            request = request_settings(packet, digest, direction, doc, view)
            write_report_json(destination, request)
            forms.alert("Запрос сохранён. Расчёт НЕ запускался, старые зоны НЕ менялись.\n"+destination)
            return
        entered = ask("Смещение исходных координат в ВНУТРЕННИЕ XY Revit, мм, через ;.\n"
            "Только одинаковый масштаб 1:1 и неповёрнутые глобальные оси. Иначе остановись и исправь привязку.", "0; 0")
        offset = []
        for part in entered.split(";"):
            offset.append(float(part.strip().replace(",", ".")))
        if len(offset) != 2:
            raise ValueError("Нужны два числа X; Y")
        rows = source_components(packet, direction, offset)
        selections = {"zone": select_family(doc, view, "zone"), "annotation": select_family(doc, view, "annotation")}
        message = ("{0}\n\nНаправление {1}; {2} исходных компонентов, {3} НОВЫХ экземпляров на ТЕКУЩЕМ плане.\n"
            "Семейство зоны: точка вставки в ЦЕНТРЕ осевого прямоугольника, локальная X вдоль стержней.\n"
            "Осевая ширина — расстояние между крайними осями, НЕ AreaBoundary.\n"
            "Параметры исходного набора НЕ описывают новую обрезанную партию.\n"
            "Подтверждаешь эти соглашения выбранного семейства и смещение {4}?\n"
            "Изменяю только новые экземпляры и при необходимости активирую выбранные типы.\n"
            "Рабочие наборы сохраняются. Revit может автоматически заимствовать связанные с видом элементы; "
            "Checkout/Save/Sync не вызываются. Твоя синхронизация позднее передаст новые элементы в хранилище.\n"
            "Любая ошибка/неверный readback отменяет всю партию. Не заявляется прохождение инженерных гейтов.\n\n"
            "Разместить исходные параметрические зоны?").format(DISCLAIMER, direction, len(rows), len(rows)*2, offset)
        if forms.alert(message, yes=True, no=True) is not True:
            return
        with forms.ProgressBar(title="Исходные зоны {value}/{max_value}", cancellable=True) as bar:
            def progress(value, maximum):
                bar.update_progress(value, maximum)
                return not bar.cancelled
            report = place_source_families(doc, DB, view, packet, direction, selections, offset, True, progress)
        report["packet_sha256"] = digest
        write_report_json(destination, report)
        forms.alert("{0}\n{1}\nОтчёт: {2}\nНЕ Rebar; полный workflow ещё требует проверки в Revit.".format(
            report["status"], DISCLAIMER, destination))
    except Exception as exc:
        print(traceback.format_exc())
        forms.alert("Workflow 8.1: {0}\nЕсли отчёт не сохранился ПОСЛЕ успешного размещения, экземпляры "
            "остаются в документе: не повторяй запуск, скопируй ошибку из вывода.\n"
            "При неподтверждённом откате не сохраняй и не синхронизируй модель.".format(text_type(exc)))


if __name__ == "__main__":
    print("QMonitoring Workflow 8.1 "+VERSION)
    main()
