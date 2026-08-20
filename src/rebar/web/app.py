"""FastAPI-приложение для синхронного анализа одного DXF."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

from ezdxf.lldxf.const import DXFError
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from rebar.application import (
    DEFAULT_ALGORITHMS,
    IRREGULAR_PLATE_DEMO,
    DirectionAnalysis,
    analyze_direction,
    available_cutting_profile_ids,
    available_demo_cases,
    available_mapping_ids,
    get_demo_case,
    write_demo_dxf,
)
from rebar.optimization import (
    MissingRebarSpecificationError,
    RebarMappingError,
    built_in_optimizer_registry,
)
from rebar.reporting.serialization import to_jsonable
from rebar.reporting.svg import render_solution_svg

STATIC_DIR = Path(__file__).with_name("static")
MAX_UPLOAD_BYTES = 30 * 1024 * 1024

ALGORITHM_INFO = {
    "agglomerative": {
        "title": "Agglomerative",
        "description": "Объединяет соседние области снизу вверх.",
    },
    "bbox": {
        "title": "Bounding box",
        "description": "Быстрый тяжёлый baseline одной зоной.",
    },
    "bsp": {
        "title": "BSP",
        "description": "Рекурсивно ищет выгодные ортогональные разрезы.",
    },
    "greedy": {
        "title": "Greedy",
        "description": "Последовательно выбирает выгодные полосы.",
    },
    "greedy-priority": {
        "title": "Priority greedy",
        "description": "Сначала локализует самые сильные требования.",
    },
    "row-run-greedy": {
        "title": "Row-run greedy",
        "description": "Собирает продольные серии КЭ и выгодно объединяет соседние.",
    },
    "spatial-partition-greedy": {
        "title": "Spatial partition greedy",
        "description": "Делит плиту на непересекающиеся плитки и безопасно укрупняет их.",
    },
    "strip-profile-dp": {
        "title": "Strip profile DP",
        "description": "Ищет лучший вариант внутри класса поперечных полос.",
    },
}

app = FastAPI(
    title="Rebar Auto-Layout",
    description="Локальный MVP анализа одного направления армирования из DXF.",
    version="0.1.0",
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _safe_name(upload: UploadFile, fallback: str) -> str:
    candidate = (upload.filename or fallback).replace("\\", "/")
    return Path(candidate).name or fallback


async def _save_upload(upload: UploadFile, destination: Path) -> None:
    written = 0
    with destination.open("wb") as output:
        while chunk := await upload.read(1024 * 1024):
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="Файл превышает лимит 30 МБ.")
            output.write(chunk)
    if written == 0:
        raise HTTPException(status_code=400, detail=f"Файл {destination.name!r} пуст.")


def _source_payload(
    analysis: DirectionAnalysis,
    filename: str,
    *,
    source_kind: str,
    source_id: str | None,
) -> dict:
    mosaic = analysis.mosaic
    mapping = mosaic.meta.get("rebar_mapping") or {}
    return {
        "filename": filename,
        "source_kind": source_kind,
        "source_id": source_id,
        "direction": {
            "layer": mosaic.direction.layer.value,
            "axis": mosaic.direction.axis.value,
        },
        "units": "mm",
        "source_units": mosaic.meta.get("source_units"),
        "unit_detection": mosaic.meta.get("unit_detection"),
        "cell_count": len(mosaic.cells),
        "bbox": list(mosaic.bbox),
        "level_count": len(analysis.problem.demand.levels),
        "legend_source": "mapping" if mapping else "shk",
        "mapping_id": mapping.get("id"),
    }


def _analysis_payload(
    analysis: DirectionAnalysis,
    filename: str,
    *,
    source_kind: str,
    source_id: str | None = None,
) -> dict:
    solutions = []
    for solution in analysis.solutions:
        payload = to_jsonable(solution)
        payload["physical_bar_count"] = solution.metrics.physical_bar_count
        payload["svg"] = render_solution_svg(analysis.problem, solution)
        solutions.append(payload)

    return {
        "schema_version": 2,
        "source": _source_payload(
            analysis,
            filename,
            source_kind=source_kind,
            source_id=source_id,
        ),
        "constraints": to_jsonable(analysis.problem.constraints),
        "solutions": solutions,
    }


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/options")
def options() -> dict:
    registry_names = built_in_optimizer_registry().names()
    return {
        "algorithms": [
            {
                "id": name,
                **ALGORITHM_INFO[name],
                "default": name in DEFAULT_ALGORITHMS,
            }
            for name in registry_names
        ],
        "mappings": [
            {
                "id": "auto",
                "title": "Автоматически / .shk",
                "description": "Использовать загруженный или однозначно найденный .shk.",
            },
            *[
                {
                    "id": mapping_id,
                    "title": "Плита нуля · ⌀12",
                    "description": "Зафиксированная MVP-таблица для golden-case.",
                }
                for mapping_id in available_mapping_ids()
            ],
        ],
        "demo_cases": [
            {
                "id": case.id,
                "title": case.title,
                "description": case.description,
                "default": case.id == IRREGULAR_PLATE_DEMO.id,
            }
            for case in available_demo_cases()
        ],
        "cutting_profiles": [
            {
                "id": "continuous",
                "title": "Без округления",
                "description": "Фактическая длина равна минимуму с анкеровкой.",
            },
            *[
                {
                    "id": profile_id,
                    "title": "Раскрой из прутка 11 700 мм",
                    "description": "Проектный каталог кратных длин из готового КЖ.",
                }
                for profile_id in available_cutting_profile_ids()
                if profile_id != "continuous"
            ],
        ],
        "defaults": {
            "source_mode": "demo",
            "demo_id": IRREGULAR_PLATE_DEMO.id,
            "algorithms": list(DEFAULT_ALGORITHMS),
            "max_details": 32,
            "min_width_cells": 2,
            "cutting_profile": "continuous",
        },
        "limits": {"max_upload_mb": MAX_UPLOAD_BYTES // 1024 // 1024},
    }


async def _execute_analysis(
    dxf_path: Path,
    *,
    filename: str,
    source_kind: str,
    source_id: str | None,
    shk_path: Path | None,
    mapping_id: str,
    algorithms: str,
    max_details: int,
    min_width_cells: int,
    detail_penalty_kg: float,
    cutting_profile: str,
) -> dict:
    """Выполнить общий application-сценарий для upload и встроенного DXF."""

    algorithm_names = tuple(name.strip() for name in algorithms.split(",") if name.strip())
    try:
        analysis = await run_in_threadpool(
            analyze_direction,
            dxf_path,
            shk_path=shk_path,
            mapping_id=mapping_id,
            algorithm_names=algorithm_names,
            max_details=max_details,
            min_width_cells=min_width_cells,
            detail_penalty_kg=detail_penalty_kg,
            cutting_profile=cutting_profile,
        )
    except (DXFError, KeyError, MissingRebarSpecificationError, RebarMappingError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=400, detail=f"Не удалось прочитать файл: {error}") from error

    return _analysis_payload(
        analysis,
        filename,
        source_kind=source_kind,
        source_id=source_id,
    )


@app.post("/api/analyze")
async def analyze(
    dxf: Annotated[UploadFile, File(description="Один DXF одного направления")],
    shk: Annotated[UploadFile | None, File(description="Необязательная шкала .shk")] = None,
    mapping_id: Annotated[str, Form()] = "auto",
    algorithms: Annotated[str, Form()] = ",".join(DEFAULT_ALGORITHMS),
    max_details: Annotated[int, Form(ge=1, le=100)] = 32,
    min_width_cells: Annotated[int, Form(ge=1, le=10)] = 2,
    detail_penalty_kg: Annotated[float, Form(ge=0, le=1_000_000)] = 0.0,
    cutting_profile: Annotated[str, Form()] = "continuous",
) -> dict:
    dxf_name = _safe_name(dxf, "input.dxf")
    if Path(dxf_name).suffix.casefold() != ".dxf":
        raise HTTPException(status_code=400, detail="Основной файл должен иметь расширение .dxf.")

    shk_name: str | None = None
    if shk is not None:
        shk_name = _safe_name(shk, "scale.shk")
        if Path(shk_name).suffix.casefold() != ".shk":
            raise HTTPException(status_code=400, detail="Файл шкалы должен иметь расширение .shk.")
    if shk is not None and mapping_id.strip().casefold() != "auto":
        raise HTTPException(
            status_code=400,
            detail="Выберите либо .shk, либо ручную таблицу армирования — не оба варианта.",
        )

    with TemporaryDirectory(prefix="rebar-web-") as temp_dir:
        temp_path = Path(temp_dir)
        dxf_path = temp_path / dxf_name
        await _save_upload(dxf, dxf_path)
        shk_path: Path | None = None
        if shk is not None and shk_name is not None:
            shk_path = temp_path / shk_name
            await _save_upload(shk, shk_path)

        return await _execute_analysis(
            dxf_path,
            filename=dxf_name,
            source_kind="upload",
            source_id=None,
            shk_path=shk_path,
            mapping_id=mapping_id,
            algorithms=algorithms,
            max_details=max_details,
            min_width_cells=min_width_cells,
            detail_penalty_kg=detail_penalty_kg,
            cutting_profile=cutting_profile,
        )


@app.post("/api/demo")
async def analyze_demo(
    demo_id: Annotated[str, Form()] = IRREGULAR_PLATE_DEMO.id,
    algorithms: Annotated[str, Form()] = ",".join(DEFAULT_ALGORITHMS),
    max_details: Annotated[int, Form(ge=1, le=100)] = 32,
    min_width_cells: Annotated[int, Form(ge=1, le=10)] = 2,
    detail_penalty_kg: Annotated[float, Form(ge=0, le=1_000_000)] = 0.0,
    cutting_profile: Annotated[str, Form()] = "continuous",
) -> dict:
    """Рассчитать встроенный синтетический DXF тем же production-пайплайном."""

    try:
        case = get_demo_case(demo_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    with TemporaryDirectory(prefix="rebar-demo-") as temp_dir:
        dxf_path = Path(temp_dir) / case.filename
        write_demo_dxf(case.id, dxf_path)
        return await _execute_analysis(
            dxf_path,
            filename=case.title,
            source_kind="demo",
            source_id=case.id,
            shk_path=None,
            mapping_id=case.mapping_id,
            algorithms=algorithms,
            max_details=max_details,
            min_width_cells=min_width_cells,
            detail_penalty_kg=detail_penalty_kg,
            cutting_profile=cutting_profile,
        )
