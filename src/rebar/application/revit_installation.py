"""Versioned, data-free pyRevit downloads. Build path uses the standard library only.

Distribution builds generate deterministic ZIP resources; production never needs
a checkout, a writable filesystem or access to private Revit/DXF input files.
"""
from __future__ import annotations

import hashlib
from importlib import resources
from io import BytesIO
import json
from pathlib import Path
import re
from zipfile import ZIP_STORED, ZipFile, ZipInfo

CATALOG_SCHEMA = "qmonitoring-revit-tools/v1"
PACKAGE_SCHEMA = "qmonitoring-revit-installation-package/v1"
MAX_BUNDLE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 512 * 1024
SOURCE_EXTENSION = "QMonitoring.extension"
SOURCE_TAB = "QMonitoring.tab"

TOOLS = (
    {
        "id": "rebar-review-mvp", "title": "Раскладка арматуры в Revit", "runtime_version": "0.1.6",
        "button": "RebarReview", "command": "Раскладка арматуры", "extension": "QMonitoringRebarReview",
        "button_assets": ("icon.svg", "icon.png", "icon.dark.png"),
        "panel": "Review", "mode": "native_rebar_review",
        "description": "Вся прямая партия Rebar в копии RVT: строгая сверка или явно выбранная презентация с измеренными замечаниями и новым 3D-видом. Оставление только по отдельному подтверждению; не инженерная выдача.",
        "report_schema": "revit-rebar-review-mvp-report/v1",
        "input_schemas": ["graphic-bar-plan-draft/v1", "graphic-bar-plan-pruned/v1", "graphic-bar-plan-repaired/v1"],
        "runtime_module": "qm_rebar_review.py",
        "modules": ("qm_probe_geometry.py", "qm_revit_probe.py", "qm_trial_geometry.py", "qm_trial_input.py",
            "qm_core_trial.py", "qm_plate_packet.py", "qm_physical_packet.py", "qm_revit_trial.py",
            "qm_trial_worksharing.py", "qm_revit_plan_preview.py", "qm_revit_source_preview.py", "qm_revit_pruned_preview.py",
            "qm_revit_repaired_preview.py", "qm_rebar_review.py", "qm_revit_rebar_review.py", "qm_revit_presentation_view.py"),
        "steps": (
            "Открой локальную или отсоединённую КОПИЮ Revit 2024 и выдели одну native Floor. Central/cloud и семейство не поддержаны.",
            "Выбери полный graphic-bar-plan-draft/pruned/repaired JSON и явный XY-перенос. Подтверди предложенные глубины осей: native-грани, Dmax выбранных типов, явный MVP face40/gap4; ручной ввод только через «Другие настройки». Масштаб и поворот не угадываются.",
            "Для каждого D/steel выбери точный загруженный RebarBarType и подтверди копию. Создаётся вся партия отдельных прямых Rebar без загибов, муфт и пропуска проблемных стержней.",
            "Выбери «Для презентации — показать с замечаниями» либо «Строгая сверка осей». Презентация сохраняет измеренные сдвиги только после отдельного «Оставить»; фактическая масса и замечания остаются в обязательном JSON. Новый 3D-вид открывается после оставления.",
            "После Commit команда перечитает каждый ID, host, тип, конечную ось XYZ, длину, диаметр, количество и расчётную массу. Любое отличие откатывает всю партию.",
            "Только после успешного readback отдельно выбери: оставить диагностические Rebar в копии или откатить всё. Команда не вызывает Save/Sync; сохрани JSON-отчёт.",
        ),
        "limits": "Review-only MVP, не выпуск: исходные coverage/40d/stock fail/not_checked остаются видимыми. Проверяется тело каждого стержня по native top И bottom наружным контурам и толщине без bbox fallback; flat200 top-subset-bottom профиль поддерживает Пм-1 К09. Отверстия, cover, перепады, фон и его коллизии исключены; intermediate Solid не сертифицируется. Исходный conditional 3D Z-profile не проверяет четыре выбранные UI-глубины. Наклонный/составной/криволинейный host блокируется. До 5000 отдельных стержней, поэтому операция может быть длительной. Существующая арматура не меняется.",
        "verification": "Полнота inventory, CreateFromCurves-контракт, строгий readback 0,01 мм, tamper, rollback и explicit keep проверены offline doubles. Первый запуск всей партии в настоящем Revit ещё не выполнен.",
    },
    {
        "id": "working-host-probe", "title": "Геометрия плиты и DXF", "runtime_version": "0.1.0",
        "button": "WorkingHostProbe", "command": "Working Host Probe", "extension": "QMonitoringHostTools",
        "mode": "read_only", "description": "Снимок выбранной рабочей плиты, проёмов, защитных слоёв и выбранного CAD.",
        "report_schema": "revit-working-host-cad-probe/v1", "input_schemas": [],
        "runtime_module": "qm_working_host_probe.py",
        "modules": ("qm_probe_geometry.py", "qm_revit_probe.py", "qm_cad_diagnostics.py",
            "qm_revit_cad.py", "qm_working_host_probe.py"),
        "steps": (
            "Открой рабочую модель и выдели нужную native-плиту Floor (не плиту внутри связи).",
            "Нажми Working Host Probe и выбери импортированный или связанный DXF именно этой плиты.",
            "Укажи новое имя JSON и подтверди чтение. Дополнительный выбор исходного DXF можно отменить.",
            "Сохрани JSON для загрузки в приложение или передачи разработчику. partial тоже содержит полезные факты.",
        ),
        "limits": "Не проверяет совпадение исходного DXF с моделью автоматически и не размещает арматуру.",
        "verification": "Команда уже запускалась в Revit 2024; текущая сборка дополнительно проверяется offline-тестами.",
    },
    {
        "id": "working-rebar-probe", "title": "Существующая арматура", "runtime_version": "0.1.0",
        "button": "WorkingRebarProbe", "command": "Working Rebar Probe", "extension": "QMonitoringReadOnly",
        "mode": "read_only", "description": "Фактические положения Rebar/RebarInSystem выбранной плиты, прямые и дуги, типы и количество.",
        "report_schema": "revit-working-rebar-probe/v1", "input_schemas": [],
        "runtime_module": "qm_working_rebar_probe.py",
        "modules": ("qm_probe_geometry.py", "qm_revit_probe.py", "qm_working_rebar_probe.py"),
        "steps": (
            "Открой рабочую модель с обычными рабочими наборами и выбери ту же native-плиту Floor.",
            "Нажми Working Rebar Probe. Пакет раскладки и DXF для этой команды не нужны.",
            "Выбери новое имя JSON, подтверди только чтение и дождись отчёта.",
            "Сохрани JSON, включая partial: закрытые worksets и недоступные объекты не исправляются автоматически.",
        ),
        "limits": "Роли фон/добавочная/краевая не назначаются. Арматура других hosts, связей и Fabric не образует полный collision-инвентарь.",
        "verification": "Новая команда проверена offline-тестами; её первый реальный запуск Revit ещё требуется.",
    },
    {
        "id": "plan-preview", "title": "Изополя, зоны и раскладка в Revit", "runtime_version": "0.2.4",
        "button": "PlanPreview", "command": "Plan Preview", "extension": "QMonitoringPreview",
        "mode": "graphic_preview", "description": "Исходные изополя и прямоугольные зоны в четырёх новых видах; физические стержни — отдельный режим. Графика, не арматура.",
        "report_schema": "revit-graphic-plan-preview-report/v1",
        "input_schemas": ["source-isofields-zones/v1", "graphic-bar-plan-draft/v1", "graphic-bar-plan-pruned/v1", "graphic-bar-plan-repaired/v1", "physical-bar-plan-trial/v1", "physical-bar-relocation-draft/v1"],
        "runtime_module": "qm_revit_plan_preview.py",
        "modules": ("qm_probe_geometry.py", "qm_revit_probe.py", "qm_trial_geometry.py", "qm_trial_input.py",
            "qm_core_trial.py", "qm_plate_packet.py", "qm_physical_packet.py", "qm_revit_trial.py",
            "qm_trial_worksharing.py", "qm_revit_plan_preview.py", "qm_revit_source_preview.py", "qm_revit_pruned_preview.py", "qm_revit_repaired_preview.py"),
        "steps": (
            "Открой файловую локальную либо отсоединённую модель с сохранёнными worksets. Центральная/облачная модель этой командой не поддержана.",
            "В расчёте плиты нажми «Скачать изополя + исходные зоны». В Revit выдели одну плиту, нажми Plan Preview и выбери source-isofields-zones.json. Совместимый физический JSON открывается отдельно.",
            "Укажи новое имя отчёта и осознанно подтверди перенос XY. Ноль не является универсальной привязкой.",
            "Подтверди создание четырёх новых исходных видов (либо одного физического). Они могут попасть в центральную модель при твоей дальнейшей синхронизации. При ошибке чтения результата вся группа новых видов откатывается.",
        ),
        "limits": "Исходный режим сохраняет каждый КЭ, пятно demand_bbox, границы наборов после40d/раскроя и параметры. Это не физическая ведомость и не AreaReinforcement. Обрезанная и удалённая избыточная партии передаются отдельными graphic-bar-plan-draft/pruned с явными статусами покрытия/40d/раскроя. Flat MVP привязан к внешнему DXF без отверстий, перепадов и cover, не к измеренной Revit-геометрии. Revit ничего не обрезает сам. Общий composite JSON не является входом.",
        "verification": "Прежний физический режим 0.1.1 проверен в Revit: 902/902 линии, допуск 0,01 мм. Исходный и pruned-режимы 0.2.3 проверены offline, в том числе реальный JSON 975 стержней; их первый запуск в настоящем Revit ещё требуется. Покрытие/40d/раскрой с fail остаются fail. Это графика, не инженерная приёмка.",
    },
    {
        "id": "source-workflow-81", "title": "Доп. поля · выбранные семейства на плане", "runtime_version": "0.1.3",
        "button": "SourceWorkflow", "command": "Source Workflow", "extension": "QMonitoringWorkflow",
        "panel": "Workflow", "mode": "view_family", "description": "Один DXF → подтверждённая HTTPS-передача → новый расчёт → выбранные загруженные семейства элементов узлов и аннотаций на текущем виде. Не Rebar.",
        "report_schema": "qmonitoring-workflow-8-1-report/v1",
        "input_schemas": ["source-isofields-zones/v1", "qmonitoring-workflow-analysis/v1"],
        "runtime_module": "qm_workflow_81.py",
        "modules": ("qm_probe_geometry.py", "qm_revit_probe.py", "qm_trial_geometry.py", "qm_trial_input.py",
            "qm_core_trial.py", "qm_plate_packet.py", "qm_physical_packet.py", "qm_revit_trial.py",
            "qm_trial_worksharing.py", "qm_revit_plan_preview.py", "qm_revit_source_preview.py", "qm_revit_pruned_preview.py",
            "qm_revit_repaired_preview.py", "qm_workflow_81.py", "qm_workflow_81_native.py", "qm_workflow_81_transport.py"),
        "steps": (
            "Открой локальный горизонтальный план и штатно подгрузи DXF; проверь единицы, оси и масштаб 1:1.",
            "Выбери направление и настройки расчёта. Выбери исходный DXF и совместимый SHK либо явный mapping.",
            "Укажи доверенный HTTPS-сервер (HTTP разрешён только для localhost), прочитай список передаваемых файлов и отдельно согласись на upload. Без согласия запрос не отправляется.",
            "Проверь возвращённый расчёт и гейты, затем выбери уже загруженные семейства элементов узлов и аннотаций и их реальные параметры экземпляров.",
            "Подтверди вставку на текущий вид, проверь внешний вид и пришли JSON-отчёт. Ошибка readback откатывает группу новых экземпляров.",
        ),
        "limits": "Один DXF/направление; PNG не оцифровывается. Размещаются только view-family экземпляры исходных зон, не конструктивная арматура и не обрезанная физическая партия. Фон/настройки не отменяют существующие fail-гейты. Семейство, 3D и полный ТЗ-workflow требуют проверки в Revit.",
        "verification": "HTTP/request binding, параметры, readback и rollback проверены offline-тестами. Первый запуск всей цепочки в настоящем Revit ещё не выполнен.",
    },
)


