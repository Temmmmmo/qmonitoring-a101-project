"""One-click real engineering input; operators install files outside the repository."""
from threading import Lock

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from rebar.application.engineering_example import (
    EngineeringFilesUnavailableError, analyze_engineering_example, engineering_example_catalog,
)

router = APIRouter()
_calculation_lock = Lock()


@router.get("/api/engineering-examples")
async def catalog():
    return await run_in_threadpool(engineering_example_catalog)


def _analyze(example_id):
    if not _calculation_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Расчёт плиты уже выполняется. Дождись результата и повтори запуск.")
    try:
        return analyze_engineering_example(example_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Такого инженерного комплекта нет.") from error
    except EngineeringFilesUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        _calculation_lock.release()


@router.post("/api/engineering-examples/{example_id}/analyze")
async def analyze(example_id: str):
    return await run_in_threadpool(_analyze, example_id)
