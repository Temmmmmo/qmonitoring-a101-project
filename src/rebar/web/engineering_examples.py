"""One-click real engineering input; operators install files outside the repository."""
from threading import Lock

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

from rebar.application.engineering_example import (
    EngineeringFilesUnavailableError, analyze_engineering_example, engineering_example_catalog,
)
from rebar.application.working_host import load_working_host_json
from rebar.application.working_solid_host import MAX_WORKING_REPORT_BYTES, inspect_working_solid

router = APIRouter()
_calculation_lock = Lock()


@router.get("/api/engineering-examples")
async def catalog():
    return await run_in_threadpool(engineering_example_catalog)


def _analyze(example_id, **kwargs):
    if not _calculation_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Расчёт плиты уже выполняется. Дождись результата и повтори запуск.")
    try:
        return analyze_engineering_example(example_id, **kwargs)
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


@router.post("/api/engineering-examples/{example_id}/boundary-trim")
async def boundary_trim(example_id: str, request: Request):
    """An explicitly selected NEW calculation; no cached physical packet accepted."""
    limit = MAX_WORKING_REPORT_BYTES + 64*1024
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise HTTPException(422, "Нужна multipart-форма с Working Host JSON и подтверждением XY")
    length = request.headers.get("content-length")
    if length is not None:
        try:
            size = int(length)
        except ValueError as error:
            raise HTTPException(422, "Некорректный Content-Length") from error
        if size < 0:
            raise HTTPException(422, "Некорректный Content-Length")
        if size > limit:
            raise HTTPException(413, "JSON Working Host превышает 32 MiB")
    try:
        chunks, total = [], 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                raise HTTPException(413, "JSON Working Host превышает 32 MiB")
            if chunk:
                chunks.append(chunk)
        cursor = iter(chunks)

        async def receive():
            chunk = next(cursor, None)
            return {"type": "http.request", "body": chunk or b"", "more_body": chunk is not None}

        bounded = Request(request.scope, receive=receive)
        async with bounded.form(max_files=1, max_fields=1, max_part_size=1024) as form:
            if sorted(k for k, _ in form.multi_items()) != ["host_xy_confirmed", "working_host"]:
                raise ValueError("Нужны ровно Working Host JSON и подтверждение совпадения XY")
            upload, confirmed = form["working_host"], form["host_xy_confirmed"]
            if confirmed != "true" or not isinstance(upload, UploadFile):
                raise ValueError("Подтвердите совпадение XY исходных DXF и выбранной плиты")
            if not (upload.filename or "").lower().endswith(".json"):
                raise ValueError("Выберите JSON-отчёт Working Host")
            content = await upload.read(MAX_WORKING_REPORT_BYTES+1)
            if len(content) > MAX_WORKING_REPORT_BYTES:
                raise HTTPException(413, "JSON Working Host превышает 32 MiB")
            # Reject unsupported/partial host geometry BEFORE the expensive DXF calculation.
            await run_in_threadpool(inspect_working_solid,
                load_working_host_json(content, maximum_bytes=MAX_WORKING_REPORT_BYTES))
            return await run_in_threadpool(_analyze, example_id,
                working_host_bytes=content, confirm_identity_xy=True)
    except (ValueError, ClientDisconnect) as error:
        raise HTTPException(422, str(error) or "Передача JSON прервана") from error
    except StarletteHTTPException as error:
        if isinstance(error, HTTPException):
            raise
        raise HTTPException(422, "Некорректная multipart-форма: один Working Host JSON и подтверждение XY") from error