class RevitBundleUnavailableError(RuntimeError):
    """Distribution lacks intact public resources; never fall back to private data."""


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _readme(tool: dict) -> bytes:
    text = f"""# QMonitoring — {tool['title']}

Инструмент: `{tool['command']}`; runtime {tool['runtime_version']}; Revit 2024 / pyRevit / IronPython 2.7.

## Установка — старые расширения не трогать

1. pyRevit должен быть установлен в Revit заранее. Этот ZIP — расширение, не установщик pyRevit.
2. Распакуй ZIP в новую папку. В pyRevit → Settings → Custom Extension Directories добавь
   папку, ВНУТРИ которой лежит `{tool['extension']}.extension`, и нажми Reload.
   Выбирается путь к папке, не script.py, не ZIP и не сама папка .extension.
3. Открой новую вкладку **{tool['extension']} → {tool.get('panel', 'Diagnostics')} → {tool['command']}**.
   Старые QMonitoring-вкладки останутся на месте; отключать их не требуется.
4. Для обновления ЭТОГО инструмента убери из списка только предыдущий путь с той же
   `{tool['extension']}.extension`, добавь новый и нажми Reload. Две версии одной
   extension одновременно не подключай. Другие расширения и рабочие наборы не меняй.

## Работа

"""
    for index, step in enumerate(tool["steps"], 1):
        text += f"{index}. {step}\n"
    text += f"""
## Что важно

{tool['limits']}

Все инструменты не сохраняют и не синхронизируют RVT автоматически. Читающие кнопки
не меняют модель и не открывают worksets. Plan Preview только после подтверждения
создаёт новый графический вид; Source Workflow после отдельного согласия на upload,
выбора семейства и подтверждения создаёт экземпляры на текущем виде. Ни то, ни другое
НЕ является конструктивной арматурой. Rebar Review отдельно создаёт диагностические
native Rebar в подтверждённой копии и оставляет их только после post-Commit readback
и второго подтверждения; это всё равно не выпуск и не инженерное разрешение.
Ни один пакет не генерирует новые загибы/муфты и не
объявляет пройденными анкеровку, покрытие, коллизии или инженерные гейты.

{tool['verification']}

В ZIP нет исходных RVT/DXF/PDF, частных отчётов, демонстрационной плиты или раскладки.
`manifest.json` содержит версии, точный whitelist и SHA256 файлов. Путь установки
может содержать кириллицу. JSON сохраняется в UTF-8, существующий файл не перезаписывается.
Если кнопка не появилась, проверь выбранный родительский путь и Reload; если упала,
пришли полный traceback. Передавай JSON только участникам проекта: отчёт может
содержать имена объектов, пути модели и её геометрию.
"""
    return text.encode("utf-8")


