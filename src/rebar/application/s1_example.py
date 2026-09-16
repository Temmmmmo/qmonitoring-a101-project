"""Original S1 DXF delivery and the explicitly simplified planar MVP scenario."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from rebar.models import Axis, Direction, Layer
from rebar.optimization.mappings.legacy_s1 import LEGACY_S1_D18

from .analyze_composite_plate import CompositeDirectionSettings
from .analyze_plate import PlateDirectionSource
from .assistant_inputs import analyze_assistant_sources
from .flat_mvp import PROFILE_ID, flat_mvp_source_web_report
from .patterned_layout_recovery import recover_patterned_layout

EXAMPLE_ID = "legacy-s1-t800"
SOURCES = (
    ("bottom", "X", "С1_t_800_Нижняя по оси Х.dxf", "999d3b809e59d14a8dcd806adffc512c83e5898d03c9f96b0067b103afe7f48d"),
    ("bottom", "Y", "С1_t_800_Нижняя по оси У.dxf", "0064bc781126de9a96e5d6e49ae420af3e2682ae37878b1cef619f841cf43455"),
    ("top", "X", "С1_t_800_Верхняя по оси Х.dxf", "934a4fdec9986513f67f75e93d7e1bd2060014868328a0dad7efb71fe770d769"),
    ("top", "Y", "С1_t_800_Верхняя по оси У.dxf", "6c9a71d23baf1a9c81c7eb7ce34ab81325fae8d041e9038605d8f51bfb8894ee"),
)


def source_bytes():
    from .engineering_example import ENV_NAME, EngineeringFilesUnavailableError, _checked_bytes
    root = os.environ.get(ENV_NAME)
    if not root:
        raise EngineeringFilesUnavailableError("Четыре исходных DXF С1 ещё не установлены на сервере.")
    return tuple(_checked_bytes(Path(root) / EXAMPLE_ID / name, digest) for _, _, name, digest in SOURCES)


def metadata(*, available, status):
    return {"id": EXAMPLE_ID, "title": "С1 · фундаментная плита · плоский MVP",
        "description": "Реальные четыре DXF А101 из 1-КЖ00.С1-2: 3680 КЭ на направление. "
            "Плоская постановка без отверстий и перепадов высоты; внешний контур сохраняется.",
        "source_kind": "real_engineering_files", "is_available": available, "status": status,
        "supports_working_host_trim": False,
        "reference": {"mass_kg": None, "physical_bar_count": None, "position_count": None,
            "scope": "PDF содержит совместную выдачу С1 и С2.",
            "note": "Итоги отдельно С1 ещё не извлечены; метрики К09 и всей С1+С2 здесь не подставляются."},
        "sources": [{"direction": {"layer": layer, "axis": axis}, "dxf_filename": name,
            "sha256": digest, "shk_filename": None, "mapping_id": LEGACY_S1_D18.id,
            "mapping_label": "Шкала С1 · фон Ø18@300 · таблица А101 2.4.3"}
            for layer, axis, name, digest in SOURCES],
        "profile": {"id": PROFILE_ID, "engineering_approval": False,
            "note": "MVP: плоская плита 800 мм по внешнему контуру КЭ-сетки. "
                "Отверстия, перепады высоты и защитный слой не учитываются. "
                "Потребность сохраняется; 40d и раскрой проверяются отдельно. "
                "Фон Ø18@300, начало 0 мм; первая добавка @300 со сдвигом 100 мм, "
                "@150 — последовательность 100/200 мм. X ближе к граням, Y глубже на 36 мм. "
                "Это принятый профиль, не подтверждённые параметры рабочего Revit."}}


def analyze_s1_example():
    originals = source_bytes()
    with TemporaryDirectory(prefix="rebar-s1-") as temporary:
        sources, settings = [], []
        for (layer, axis, name, _), content in zip(SOURCES, originals):
            path = Path(temporary) / name
            path.write_bytes(content)
            sources.append(PlateDirectionSource(path, mapping_id=LEGACY_S1_D18.id))
            settings.append(CompositeDirectionSettings(Direction(Layer(layer), Axis(axis)),
                0, 100, 0, "A500", "User-selected flat S1 MVP profile; not measured Revit", "left"))
        source = analyze_assistant_sources(tuple(sources), case_id=EXAMPLE_ID)
        provenance = {"mode": "fresh-four-original-dxf", "case_id": EXAMPLE_ID,
            "sources": metadata(available=True, status="ready")["sources"],
            **{key: source.provenance[key] for key in ("algorithm", "config", "candidate_id", "selection")}}
        recovery = recover_patterned_layout(source.problem, source.solution, tuple(settings))
        recovery.report["source_provenance"] = provenance
        report = flat_mvp_source_web_report(source.problem, recovery.report, repair_deficits=True)
    report["engineering_example"] = metadata(available=True, status="ready")
    return report
