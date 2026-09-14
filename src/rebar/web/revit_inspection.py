"""Read-only upload endpoint; bound the wire body BEFORE multipart spooling."""
from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

from rebar.application.revit_snapshot_inspection import (
    MAX_SNAPSHOT_BYTES, SnapshotTooLargeError, inspect_revit_snapshots,
)

router = APIRouter()
MAX_REQUEST_BYTES = MAX_SNAPSHOT_BYTES + 64 * 1024


@router.post("/api/revit/inspect-snapshots", openapi_extra={"requestBody": {
    "required": True, "content": {"multipart/form-data": {"schema": {
        "type": "object", "properties": {"host": {"type": "string", "format": "binary"},
            "rebar": {"type": "string", "format": "binary"}}}}}}})
async def inspect_snapshot_uploads(request: Request):
    """Optional host/rebar files, combined 32 MiB, no storage or download handle."""
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise HTTPException(422, "Нужна multipart-форма с JSON-файлами host и/или rebar")
    length = request.headers.get("content-length")
    if length is not None:
        try:
            size = int(length)
        except ValueError as error:
            raise HTTPException(422, "Некорректный Content-Length") from error
        if size < 0:
            raise HTTPException(422, "Некорректный Content-Length")
        if size > MAX_REQUEST_BYTES:
            raise HTTPException(413, "Суммарный размер JSON превышает 32 MiB")
    try:
        chunks, total = [], 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_REQUEST_BYTES:
                raise SnapshotTooLargeError("Суммарный размер JSON превышает 32 MiB")
            if chunk:
                chunks.append(chunk)
        # Replay only the already bounded body through Starlette's standard parser.
        # Original Request.stream is consumed; never set private _body/_receive fields.
        cursor = iter(chunks)

        async def receive():
            chunk = next(cursor, None)
            return {"type": "http.request", "body": chunk or b"", "more_body": chunk is not None}

        bounded = Request(request.scope, receive=receive)
        async with bounded.form(max_files=2, max_fields=0, max_part_size=1024) as form:
            values, total = {}, 0
            for key, value in form.multi_items():
                if key not in ("host", "rebar") or key in values or not isinstance(value, UploadFile):
                    raise ValueError("Допустимы только два уникальных файловых поля: host и rebar")
                if not (value.filename or "").lower().endswith(".json"):
                    raise ValueError("Выберите JSON-файл отчёта")
                content = await value.read(MAX_SNAPSHOT_BYTES + 1)
                total += len(content)
                if total > MAX_SNAPSHOT_BYTES:
                    raise SnapshotTooLargeError("Суммарный размер JSON превышает 32 MiB")
                values[key] = content
            return await run_in_threadpool(inspect_revit_snapshots, **values)
    except SnapshotTooLargeError as error:
        raise HTTPException(413, str(error)) from error
    except (ValueError, ClientDisconnect) as error:
        raise HTTPException(422, str(error) or "Передача JSON прервана") from error
    except StarletteHTTPException as error:
        raise HTTPException(422, "Некорректная multipart-форма: максимум два JSON-файла") from error