def _entries(tool: dict, source_root: Path) -> list[tuple[str, bytes]]:
    root = source_root.resolve(strict=True)
    extension = f"{tool['extension']}.extension"
    tab = f"{tool['extension']}.tab"
    requested = []
    for module in tool["modules"]:
        requested.append((f"{SOURCE_EXTENSION}/lib/{module}", f"{extension}/lib/{module}"))
    for name in ("script.py", "bundle.yaml", *tool.get("button_assets", ())):
        panel = tool.get("panel", "Diagnostics")
        source = f"{SOURCE_EXTENSION}/{SOURCE_TAB}/{panel}.panel/{tool['button']}.pushbutton/{name}"
        target = f"{extension}/{tab}/{panel}.panel/{tool['button']}.pushbutton/{name}"
        requested.append((source, target))
    entries = []
    for source, target in requested:
        path = (root / source).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Tool source is outside the explicit public whitelist")
        with path.open("rb") as stream:
            content = stream.read(MAX_SOURCE_BYTES + 1)
        if not content or len(content) > MAX_SOURCE_BYTES:
            raise ValueError("Tool source is empty or exceeds its bounded size")
        entries.append((target, content))
    entries.append(("README.md", _readme(tool)))
    return sorted(entries)


def _metadata(tool: dict) -> dict:
    return {"id": tool["id"], "title": tool["title"], "description": tool["description"],
        "runtime_version": tool["runtime_version"], "mode": tool["mode"],
        "extension_directory": tool["extension"] + ".extension", "tab": tool["extension"],
        "command": tool["command"], "panel": tool.get("panel", "Diagnostics"),
        "steps": list(tool["steps"]), "limitations": tool["limits"],
        "verification": tool["verification"], "report_schema": tool["report_schema"],
        "archive_format": "zip-stored-fixed-1980/v1",
        "accepted_input_schemas": list(tool["input_schemas"]),
        "compatibility": {"revit_major_versions": ["2024"], "runner": "pyRevit", "python": "IronPython 2.7"},
        "capabilities": {"read_only": tool["mode"] == "read_only",
            "creates_new_graphic_view": tool["mode"] == "graphic_preview",
            "creates_view_family_instances": tool["mode"] == "view_family",
            "creates_structural_rebar": tool["mode"] == "native_rebar_review",
            "retains_created_elements_only_after_explicit_confirmation": tool["mode"] == "native_rebar_review",
            "modifies_existing_elements": False,
            "saves_or_syncs_model": False, "includes_project_data": False,
            "placement_eligible": False, "engineering_approval": False}}


