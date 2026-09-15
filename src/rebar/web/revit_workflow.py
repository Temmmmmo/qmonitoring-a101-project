"""Bounded single-direction DXF transport for the Revit workflow, no storage."""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore
from typing import Annotated

from ezdxf.lldxf.const import DXFError
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from rebar.application.revit_workflow import MAX_FILE_BYTES, analyze_workflow, parse_request

router = APIRouter()
_CAPACITY = BoundedSemaphore(1)


async def _save(upload, folder, suffix):
    name = Path((upload.filename or "").replace("\\", "/")).name
    if not name or Path(name).suffix.lower() != suffix or len(name) > 240:
        raise ValueError("Expected an actual "+suffix+" filename")
    path = Path(folder)/name
    count = 0
    with path.open("xb") as stream:
        while chunk := await upload.read(1024*1024):
            count += len(chunk)
            if count > MAX_FILE_BYTES:
                raise HTTPException(413, "Source file exceeds 30 MiB")
            stream.write(chunk)
    if not count:
        raise ValueError("Empty source file")
    return path


@router.post("/api/revit/workflow/analyze")
async def analyze_revit_workflow(dxf: Annotated[UploadFile, File()],
    request_json: Annotated[str, Form(max_length=16384)],
    shk: Annotated[UploadFile | None, File()] = None):
    acquired = False
    try:
        raw = request_json.encode("utf-8")
        parse_request(raw)
        acquired = _CAPACITY.acquire(blocking=False)
        if not acquired:
            raise HTTPException(429, "Another Revit workflow calculation is running; try again later")
        with TemporaryDirectory(prefix="rebar-revit-workflow-") as folder:
            dxf_path = await _save(dxf, folder, ".dxf")
            shk_path = await _save(shk, folder, ".shk") if shk is not None else None
            return await run_in_threadpool(analyze_workflow, dxf_path, raw, shk_path=shk_path)
    except (ValueError, KeyError, DXFError) as error:
        raise HTTPException(422, str(error)) from error
    except OSError as error:
        raise HTTPException(400, "Source file could not be read") from error
    finally:
        if acquired:
            _CAPACITY.release()
        await dxf.close()
        if shk is not None:
            await shk.close()
