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
        "id": "plan-preview", "title": "Посмотреть раскладку в Revit", "runtime_version": "0.1.1",
        "button": "PlanPreview", "command": "Plan Preview", "extension": "QMonitoringPreview",
        "mode": "graphic_preview", "description": "Все четыре направления совместимого физического пакета в отдельном чертёжном виде. Линии, не арматура.",
        "report_schema": "revit-graphic-plan-preview-report/v1",
        "input_schemas": ["physical-bar-plan-trial/v1", "physical-bar-relocation-draft/v1"],
        "runtime_module": "qm_revit_plan_preview.py",
        "modules": ("qm_probe_geometry.py", "qm_revit_probe.py", "qm_trial_geometry.py", "qm_trial_input.py",
            "qm_core_trial.py", "qm_plate_packet.py", "qm_physical_packet.py", "qm_revit_trial.py",
            "qm_trial_worksharing.py", "qm_revit_plan_preview.py"),
        "steps": (
            "Открой файловую локальную либо отсоединённую модель с сохранёнными worksets. Центральная/облачная модель этой командой не поддержана.",
            "Выдели одну плиту, нажми Plan Preview и выбери совместимый физический JSON, отдельно полученный для этой плиты.",
            "Укажи новое имя отчёта и осознанно подтверди перенос XY. Ноль не является универсальной привязкой.",
            "Подтверди создание нового чертёжного вида. Он может попасть в центральную модель при твоей дальнейшей синхронизации.",
        ),
        "limits": "Создаёт один новый графический вид, но не Rebar. Проблемные стержни сохраняются на схеме. Общий composite JSON не является входом этой команды.",
        "verification": "Версия 0.1.1 проверена в Revit: новый вид создан, 902 из 902 графических линий совпали при обратном чтении с допуском 0,01 мм. Это графика, не инженерная приёмка.",
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
3. Открой новую вкладку **{tool['extension']} → Diagnostics → {tool['command']}**.
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
создаёт новый графический вид, который НЕ является конструктивной арматурой.
Ни один пакет не разрешает размещение, не генерирует новые загибы/муфты и не
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
    for name in ("script.py", "bundle.yaml"):
        source = f"{SOURCE_EXTENSION}/{SOURCE_TAB}/Diagnostics.panel/{tool['button']}.pushbutton/{name}"
        target = f"{extension}/{tab}/Diagnostics.panel/{tool['button']}.pushbutton/{name}"
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
        "command": tool["command"], "steps": list(tool["steps"]), "limitations": tool["limits"],
        "verification": tool["verification"], "report_schema": tool["report_schema"],
        "archive_format": "zip-stored-fixed-1980/v1",
        "accepted_input_schemas": list(tool["input_schemas"]),
        "compatibility": {"revit_major_versions": ["2024"], "runner": "pyRevit", "python": "IronPython 2.7"},
        "capabilities": {"read_only": tool["mode"] == "read_only",
            "creates_new_graphic_view": tool["mode"] == "graphic_preview",
            "creates_structural_rebar": False, "modifies_existing_elements": False,
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
            "automatic_revit_connection": "not_supported", "new_forms_structural_export": "not_available",
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