def build_tool_bundle(tool: dict, source_root: Path) -> tuple[dict, bytes]:
    """Pure whitelist build; returns content-addressed metadata and deterministic ZIP."""
    entries = _entries(tool, source_root)
    files = [{"path": name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
        for name, content in entries]
    metadata = _metadata(tool)
    source_digest = hashlib.sha256(_json_bytes({"tool": metadata, "files": files})).hexdigest()
    version = tool["runtime_version"] + "-" + source_digest[:16]
    manifest = {"schema_version": PACKAGE_SCHEMA, **metadata, "bundle_version": version,
        "source_sha256": source_digest, "files": files}
    entries.append(("manifest.json", _json_bytes(manifest)))
    output = BytesIO()
    # STORE avoids zlib-version-dependent bytes for the same content-addressed URL.
    with ZipFile(output, "w", compression=ZIP_STORED) as archive:
        for name, content in sorted(entries):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content, compress_type=ZIP_STORED)
    content = output.getvalue()
    if len(content) > MAX_BUNDLE_BYTES:
        raise ValueError("Public tool bundle exceeds size budget")
    metadata.update({"version": version, "source_sha256": source_digest,
        "filename": f"qmonitoring-{tool['id']}-{version}.zip", "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "download_url": f"/api/revit/tools/{tool['id']}/{version}/download"})
    return metadata, content


def build_tool_catalog(source_root: Path) -> tuple[dict, dict[str, bytes]]:
    rows, contents = [], {}
    for tool in TOOLS:
        metadata, content = build_tool_bundle(tool, source_root)
        rows.append(metadata)
        contents[metadata["filename"]] = content
    return {"schema_version": CATALOG_SCHEMA, "tools": rows,
        "workflow": {"tool_download": "available", "snapshot_upload_inspection": "available",
            "automatic_revit_connection": "not_supported", "native_straight_rebar_review": "available_copy_only",
            "new_forms_structural_export": "review_only_not_engineering_release",
            "graphic_preview_requires_separate_compatible_packet": True},
        "placement_eligible": False, "engineering_approval": False}, contents


def build_distributable_bundles(source_root: Path, output_root: Path) -> list[str]:
    """Called only by the distribution build, never by an HTTP request."""
    catalog, contents = build_tool_catalog(source_root)
    output_root.mkdir(parents=True, exist_ok=True)
    files = []
    for name, content in {**contents, "catalog.json": _json_bytes(catalog)}.items():
        path = output_root / name
        path.write_bytes(content)
        files.append(str(path))
    return files


def _development_checkout() -> Path | None:
    """Recognize this exact src checkout; not an arbitrary environment/user path."""
    location = Path(__file__).resolve()
    root = location.parents[3]
    expected = root / "src/rebar/application/revit_installation.py"
    if (location == expected and (root / ".git").exists() and (root / "AGENTS.md").is_file()
            and (root / "pyproject.toml").is_file()):
        return root / "integrations/pyrevit"
    return None


def _load_catalog_and_files() -> tuple[dict, object]:
    root = resources.files("rebar.web").joinpath("revit_bundles")
    try:
        catalog_path = root.joinpath("catalog.json")
        if catalog_path.is_file():
            content = catalog_path.read_bytes()
            if len(content) > 128 * 1024:
                raise ValueError("Oversized packaged catalog")
            return json.loads(content), root
        checkout = _development_checkout()
        if checkout is not None:
            return build_tool_catalog(checkout)
    except (OSError, ValueError, KeyError) as exc:
        raise RevitBundleUnavailableError("Пакеты Revit повреждены или не включены в сборку приложения.") from exc
    raise RevitBundleUnavailableError("Пакеты Revit отсутствуют в установленной сборке приложения.")


def _checked_catalog(catalog: dict) -> dict:
    if (not isinstance(catalog, dict) or catalog.get("schema_version") != CATALOG_SCHEMA
            or catalog.get("placement_eligible") is not False or catalog.get("engineering_approval") is not False):
        raise RevitBundleUnavailableError("Неверный manifest установочных пакетов Revit.")
    rows = catalog.get("tools")
    if not isinstance(rows, list) or len(rows) != len(TOOLS):
        raise RevitBundleUnavailableError("Неполный whitelist установочных пакетов Revit.")
    expected = {tool["id"] for tool in TOOLS}
    if any(not isinstance(row, dict) for row in rows) or {row.get("id") for row in rows} != expected:
        raise RevitBundleUnavailableError("Неизвестный установочный пакет Revit.")
    for row in rows:
        tool = next(tool for tool in TOOLS if tool["id"] == row["id"])
        filename, version = row.get("filename"), row.get("version")
        if (not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{16}", version)
                or filename != f"qmonitoring-{row['id']}-{version}.zip"
                or row.get("download_url") != f"/api/revit/tools/{row['id']}/{version}/download"
                or type(row.get("bytes")) is not int or not 0 < row["bytes"] <= MAX_BUNDLE_BYTES):
            raise RevitBundleUnavailableError("Небезопасная запись установочного пакета Revit.")
        for key in ("sha256", "source_sha256"):
            if not isinstance(row.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", row[key]):
                raise RevitBundleUnavailableError("Неверная контрольная сумма в manifest Revit.")
        if version != tool["runtime_version"] + "-" + row["source_sha256"][:16]:
            raise RevitBundleUnavailableError("Версия Revit-пакета не связана с исходным содержимым.")
        metadata = _metadata(tool)
        if _json_bytes({key: row.get(key) for key in metadata}) != _json_bytes(metadata):
            raise RevitBundleUnavailableError("Возможности Revit-пакета не соответствуют приложению.")
    return catalog


def revit_tool_catalog() -> dict:
    catalog, root = _load_catalog_and_files()
    catalog = _checked_catalog(catalog)
    for entry in catalog["tools"]:
        _checked_bundle_content(root, entry)
    return catalog


def revit_tool_download(tool_id: str, version: str) -> tuple[dict, bytes]:
    """Only a currently packaged exact id/version; no path parameter joins."""
    if tool_id not in {tool["id"] for tool in TOOLS}:
        raise KeyError("Unknown Revit tool")
    catalog, root = _load_catalog_and_files()
    rows = _checked_catalog(catalog)["tools"]
    entry = next(row for row in rows if row["id"] == tool_id)
    if version != entry["version"]:
        raise KeyError("Unknown Revit tool version")
    return entry, _checked_bundle_content(root, entry)


def _checked_bundle_content(root: object, entry: dict) -> bytes:
    try:
        content = root[entry["filename"]] if isinstance(root, dict) else root.joinpath(entry["filename"]).read_bytes()
    except (OSError, KeyError) as exc:
        raise RevitBundleUnavailableError("Файл установочного пакета отсутствует в сборке.") from exc
    if (not content or len(content) > MAX_BUNDLE_BYTES or len(content) != entry["bytes"]
            or hashlib.sha256(content).hexdigest() != entry["sha256"]):
        raise RevitBundleUnavailableError("Контрольная сумма установочного пакета не совпадает.")
    return content
