"""One active uploaded-plate calculation outside the lifetime of its HTTP request."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
import time
from uuid import uuid4

LOGGER = logging.getLogger(__name__)
RESULT_TTL_S = 3600


class JobInputError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class CompositeJob:
    id: str
    folder: TemporaryDirectory
    status: str = "running"
    result_path: Path | None = None
    error_status: int | None = None
    error_detail: str | None = None
    finished_at: float | None = None


class CompositeJobs:
    def __init__(self):
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="composite-plate")
        self._jobs: dict[str, CompositeJob] = {}
        self._active_id: str | None = None

    def _prune(self):
        now = time.monotonic()
        for key, job in tuple(self._jobs.items()):
            if job.finished_at is not None and now - job.finished_at > RESULT_TTL_S:
                job.folder.cleanup()
                del self._jobs[key]

    def start(self, folder: TemporaryDirectory, calculate) -> str | None:
        """Return an opaque id, or None when another plate is still running."""
        with self._lock:
            self._prune()
            if self._active_id is not None:
                return None
            job = CompositeJob(uuid4().hex, folder)
            self._jobs[job.id] = job
            self._active_id = job.id
            try:
                self._executor.submit(self._run, job, calculate)
            except RuntimeError:
                del self._jobs[job.id]
                self._active_id = None
                raise
            return job.id

    def _run(self, job: CompositeJob, calculate):
        try:
            result = calculate()
            path = Path(job.folder.name) / "result.json"
            with path.open("w", encoding="utf-8") as output:
                json.dump(result, output, ensure_ascii=False, allow_nan=False)
            status, error_status, error_detail = "complete", None, None
        except JobInputError as error:
            path = None
            status, error_status, error_detail = "failed", error.status_code, error.detail
        except Exception:
            LOGGER.exception("Composite plate background job %s failed", job.id)
            path = None
            status, error_status, error_detail = "failed", 500, "Расчёт завершился внутренней ошибкой; сообщите ID задания разработчику"
        with self._lock:
            job.status, job.result_path = status, path
            job.error_status, job.error_detail = error_status, error_detail
            job.finished_at = time.monotonic()
            self._active_id = None

    def get(self, job_id: str) -> CompositeJob | None:
        with self._lock:
            self._prune()
            return self._jobs.get(job_id)


composite_jobs = CompositeJobs()
