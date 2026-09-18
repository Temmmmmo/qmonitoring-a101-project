from pathlib import Path
from tempfile import TemporaryDirectory

from rebar.web import composite_jobs as module


def test_history_keeps_success_and_failure_and_expires_finished_jobs(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    jobs = module.CompositeJobs()

    def invalid_input(progress):
        raise module.JobInputError(422, "bad scale")

    with TemporaryDirectory() as first, TemporaryDirectory() as second, jobs._executor:
        first_folder = TemporaryDirectory(dir=first)
        second_folder = TemporaryDirectory(dir=second)
        first_id = jobs.start(first_folder, lambda progress: {"report": 1}, case_id="Плита 1")
        jobs._executor.submit(lambda: None).result(timeout=5)
        second_id = jobs.start(second_folder, invalid_input, case_id="Плита 2")
        jobs._executor.submit(lambda: None).result(timeout=5)
        entries = jobs.list_jobs()
        assert [job.id for job in entries] == [second_id, first_id]
        assert [job.status for job in entries] == ["failed", "complete"]
        assert jobs.active_job() is None
        assert jobs.get(second_id).error_detail == "bad scale"
        assert jobs.get(first_id).result_path.is_file()
        entries[0].case_id = "modified snapshot"
        assert jobs.get(second_id).case_id == "Плита 2"
        clock[0] += module.RESULT_TTL_S + 1
        assert jobs.list_jobs() == []
        assert jobs.get(first_id) is None
        assert not Path(first_folder.name).exists() and not Path(second_folder.name).exists()
