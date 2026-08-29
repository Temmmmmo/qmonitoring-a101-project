"""FastAPI-приложение для анализа одного DXF или полного комплекта плиты."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

from ezdxf.lldxf.const import DXFError
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from rebar.application import (
    DEFAULT_ALGORITHMS,
    IRREGULAR_PLATE_DEMO,
    DirectionAnalysis,
    PlateAnalysis,
    PlateDirectionSource,
    analyze_direction,
    analyze_plate,
    assess_layout_gates,
    assess_plate_gates,
    available_cutting_profile_ids,
    available_demo_cases,
    available_mapping_ids,
    build_plate_solution_revit_export,
    get_demo_case,
    write_demo_dxf,
)
from rebar.golden import GOLDEN_CASES, GoldenCaseDefinition, get_golden_case
from rebar.optimization import (
    MissingRebarSpecificationError,
    RebarMappingError,
    built_in_optimizer_registry,
    measure_plate_constructability,
    zone_count_bounds,
)
from rebar.reporting.serialization import to_jsonable
from rebar.reporting.svg import render_solution_svg
from rebar.reporting.zone_schedule import build_zone_schedule

STATIC_DIR = Path(__file__).with_name("static")
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
OPTIONS_SCHEMA_VERSION = 2

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
    "genetic-pareto": {
        "title": "Genetic Pareto",
        "description": (
            "Эволюционно строит серию вариантов; UCB адаптивно выбирает "
            "предметные мутации."
        ),
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

MAPPING_INFO = {
    "k09-above-3-d10-v1": {
        "title": "Плита над 3 этажом · ⌀10",
        "description": "Таблица из собственной PNG-легенды комплекта над 3 этажом.",
    },
    "k09-minus-2-d12-v1": {
        "title": "Плита над −2 этажом · ⌀12",
        "description": "Таблица из собственной PNG-легенды комплекта над −2 этажом.",
    },
    "plate-zero-d12-v1": {
        "title": "Плита нуля · ⌀12",
        "description": "Таблица из собственной PNG-легенды golden-case плиты нуля.",
    },
}

app = FastAPI(
    title="Rebar Auto-Layout",
    description="Локальный MVP анализа одного направления или полного комплекта плиты.",
    version="0.1.0",
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def prevent_stale_web_assets(request: Request, call_next):
    """Не смешивать UI и API разных релизов в кеше браузера или reverse proxy."""

    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith(("/api/", "/static/")):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


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
    minimum_zones, maximum_zones = zone_count_bounds(analysis.problem)
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
        "zone_count_bounds": {
            "minimum": minimum_zones,
            "maximum": maximum_zones,
        },
        "single_cell_preprocessing": analysis.problem.meta.get(
            "single_cell_preprocessing"
        ),
    }


def _analysis_payload(
    analysis: DirectionAnalysis,
    filename: str,
    *,
    source_kind: str,
    source_id: str | None = None,
) -> dict:
    baseline_solutions = [
        _layout_solution_payload(analysis.problem, solution)
        for solution in analysis.solutions
    ]
    if analysis.front is None:
        solutions = baseline_solutions
        pareto_payload = None
    else:
        solutions = []
        for index, candidate in enumerate(analysis.front.candidates):
            payload = _layout_solution_payload(
                analysis.problem,
                candidate.solution,
            )
            payload.update(
                {
                    "title": f"Вариант {index + 1}",
                    "candidate_id": candidate.id,
                    "constructability": to_jsonable(candidate.constructability),
                    "equivalent_candidate_ids": list(
                        candidate.equivalent_candidate_ids
                    ),
                }
            )
            solutions.append(payload)
        if not solutions:
            solutions = baseline_solutions
        pareto_payload = {
            "complexity_axis": analysis.front.complexity_axis.value,
            "source_candidate_count": analysis.front.source_candidate_count,
            "dominated_candidate_count": analysis.front.dominated_candidate_count,
            "equivalent_candidate_count": analysis.front.equivalent_candidate_count,
            "rejected_candidate_count": len(analysis.front.rejections),
            "points": [
                {
                    "candidate_id": candidate.id,
                    "solution_index": index,
                    "total_mass_kg": candidate.solution.metrics.total_mass_kg,
                    "complexity": candidate.constructability.value(
                        analysis.front.complexity_axis
                    ),
                    "constructability": to_jsonable(candidate.constructability),
                    "equivalent_candidate_count": len(
                        candidate.equivalent_candidate_ids
                    ),
                }
                for index, candidate in enumerate(analysis.front.candidates)
            ],
        }

    return {
        "schema_version": 3,
        "kind": "direction",
        "source": _source_payload(
            analysis,
            filename,
            source_kind=source_kind,
            source_id=source_id,
        ),
        "constraints": to_jsonable(analysis.problem.constraints),
        "pareto_front": pareto_payload,
        "baseline_solutions": baseline_solutions,
        "solutions": solutions,
    }


def _layout_solution_payload(problem, solution) -> dict:
    payload = to_jsonable(solution)
    payload["physical_bar_count"] = solution.metrics.physical_bar_count
    payload["zone_schedule"] = to_jsonable(
        build_zone_schedule(problem.demand.direction, solution)
    )
    payload["svg"] = render_solution_svg(problem, solution)
    payload["gate_assessment"] = _gate_assessment_payload(
        assess_layout_gates(problem, solution)
    )
    return payload


def _gate_assessment_payload(assessment) -> dict:
    return {
        "summary": assessment.summary,
        "reference_id": assessment.reference_id,
        "reference_title": assessment.reference_title,
        "items": to_jsonable(assessment.items),
    }


def _plate_analysis_payload(
    analysis: PlateAnalysis,
    filename_by_path: dict[str, str],
    reference: GoldenCaseDefinition | None,
) -> dict:
    direction_analyses = {
        item.problem.demand.direction: item for item in analysis.direction_analyses
    }
    sources = []
    for item in analysis.direction_analyses:
        source_path = item.mosaic.source_path
        sources.append(
            _source_payload(
                item,
                filename_by_path.get(source_path, Path(source_path).name),
                source_kind="upload",
                source_id=None,
            )
        )

    def plate_solution_payload(
        plate_solution,
        *,
        title: str,
        constructability=None,
        candidate_id: str | None = None,
        direction_candidate_ids: tuple[str, ...] = (),
        equivalent_candidate_ids: tuple[str, ...] = (),
    ) -> dict:
        algorithms = set(plate_solution.meta["algorithm_by_direction"].values())
        payload = {
            "title": title,
            "candidate_id": candidate_id,
            "algorithm": next(iter(algorithms)) if len(algorithms) == 1 else "mixed",
            "status": plate_solution.status.value,
            "valid": plate_solution.valid,
            "metrics": to_jsonable(plate_solution.metrics),
            "constructability": to_jsonable(
                constructability or measure_plate_constructability(plate_solution)
            ),
            "runtime_ms": plate_solution.runtime_ms,
            "diagnostics": list(plate_solution.diagnostics),
            "meta": to_jsonable(plate_solution.meta),
            "direction_candidate_ids": list(direction_candidate_ids),
            "equivalent_candidate_ids": list(equivalent_candidate_ids),
            "gate_assessment": _gate_assessment_payload(
                assess_plate_gates(
                    analysis.problem,
                    plate_solution,
                    reference=reference,
                )
            ),
            "direction_solutions": [],
        }
        payload["revit_export"] = build_plate_solution_revit_export(
            analysis.problem,
            plate_solution,
            candidate_id=candidate_id,
        )
        for direction_solution in plate_solution.direction_solutions:
            direction = direction_solution.direction
            direction_analysis = direction_analyses[direction]
            payload["direction_solutions"].append(
                {
                    "direction": {
                        "layer": direction.layer.value,
                        "axis": direction.axis.value,
                    },
                    "solution": _layout_solution_payload(
                        direction_analysis.problem,
                        direction_solution.solution,
                    ),
                }
            )
        return payload

    baseline_solutions = [
        plate_solution_payload(
            plate_solution,
            title=(
                ALGORITHM_INFO.get(
                    next(iter(plate_solution.meta["algorithm_by_direction"].values())),
                    {},
                ).get("title", "Контрольный baseline")
            ),
        )
        for plate_solution in analysis.solutions
    ]
    if analysis.front is None:
        solutions = baseline_solutions
        pareto_payload = None
    else:
        solutions = [
            plate_solution_payload(
                candidate.solution,
                title=f"Вариант {index + 1}",
                constructability=candidate.constructability,
                candidate_id=candidate.id,
                direction_candidate_ids=candidate.direction_candidate_ids,
                equivalent_candidate_ids=candidate.equivalent_candidate_ids,
            )
            for index, candidate in enumerate(analysis.front.candidates)
        ] or baseline_solutions
        pareto_payload = {
            "complexity_axis": analysis.front.complexity_axis.value,
            "combination_count": analysis.front.combination_count,
            "dominated_candidate_count": analysis.front.dominated_candidate_count,
            "equivalent_candidate_count": analysis.front.equivalent_candidate_count,
            "rejected_direction_candidate_count": sum(
                len(front.rejections) for front in analysis.front.direction_fronts
            ),
            "points": [
                {
                    "candidate_id": candidate.id,
                    "solution_index": index,
                    "total_mass_kg": candidate.solution.metrics.total_mass_kg,
                    "complexity": candidate.constructability.value(
                        analysis.front.complexity_axis
                    ),
                    "constructability": to_jsonable(candidate.constructability),
                    "equivalent_candidate_count": len(
                        candidate.equivalent_candidate_ids
                    ),
                }
                for index, candidate in enumerate(analysis.front.candidates)
            ],
        }

    return {
        "schema_version": 4,
        "kind": "plate",
        "plate": {
            "case_id": analysis.problem.case_id,
            "direction_count": len(analysis.direction_analyses),
            "units": "mm",
        },
        "sources": sources,
        "constraints_by_direction": [
            {
                "direction": {
                    "layer": item.problem.demand.direction.layer.value,
                    "axis": item.problem.demand.direction.axis.value,
                },
                "constraints": to_jsonable(item.problem.constraints),
            }
            for item in analysis.direction_analyses
        ],
        "pareto_front": pareto_payload,
        "baseline_solutions": baseline_solutions,
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
        "schema_version": OPTIONS_SCHEMA_VERSION,
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
                    **MAPPING_INFO[mapping_id],
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
        "references": [
            {
                "id": reference.id,
                "title": reference.title,
                "expected_mass_kg": reference.expected_mass_kg,
                "expected_bar_count": reference.expected_bar_count,
                "expected_position_count": reference.expected_position_count,
            }
            for reference in GOLDEN_CASES.values()
        ],
        "defaults": {
            "source_mode": "demo",
            "demo_id": IRREGULAR_PLATE_DEMO.id,
            "algorithms": list(DEFAULT_ALGORITHMS),
            "max_details": None,
            "min_width_cells": 2,
            "cutting_profile": "continuous",
            "genetic_population_size": 16,
            "genetic_generations": 30,
            "genetic_seed": 42,
            "genetic_operator_policy": "ucb1",
            "genetic_ucb_exploration": 2**0.5,
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
    max_details: int | None,
    min_width_cells: int,
    detail_penalty_kg: float,
    cutting_profile: str,
    genetic_population_size: int,
    genetic_generations: int,
    genetic_seed: int,
    genetic_operator_policy: str,
    genetic_ucb_exploration: float,
) -> dict:
    """Выполнить общий application-сценарий для upload и встроенного DXF."""

    algorithm_names = tuple(name.strip() for name in algorithms.split(",") if name.strip())
    algorithm_params = (
        {
            "genetic-pareto": {
                "population_size": genetic_population_size,
                "generations": genetic_generations,
                "random_seed": genetic_seed,
                "operator_policy": genetic_operator_policy,
                "ucb_exploration": genetic_ucb_exploration,
            }
        }
        if "genetic-pareto" in {name.casefold() for name in algorithm_names}
        else None
    )
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
            algorithm_params=algorithm_params,
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


async def _execute_plate_analysis(
    sources: tuple[PlateDirectionSource, ...],
    *,
    filename_by_path: dict[str, str],
    algorithms: str,
    max_details: int | None,
    min_width_cells: int,
    detail_penalty_kg: float,
    cutting_profile: str,
    case_id: str,
    reference: GoldenCaseDefinition | None,
    genetic_population_size: int,
    genetic_generations: int,
    genetic_seed: int,
    genetic_operator_policy: str,
    genetic_ucb_exploration: float,
) -> dict:
    """Выполнить общеплитный application-сценарий в рабочем потоке."""

    algorithm_names = tuple(name.strip() for name in algorithms.split(",") if name.strip())
    algorithm_params = (
        {
            "genetic-pareto": {
                "population_size": genetic_population_size,
                "generations": genetic_generations,
                "random_seed": genetic_seed,
                "operator_policy": genetic_operator_policy,
                "ucb_exploration": genetic_ucb_exploration,
            }
        }
        if "genetic-pareto" in {name.casefold() for name in algorithm_names}
        else None
    )
    try:
        analysis = await run_in_threadpool(
            analyze_plate,
            sources,
            algorithm_names=algorithm_names,
            max_details_per_direction=max_details,
            min_width_cells=min_width_cells,
            detail_penalty_kg=detail_penalty_kg,
            cutting_profile=cutting_profile,
            case_id=case_id,
            algorithm_params=algorithm_params,
        )
    except (DXFError, KeyError, MissingRebarSpecificationError, RebarMappingError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=400, detail=f"Не удалось прочитать файл: {error}") from error

    return _plate_analysis_payload(analysis, filename_by_path, reference)


@app.post("/api/analyze")
async def analyze(
    dxf: Annotated[UploadFile, File(description="Один DXF одного направления")],
    shk: Annotated[UploadFile | None, File(description="Необязательная шкала .shk")] = None,
    mapping_id: Annotated[str, Form()] = "auto",
    algorithms: Annotated[str, Form()] = ",".join(DEFAULT_ALGORITHMS),
    max_details: Annotated[int | None, Form(ge=1)] = None,
    min_width_cells: Annotated[int, Form(ge=1, le=10)] = 2,
    detail_penalty_kg: Annotated[float, Form(ge=0, le=1_000_000)] = 0.0,
    cutting_profile: Annotated[str, Form()] = "continuous",
    genetic_population_size: Annotated[int, Form(ge=4, le=64)] = 16,
    genetic_generations: Annotated[int, Form(ge=1, le=200)] = 30,
    genetic_seed: Annotated[int, Form(ge=0, le=2_147_483_647)] = 42,
    genetic_operator_policy: Annotated[str, Form()] = "ucb1",
    genetic_ucb_exploration: Annotated[float, Form(ge=0, le=10)] = 2**0.5,
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
            genetic_population_size=genetic_population_size,
            genetic_generations=genetic_generations,
            genetic_seed=genetic_seed,
            genetic_operator_policy=genetic_operator_policy,
            genetic_ucb_exploration=genetic_ucb_exploration,
        )


@app.post("/api/analyze-plate")
async def analyze_plate_upload(
    dxf_bottom_x: Annotated[UploadFile, File(description="Нижнее армирование вдоль X")],
    dxf_bottom_y: Annotated[UploadFile, File(description="Нижнее армирование вдоль Y")],
    dxf_top_x: Annotated[UploadFile, File(description="Верхнее армирование вдоль X")],
    dxf_top_y: Annotated[UploadFile, File(description="Верхнее армирование вдоль Y")],
    shk: Annotated[
        UploadFile | None,
        File(description="Необязательный общий .shk для четырёх направлений"),
    ] = None,
    mapping_id: Annotated[str, Form()] = "plate-zero-d12-v1",
    algorithms: Annotated[str, Form()] = ",".join(DEFAULT_ALGORITHMS),
    max_details: Annotated[int | None, Form(ge=1)] = None,
    min_width_cells: Annotated[int, Form(ge=1, le=10)] = 2,
    detail_penalty_kg: Annotated[float, Form(ge=0, le=1_000_000)] = 0.0,
    cutting_profile: Annotated[str, Form()] = "continuous",
    genetic_population_size: Annotated[int, Form(ge=4, le=64)] = 16,
    genetic_generations: Annotated[int, Form(ge=1, le=200)] = 30,
    genetic_seed: Annotated[int, Form(ge=0, le=2_147_483_647)] = 42,
    genetic_operator_policy: Annotated[str, Form()] = "ucb1",
    genetic_ucb_exploration: Annotated[float, Form(ge=0, le=10)] = 2**0.5,
    case_id: Annotated[str, Form(max_length=200)] = "",
    reference_id: Annotated[str, Form(max_length=200)] = "",
) -> dict:
    """Рассчитать четыре направления с явной таблицей или общим ``.shk``."""

    uploads = (dxf_bottom_x, dxf_bottom_y, dxf_top_x, dxf_top_y)
    safe_names = tuple(
        _safe_name(upload, f"direction-{index + 1}.dxf")
        for index, upload in enumerate(uploads)
    )
    if any(Path(name).suffix.casefold() != ".dxf" for name in safe_names):
        raise HTTPException(status_code=400, detail="Все четыре файла должны иметь расширение .dxf.")
    normalized_mapping_id = mapping_id.strip().casefold()
    shk_name: str | None = None
    if shk is not None:
        shk_name = _safe_name(shk, "plate-scale.shk")
        if Path(shk_name).suffix.casefold() != ".shk":
            raise HTTPException(
                status_code=400,
                detail="Общий файл шкалы должен иметь расширение .shk.",
            )
    if shk is not None and normalized_mapping_id != "auto":
        raise HTTPException(
            status_code=400,
            detail="Выберите либо общий .shk, либо встроенную таблицу — не оба варианта.",
        )
    if shk is None and normalized_mapping_id == "auto":
        raise HTTPException(
            status_code=400,
            detail=(
                "Для общеплитного расчёта загрузите общий .shk либо выберите "
                "встроенную таблицу армирования."
            ),
        )
    try:
        reference = get_golden_case(reference_id) if reference_id else None
    except KeyError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    with TemporaryDirectory(prefix="rebar-plate-web-") as temp_dir:
        temp_path = Path(temp_dir)
        saved_paths = tuple(
            temp_path / f"{index + 1}-{name}"
            for index, name in enumerate(safe_names)
        )
        for upload, destination in zip(uploads, saved_paths):
            await _save_upload(upload, destination)

        shk_path: Path | None = None
        if shk is not None and shk_name is not None:
            shk_path = temp_path / f"shared-{shk_name}"
            await _save_upload(shk, shk_path)

        filename_by_path = {
            str(path): filename for path, filename in zip(saved_paths, safe_names)
        }
        sources = tuple(
            PlateDirectionSource(
                path,
                shk_path=shk_path,
                mapping_id=normalized_mapping_id,
            )
            for path in saved_paths
        )
        return await _execute_plate_analysis(
            sources,
            filename_by_path=filename_by_path,
            algorithms=algorithms,
            max_details=max_details,
            min_width_cells=min_width_cells,
            detail_penalty_kg=detail_penalty_kg,
            cutting_profile=cutting_profile,
            case_id=case_id.strip(),
            reference=reference,
            genetic_population_size=genetic_population_size,
            genetic_generations=genetic_generations,
            genetic_seed=genetic_seed,
            genetic_operator_policy=genetic_operator_policy,
            genetic_ucb_exploration=genetic_ucb_exploration,
        )


@app.post("/api/demo")
async def analyze_demo(
    demo_id: Annotated[str, Form()] = IRREGULAR_PLATE_DEMO.id,
    algorithms: Annotated[str, Form()] = ",".join(DEFAULT_ALGORITHMS),
    max_details: Annotated[int | None, Form(ge=1)] = None,
    min_width_cells: Annotated[int, Form(ge=1, le=10)] = 2,
    detail_penalty_kg: Annotated[float, Form(ge=0, le=1_000_000)] = 0.0,
    cutting_profile: Annotated[str, Form()] = "continuous",
    genetic_population_size: Annotated[int, Form(ge=4, le=64)] = 16,
    genetic_generations: Annotated[int, Form(ge=1, le=200)] = 30,
    genetic_seed: Annotated[int, Form(ge=0, le=2_147_483_647)] = 42,
    genetic_operator_policy: Annotated[str, Form()] = "ucb1",
    genetic_ucb_exploration: Annotated[float, Form(ge=0, le=10)] = 2**0.5,
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
            genetic_population_size=genetic_population_size,
            genetic_generations=genetic_generations,
            genetic_seed=genetic_seed,
            genetic_operator_policy=genetic_operator_policy,
            genetic_ucb_exploration=genetic_ucb_exploration,
        )
