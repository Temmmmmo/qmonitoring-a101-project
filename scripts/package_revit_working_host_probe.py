"""Standalone read-only working Floor + CAD pyRevit probe; explicit file whitelist."""
from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "integrations/pyrevit"
EXTENSION = "QMonitoring.extension"
BUTTON = f"{EXTENSION}/QMonitoring.tab/Diagnostics.panel/WorkingHostProbe.pushbutton"
FILES = (
    f"{EXTENSION}/lib/qm_probe_geometry.py",
    f"{EXTENSION}/lib/qm_revit_probe.py",
    f"{EXTENSION}/lib/qm_revit_cad.py",
    f"{EXTENSION}/lib/qm_cad_diagnostics.py",
    f"{EXTENSION}/lib/qm_working_host_probe.py",
    f"{BUTTON}/script.py",
    f"{BUTTON}/bundle.yaml",
)
README = """# QMonitoring — рабочая плита + DXF, только чтение

1. Распакуй ZIP в новую папку. В pyRevit → Custom Extension Directories укажи
   папку, ВНУТРИ которой лежит `QMonitoring.extension`. Старый путь QMonitoring
   временно убери из списка и нажми Reload.
2. Открой рабочую модель Revit 2024 (лучше копию). Выдели ровно одну нужную плиту.
   Если плита не выделена, кнопка предложит выбрать её кликом.
3. Нажми **QMonitoring → Diagnostics → Working Host Probe**. Выбери связанный
   или импортированный DXF именно этой плиты и новое имя выходного JSON.
4. Подтверди чтение. Дополнительный выбор локального исходного DXF необязателен:
   можно нажать Отмена. Для загруженной локальной связи SHA256 читается автоматически;
   для импорта отдельный DXF помогает последующей проверке, но сам по себе НЕ
   доказывает совпадение с содержимым импорта.
5. Пришли JSON разработчику. Статус `partial` тоже полезен — он сохраняет причины
   неполного чтения, а не подменяет геометрию предположениями.

В отчёте: фактически выбранная плита, её solid/грани/проёмы/защитные слои, выбранный
CAD, преобразования, два чтения мешей и явно распознанные KLEENKA-треугольники.
Координаты Revit записываются в мм. Исходные единицы DXF и привязка проверяются
отдельно. Совпадение двух чтений CAD не означает совпадения с исходным файлом.

Кнопка НЕ создаёт арматуру, не открывает транзакций, не запрашивает высоты/XY,
не меняет виды, не перемещает геометрию и не сохраняет RVT. Она не использует
тестовую плиту или тестовые ID. В архиве нет кнопок создания/Trial/MVP.
Это сбор исходных данных, а не инженерное разрешение на размещение.

Проверено локальными тестами с заменителями Revit API. Первый реальный запуск
нужен для подтверждения работы в Windows Revit / IronPython 2.7.
"""


def build_package(output: Path, *, dxf: Path | None = None, dxf_sha256: str | None = None) -> Path:
    contents = [(name, (SOURCE / name).read_bytes()) for name in FILES]
    readme = README
    if (dxf is None) != (dxf_sha256 is None):
        raise ValueError("Provide both --dxf and --dxf-sha256, or neither")
    if dxf is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", dxf_sha256 or ""):
            raise ValueError("Expected a lowercase SHA256 digest")
        if dxf.suffix.lower() != ".dxf" or not dxf.is_file():
            raise ValueError("Expected an existing source DXF file")
        size = dxf.stat().st_size
        if not 0 < size <= 64 * 1024 * 1024:
            raise ValueError("Source DXF is empty or exceeds the 64 MiB limit")
        with dxf.open("rb") as stream:
            content = stream.read(64 * 1024 * 1024 + 1)
        if len(content) != size or hashlib.sha256(content).hexdigest() != dxf_sha256:
            raise ValueError("Source DXF bytes do not match the supplied SHA256")
        contents.append(("source/top-X.dxf", content))
        readme += ("\n## Контрольный DXF рабочей плиты\n\n"
            "В `source/top-X.dxf` лежит точная копия исходного верхнего X для проверяемого решения. "
            "Используй именно этот файл для проверки, не DXF старой тестовой плиты. "
            "Если нужный DXF уже связан/импортирован, выбери этот CAD в модели. "
            "Если его нет, сначала свяжи контрольный DXF в копии рабочей модели обычной командой Revit "
            "с осознанно выбранными единицами/размещением, затем запусти чтение. "
            "Кнопка сама ничего не связывает и не определяет правильную привязку. "
            "Не переименовывай и не подменяй существующие связи ради имени в архиве.\n\n"
            f"SHA256 точных bytes контрольного DXF: `{dxf_sha256}`. "
            "В необязательном окне выбора исходного DXF можно указать этот файл.\n")
    contents.append(("README.md", readme.encode("utf-8")))
    if len({name for name, _ in contents}) != len(contents):
        raise ValueError("Duplicate archive entry")
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "x", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in contents:
            archive.writestr(name, content)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT /
        "artifacts/revit_working_host_2026_09_14/qmonitoring-working-host-probe-0.1.0.zip")
    parser.add_argument("--dxf", type=Path, help="Optional exact source DXF, included only with a verified hash")
    parser.add_argument("--dxf-sha256", help="Expected exact source DXF SHA256")
    args = parser.parse_args()
    print(build_package(args.output, dxf=args.dxf, dxf_sha256=args.dxf_sha256))


if __name__ == "__main__":
    main()
