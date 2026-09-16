"""The actual four K09 DXFs, kept outside Git and never synthesized as a fallback.

The registry contains provenance, not the private files. REBAR_ENGINEERING_INPUTS_DIR
is an operator-controlled directory containing the case subdirectory below.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from rebar.models import Axis, Direction, Layer
from .analyze_composite_plate import CompositeDirectionSettings
from .analyze_plate import PlateDirectionSource
from .assistant_inputs import analyze_assistant_sources
from .boundary_trim_web import boundary_trim_web_report
from .physical_layout_recovery import recover_physical_layout
from .physical_web_report import physical_web_report
from rebar.optimization.contracts.physical import PhysicalNormalizationConfig

EXAMPLE_ID = "k09-typical-3-14"
MAPPING_ID = "k09-above-3-d10-v1"
ENV_NAME = "REBAR_ENGINEERING_INPUTS_DIR"
MAX_SOURCE_BYTES = 30 * 1024 * 1024
# Canonical names and SHA256 from the original A101 assignment, not re-exported DXFs.
SOURCES = (
    ("bottom", "X", "Нижняя по Х.dxf", "c18e29eaa8b42c9c3a90d45b049cbf103aa360abd35bd549cdb8f6f842939665"),
    ("bottom", "Y", "Нижняя по У.dxf", "b016dc78cef3a52fcbbc9c20a6426fd0b4bdcb7847931a62ea0218dbcbbf7e7a"),
    ("top", "X", "Верхняя по Х.dxf", "aef8de5931f32b8d2f384b4f193a3fddfc5e527943ad4733bf506892a145678f"),
    ("top", "Y", "Верхняя по У.dxf", "b10f4cfb60a53a11d7b93cb51b19e17206a83f3af29a5ea3d008bc60d3973311"),
)
PROFILE_SOURCE = "Research profile from 2026-09-14: not approved working-RVT phase or layer order"


class EngineeringFilesUnavailableError(ValueError):
    """Missing or different original bytes; do not substitute a demo or old result."""


def _checked_bytes(path: Path, expected_sha256: str) -> bytes:
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
    except OSError as error:
        raise EngineeringFilesUnavailableError("Исходный DXF-комплект не установлен на сервере.") from error
    if not data or len(data) > MAX_SOURCE_BYTES or hashlib.sha256(data).hexdigest() != expected_sha256:
        raise EngineeringFilesUnavailableError("Контрольная сумма исходного DXF не совпадает. Расчёт не запущен.")
    return data


def _source_bytes() -> tuple[bytes, ...]:
    configured = os.environ.get(ENV_NAME)
    if not configured:
        raise EngineeringFilesUnavailableError("Исходный DXF-комплект ещё не подключён к приложению.")
    root = Path(configured) / EXAMPLE_ID
    return tuple(_checked_bytes(root / name, digest) for _, _, name, digest in SOURCES)


def example_metadata(*, available: bool, status: str) -> dict:
    return {
        "id": EXAMPLE_ID, "title": "К09 · плита над 3–14 этажами",
        "description": "Реальный комплект А101: четыре исходных DXF плиты над 3 этажом, задание от 13.08.2025.",
        "source_kind": "real_engineering_files", "is_available": available, "status": status,
        "reference": {
            "mass_kg": 2878.76, "physical_bar_count": 1227, "position_count": 74,
            "scope": "Дополнительная арматура всей плиты: прямые и гнутые стержни инженерной спецификации",
            "note": "Эталон включает гнутые стержни. Этот расчёт строит прямые зоны; сравнение не означает "
                    "эквивалентность узлов и прохождение проверок размещения в Revit.",
        },
        "sources": [{"direction": {"layer": layer, "axis": axis}, "dxf_filename": name,
                     "sha256": digest, "shk_filename": None, "mapping_id": MAPPING_ID,
                     "mapping_label": "Проверенная шкала К09 · фон Ø10@300"}
                    for layer, axis, name, digest in SOURCES],
        "profile": {
            "background_origin_mm": 0, "first_300_offset_mm": 100, "second_offset_mm": 0,
            "contact_side": "left", "steel_class": "A500", "cutting_profile": "plate-11700-batch",
            "engineering_approval": False,
            "note": "Задан исследовательский профиль: начало фона 0 мм, первая добавка @300 со сдвигом 100 мм, "
                    "вторая — 0 мм. Для @150 используются оси 100/200 мм по таблице, не равномерный шаг 150. "
                    "Фазы и порядок слоёв в рабочем RVT не подтверждены. Границы, проёмы и 3D-коллизии "
                    "проверяются отдельно по снимку Revit; без него размещение недоступно. "
                    "Разрешена исследовательская замена одиночной добавки на более сильную и укрупнение "
                    "совпадающих стержней, только с независимой проверкой покрытия и анкеровки нового диаметра.",
        },
    }


def engineering_example_catalog() -> dict:
    from . import s1_example
    try:
        _source_bytes()
    except EngineeringFilesUnavailableError as error:
        entry = example_metadata(available=False, status=str(error))
    else:
        entry = example_metadata(available=True, status="ready")
    try:
        s1_example.source_bytes()
    except EngineeringFilesUnavailableError as error:
        s1 = s1_example.metadata(available=False, status=str(error))
    else:
        s1 = s1_example.metadata(available=True, status="ready")
    return {"examples": [entry, s1], "default_example_id": s1_example.EXAMPLE_ID}


def analyze_engineering_example(example_id: str, *, working_host_bytes: bytes | None = None,
                                confirm_identity_xy: bool = False, outer_only_repair: bool = False) -> dict:
    from . import s1_example
    if type(outer_only_repair) is not bool or (outer_only_repair and (
            example_id != EXAMPLE_ID or working_host_bytes is None or confirm_identity_xy is not True)):
        raise ValueError('Outer-only repair requires explicit K09 host and identity XY')
    if example_id == s1_example.EXAMPLE_ID:
        if working_host_bytes is not None or confirm_identity_xy:
            raise ValueError("С1 MVP использует плоский контур DXF, не снимок другой Revit-плиты")
        return s1_example.analyze_s1_example()
    if example_id != EXAMPLE_ID:
        raise KeyError(example_id)
    if (working_host_bytes is not None) != (confirm_identity_xy is True):
        raise ValueError("Для обрезки нужны одновременно снимок Working Host и подтверждение совпадения XY")
    originals = _source_bytes()
    # Each run reads a private immutable snapshot of the verified original bytes.
    # No persistent results or lucky cached calculation are substituted for a rerun.
    with TemporaryDirectory(prefix="rebar-k09-") as temporary:
        sources, settings = [], []
        for (layer, axis, name, _), content in zip(SOURCES, originals):
            path = Path(temporary) / name
            path.write_bytes(content)
            sources.append(PlateDirectionSource(path, mapping_id=MAPPING_ID))
            settings.append(CompositeDirectionSettings(Direction(Layer(layer), Axis(axis)), 0, 100, 0,
                                                        "A500", PROFILE_SOURCE, "left"))
        source = analyze_assistant_sources(tuple(sources), case_id=EXAMPLE_ID, maximum_source_bars=1227)
        # Use the existing physical pipeline, not the much heavier recipe-pool baseline.
        # Only public provenance leaves the private temporary source directory.
        provenance = {"mode": "fresh-four-original-dxf", "case_id": EXAMPLE_ID,
            "sources": example_metadata(available=True, status="ready")["sources"],
            "algorithm": source.provenance["algorithm"], "config": source.provenance["config"],
            "candidate_id": source.provenance["candidate_id"], "selection": source.provenance["selection"]}
        recovery = recover_physical_layout(source.problem, source.solution, tuple(settings),
            normalization_config=PhysicalNormalizationConfig(allow_diameter_increase=True),
            source_provenance=provenance)
        if outer_only_repair:
            from .k09_outer_repair_web import k09_outer_repaired_web_report
            report = k09_outer_repaired_web_report(source.problem, recovery, working_host_bytes,
                                                   confirm_identity_xy=confirm_identity_xy)
        else:
            report = (physical_web_report(source.problem, recovery) if working_host_bytes is None else
            boundary_trim_web_report(source.problem, recovery, working_host_bytes,
                                     confirm_identity_xy=confirm_identity_xy, cleanup_redundant=True))
    report["engineering_example"] = example_metadata(available=True, status="ready")
    return report


def install_original_archive(archive: Path, destination: Path, *, example_id: str = EXAMPLE_ID) -> None:
    """Operator-only deployment: copy exactly the original verified DXFs from ZIP.

    Not a seed or a data generator. Reject arbitrary members and altered sources before
    writing anything. Refuse to replace an existing different file in the data volume.
    """
    from . import s1_example
    if example_id not in (EXAMPLE_ID, s1_example.EXAMPLE_ID):
        raise ValueError("Unknown original DXF package")
    sources = SOURCES if example_id == EXAMPLE_ID else s1_example.SOURCES
    members = {name: digest for _, _, name, digest in sources}
    with ZipFile(archive) as bundle:
        infos = bundle.infolist()
        if len(infos) != len(members) or {info.filename for info in infos} != set(members):
            raise ValueError("Archive must contain exactly the four original DXFs for the selected case")
        contents = {}
        for info in infos:
            if info.file_size > MAX_SOURCE_BYTES:
                raise ValueError("Oversized source")
            data = bundle.read(info)
            if hashlib.sha256(data).hexdigest() != members[info.filename]:
                raise ValueError("Original DXF checksum mismatch")
            contents[info.filename] = data
    destination.mkdir(parents=True, exist_ok=True)
    base = destination.resolve() / example_id
    if base.is_symlink():
        raise ValueError("Symlink destination is not supported")
    for name, digest in members.items():
        target = base / name
        if target.is_symlink() or target.parent.is_symlink():
            raise ValueError("Symlink destination is not supported")
        if target.exists():
            _checked_bytes(target, digest)
    for name, data in contents.items():
        target = base / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            with target.open("xb") as stream:
                stream.write(data)
        target.chmod(0o444)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Install the four original K09 DXFs, byte-for-byte")
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--example-id", default=EXAMPLE_ID, choices=(EXAMPLE_ID, "legacy-s1-t800"))
    arguments = parser.parse_args()
    install_original_archive(arguments.archive, arguments.destination, example_id=arguments.example_id)
    print("Verified and installed four original DXF files; no synthetic data generated.")
