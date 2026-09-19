"""Private source delivery/one-click contract; miniature bytes test transport, not engineering."""
import hashlib
import json
from pathlib import Path
import runpy
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from rebar.application import engineering_example as example
from rebar.application import s1_example
from rebar.web import engineering_examples as endpoint
from rebar.web.app import app

client = TestClient(app)


@pytest.fixture
def original_transport(tmp_path, monkeypatch):
    contents = {name: ("transport-test-only:" + name).encode() for _, _, name, _ in example.SOURCES}
    monkeypatch.setattr(example, "SOURCES", tuple(
        (layer, axis, name, hashlib.sha256(contents[name]).hexdigest())
        for layer, axis, name, _ in example.SOURCES))
    archive = tmp_path / "originals.zip"
    with ZipFile(archive, "w") as bundle:
        for name, content in contents.items():
            bundle.writestr(name, content)
    root = tmp_path / "data"
    monkeypatch.setenv(example.ENV_NAME, str(root))
    return archive, root, contents


def test_original_archive_preserves_bytes_names_and_can_be_reused(original_transport):
    archive, root, contents = original_transport
    example.install_original_archive(archive, root)
    example.install_original_archive(archive, root)
    for name, data in contents.items():
        assert (root / example.EXAMPLE_ID / name).read_bytes() == data
    response = client.get("/api/engineering-examples")
    assert response.status_code == 200
    metadata = response.json()["examples"][0]
    assert metadata["is_available"] and metadata["source_kind"] == "real_engineering_files"
    assert metadata["reference"]["physical_bar_count"] == 1227
    assert metadata["reference"]["position_count"] == 74
    assert not metadata["profile"]["engineering_approval"]
    assert all(s["mapping_id"] == example.MAPPING_ID and s["shk_filename"] is None for s in metadata["sources"])
    assert str(root) not in response.text


def test_catalog_prefers_installed_k09_but_explicit_unavailable_s1_is_not_substituted(original_transport):
    archive, root, _ = original_transport
    example.install_original_archive(archive, root)
    catalog = client.get("/api/engineering-examples").json()
    assert catalog["default_example_id"] == example.EXAMPLE_ID
    assert catalog["examples"][0]["id"] == example.EXAMPLE_ID
    assert catalog["examples"][0]["is_available"] is True
    assert catalog["examples"][1]["id"] == s1_example.EXAMPLE_ID
    assert catalog["examples"][1]["is_available"] is False
    assert catalog["examples"][1]["reference"]["mass_kg"] is None
    assert client.post(f"/api/engineering-examples/{s1_example.EXAMPLE_ID}/analyze").status_code == 503


def test_s1_installer_uses_separate_whitelist_and_sha_without_touching_k09(original_transport,
                                                                           monkeypatch, tmp_path):
    k09_archive, root, k09_contents = original_transport
    example.install_original_archive(k09_archive, root)
    contents = {name: ("s1-transport-only:" + name).encode() for _, _, name, _ in s1_example.SOURCES}
    monkeypatch.setattr(s1_example, "SOURCES", tuple(
        (layer, axis, name, hashlib.sha256(contents[name]).hexdigest())
        for layer, axis, name, _ in s1_example.SOURCES))
    archive = tmp_path / "s1.zip"
    with ZipFile(archive, "w") as bundle:
        for name, data in contents.items():
            bundle.writestr(name, data)
    example.install_original_archive(archive, root, example_id=s1_example.EXAMPLE_ID)
    assert all((root / s1_example.EXAMPLE_ID / name).read_bytes() == data for name, data in contents.items())
    assert all((root / example.EXAMPLE_ID / name).read_bytes() == data for name, data in k09_contents.items())
    catalog = client.get("/api/engineering-examples").json()
    assert catalog["examples"][1]["is_available"] is True
    assert catalog["examples"][1]["source_kind"] == "real_engineering_files"
    with ZipFile(archive, "w") as bundle:
        for name, data in list(contents.items())[:-1]:
            bundle.writestr(name, data)
        bundle.writestr("unapproved.dxf", b"wrong")
    with pytest.raises(ValueError):
        example.install_original_archive(archive, root, example_id=s1_example.EXAMPLE_ID)
    assert all((root / s1_example.EXAMPLE_ID / name).read_bytes() == data for name, data in contents.items())


@pytest.mark.parametrize("kind", ["missing", "corrupt", "extra", "traversal", "duplicate"])
def test_bad_archive_writes_nothing(original_transport, kind):
    archive, root, contents = original_transport
    pairs = list(contents.items())
    if kind == "missing":
        pairs.pop()
    elif kind == "corrupt":
        pairs[0] = (pairs[0][0], b"different-file")
    elif kind == "extra":
        pairs.append(("extra.dxf", b"x"))
    elif kind == "traversal":
        pairs[0] = ("../outside", b"x")
    else:
        pairs[-1] = pairs[0]
    with ZipFile(archive, "w") as bundle:
        for name, data in pairs:
            bundle.writestr(name, data)
    with pytest.raises(ValueError):
        example.install_original_archive(archive, root)
    assert not root.exists()


def test_installer_does_not_overwrite_different_existing_original(original_transport):
    archive, root, contents = original_transport
    target = root / example.EXAMPLE_ID / next(iter(contents))
    target.parent.mkdir(parents=True)
    target.write_bytes(b"user-file")
    with pytest.raises(example.EngineeringFilesUnavailableError):
        example.install_original_archive(archive, root)
    assert target.read_bytes() == b"user-file"
    assert list(target.parent.iterdir()) == [target]


def test_missing_configuration_does_not_run_a_synthetic_fallback(monkeypatch):
    monkeypatch.delenv(example.ENV_NAME, raising=False)
    entry = client.get("/api/engineering-examples").json()["examples"][0]
    assert not entry["is_available"]
    response = client.post(f"/api/engineering-examples/{example.EXAMPLE_ID}/analyze")
    assert response.status_code == 503
    assert "front" not in response.json()
    assert client.post("/api/engineering-examples/unknown/analyze").status_code == 404


def test_unavailable_and_corrupt_originals_fail_closed(original_transport):
    archive, root, contents = original_transport
    assert not example.engineering_example_catalog()["examples"][0]["is_available"]
    example.install_original_archive(archive, root)
    target = root / example.EXAMPLE_ID / next(iter(contents))
    target.chmod(0o644)
    target.write_bytes(b"corrupted after installation")
    assert not example.engineering_example_catalog()["examples"][0]["is_available"]
    assert client.post(f"/api/engineering-examples/{example.EXAMPLE_ID}/analyze").status_code == 503


def test_exact_verified_originals_feed_existing_core_and_are_cleaned_up(original_transport, monkeypatch):
    archive, root, contents = original_transport
    example.install_original_archive(archive, root)
    seen = []

    def inspect(sources, **kwargs):
        assert len(sources) == 4
        for source in sources:
            path = Path(source.dxf_path)
            seen.append(path)
            assert path.read_bytes() == contents[path.name]
            assert source.shk_path is None and source.mapping_id == example.MAPPING_ID
        assert kwargs["case_id"] == example.EXAMPLE_ID
        assert kwargs["maximum_source_bars"] == 1227
        return SimpleNamespace(problem="typed-problem", solution="typed-solution",
            provenance={"algorithm": "genetic-source-recovery", "config": {}, "candidate_id": "test", "selection": {}})

    def recover(problem, solution, settings, **kwargs):
        assert (problem, solution) == ("typed-problem", "typed-solution")
        assert all(s.first_300_offset_mm == 100 and "not approved" in s.source for s in settings)
        assert kwargs["normalization_config"].allow_diameter_increase
        assert str(root) not in str(kwargs["source_provenance"])
        return SimpleNamespace(status='transport-only-test')

    monkeypatch.setattr(example, "analyze_assistant_sources", inspect)
    monkeypatch.setattr(example, "recover_physical_layout", recover)
    monkeypatch.setattr(example, "k09_dxf_outer_web_report", lambda *args: {
        "front": [{"additional_mass_kg": 0, "physical_bar_count": 0, "position_count": 0,
                   "direction_candidate_indexes": [0]*4}], "selected_index": 0,
        "directions": [{"candidates": [{"physical_bars": []}]} for _ in range(4)], "placement_eligible": False})
    response = client.post(f"/api/engineering-examples/{example.EXAMPLE_ID}/analyze")
    assert response.status_code == 200, response.text
    assert not response.json()["placement_eligible"]
    assert response.json()["engineering_example"]["id"] == example.EXAMPLE_ID
    assert all(not path.exists() for path in seen)
    assert all((root / example.EXAMPLE_ID / name).exists() for name in contents)


def test_ready_k09_zone_merge_uses_shared_composite_analysis_and_cleans_snapshot(original_transport, monkeypatch):
    archive, root, contents = original_transport
    example.install_original_archive(archive, root)
    seen = []

    def analyze(sources, settings, **kwargs):
        assert len(sources) == len(settings) == 4
        for source in sources:
            path = Path(source.dxf_path)
            seen.append(path)
            assert path.read_bytes() == contents[path.name]
            assert source.mapping_id == example.MAPPING_ID
        assert kwargs == {
            "maximum_zones_per_direction": 128,
            "solver_time_limit_s": 20,
            "cutting_profile": "plate-11700",
            "case_id": example.EXAMPLE_ID,
            "search_mode": "zone-merge",
            "progress_callback": "progress",
        }
        return {"search_mode": "zone-merge", "front": [{"zone_count": 4}]}

    monkeypatch.setattr(example, "analyze_composite_plate", analyze)
    report = example.analyze_engineering_example(
        example.EXAMPLE_ID, search_mode="zone-merge", progress_callback="progress")
    assert report["front"] == [{"zone_count": 4}]
    assert report["engineering_example"]["id"] == example.EXAMPLE_ID
    assert report["engineering_example"]["profile"]["cutting_profile"] == "plate-11700"
    assert report["engineering_example"]["profile"]["id"] == "k09-zone-merge-comparison/v1"
    assert "Высоты осей" in report["engineering_example"]["profile"]["note"]
    assert seen and all(not path.exists() for path in seen)


def test_ready_s1_zone_merge_uses_its_mapping_and_shared_composite_analysis(monkeypatch):
    originals = tuple(("s1:" + name).encode() for _, _, name, _ in s1_example.SOURCES)
    seen = []

    def analyze(sources, settings, **kwargs):
        assert len(sources) == len(settings) == 4
        for source, content in zip(sources, originals):
            path = Path(source.dxf_path)
            seen.append(path)
            assert path.read_bytes() == content
            assert source.mapping_id == s1_example.LEGACY_S1_D18.id
        assert kwargs["search_mode"] == "zone-merge"
        assert kwargs["case_id"] == s1_example.EXAMPLE_ID
        assert kwargs["progress_callback"] == "progress"
        return {"search_mode": "zone-merge", "front": [{"zone_count": 8}]}

    monkeypatch.setattr(s1_example, "source_bytes", lambda: originals)
    monkeypatch.setattr(s1_example, "analyze_composite_plate", analyze)
    report = s1_example.analyze_s1_example(search_mode="zone-merge", progress_callback="progress")
    assert report["front"] == [{"zone_count": 8}]
    assert report["engineering_example"]["id"] == s1_example.EXAMPLE_ID
    assert report["engineering_example"]["profile"]["cutting_profile"] == "plate-11700"
    assert report["engineering_example"]["profile"]["id"] == "s1-zone-merge-comparison/v1"
    assert "Высоты осей" in report["engineering_example"]["profile"]["note"]
    assert seen and all(not path.exists() for path in seen)


def test_ready_zone_merge_endpoint_starts_shared_background_job(monkeypatch):
    captured = {}

    class Job:
        def summary(self):
            return {"job_id": "ready-zone", "case_id": example.EXAMPLE_ID, "status": "running",
                    "progress_percent": 0, "progress_stage": "Подготовка входных файлов", "created_at": 1}

    def start(folder, calculate, *, case_id):
        captured["folder"] = folder
        captured["case_id"] = case_id
        captured["result"] = calculate(lambda percent, stage: captured.setdefault("progress", (percent, stage)))
        return "ready-zone"

    monkeypatch.setattr(endpoint.composite_jobs, "start", start)
    monkeypatch.setattr(endpoint.composite_jobs, "get", lambda job_id: Job())
    monkeypatch.setattr(endpoint, "_analyze", lambda example_id, **kwargs: {
        "example_id": example_id, "search_mode": kwargs["search_mode"],
        "has_progress": callable(kwargs["progress_callback"]),
    })
    response = client.post(
        f"/api/engineering-examples/{example.EXAMPLE_ID}/analyze?search_mode=zone-merge")
    assert response.status_code == 202
    assert response.json()["job_id"] == "ready-zone"
    assert captured["case_id"] == example.EXAMPLE_ID
    assert captured["result"] == {
        "example_id": example.EXAMPLE_ID, "search_mode": "zone-merge", "has_progress": True}
    captured["folder"].cleanup()
    assert client.post("/api/engineering-examples/unknown/analyze?search_mode=zone-merge").status_code == 404
    assert client.post(
        f"/api/engineering-examples/{example.EXAMPLE_ID}/analyze?search_mode=typo").status_code == 422


def test_ready_zone_merge_busy_job_returns_active_id_and_cleans_unused_folder(monkeypatch):
    folders = []

    class Folder:
        def __init__(self, **kwargs):
            self.cleaned = False
            folders.append(self)

        def cleanup(self):
            self.cleaned = True

    monkeypatch.setattr(endpoint, "TemporaryDirectory", Folder)
    monkeypatch.setattr(endpoint.composite_jobs, "start", lambda *args, **kwargs: None)
    monkeypatch.setattr(endpoint.composite_jobs, "active_job", lambda: SimpleNamespace(id="active-ready"))
    response = client.post(
        f"/api/engineering-examples/{example.EXAMPLE_ID}/analyze?search_mode=zone-merge")
    assert response.status_code == 409
    assert response.json() == {"detail": "Другой расчёт плиты уже выполняется",
                               "active_job_id": "active-ready"}
    assert len(folders) == 1 and folders[0].cleaned


@pytest.mark.parametrize(("error", "status"), [
    (ValueError("неверный профиль"), 422),
    (example.EngineeringFilesUnavailableError("нет исходного комплекта"), 503),
])
def test_ready_zone_merge_job_preserves_input_error_status_and_releases_lock(monkeypatch, error, status):
    captured = {}

    class Job:
        def summary(self):
            return {"job_id": "failed-ready", "case_id": example.EXAMPLE_ID, "status": "running",
                    "progress_percent": 0, "progress_stage": "Подготовка входных файлов", "created_at": 1}

    def start(folder, calculate, *, case_id):
        try:
            calculate(lambda *_: None)
        except endpoint.JobInputError as job_error:
            captured["error"] = job_error
        finally:
            folder.cleanup()
        return "failed-ready"

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(endpoint, "analyze_engineering_example", fail)
    monkeypatch.setattr(endpoint.composite_jobs, "start", start)
    monkeypatch.setattr(endpoint.composite_jobs, "get", lambda job_id: Job())
    response = client.post(
        f"/api/engineering-examples/{example.EXAMPLE_ID}/analyze?search_mode=zone-merge")
    assert response.status_code == 202
    assert captured["error"].status_code == status
    assert captured["error"].detail == str(error)
    assert not endpoint._calculation_lock.locked()


def test_concurrent_requests_are_bounded_and_lock_recovers(monkeypatch):
    with endpoint._calculation_lock:
        response = client.post(f"/api/engineering-examples/{example.EXAMPLE_ID}/analyze")
        assert response.status_code == 409
    monkeypatch.delenv(example.ENV_NAME, raising=False)
    assert client.post(f"/api/engineering-examples/{example.EXAMPLE_ID}/analyze").status_code == 503
    assert not endpoint._calculation_lock.locked()


@pytest.fixture
def working_host_bytes():
    fixture = runpy.run_path(str(Path(__file__).parents[1]/"application/test_working_solid_host.py"))
    return json.dumps(fixture["stepped_snapshot"]()).encode()


def test_boundary_trim_transport_requires_actual_host_and_exact_identity_confirmation(monkeypatch,working_host_bytes):
    calls = []
    monkeypatch.setattr(endpoint,"_analyze",lambda example_id,**kwargs: calls.append((example_id,kwargs)) or
                        {"transport_test_only":True,"placement_eligible":False})
    response = client.post(f"/api/engineering-examples/{example.EXAMPLE_ID}/boundary-trim",
        files={"working_host":("host.json",working_host_bytes,"application/json")},data={"host_xy_confirmed":"true"})
    assert response.status_code == 200,response.text
    assert calls == [(example.EXAMPLE_ID,{"working_host_bytes":working_host_bytes,"confirm_identity_xy":True,
                                         "outer_only_repair":True})]
    assert not response.json()["placement_eligible"]


@pytest.mark.parametrize("kind",["missing_confirm","false_confirm","wrong_confirm","bad_host","wrong_extension",
    "extra_field","duplicate_file","missing_host","old_reference_without_solid"])
def test_boundary_trim_upload_rejects_ambiguous_or_invalid_inputs_before_calculation(monkeypatch,working_host_bytes,kind):
    calls = []
    monkeypatch.setattr(endpoint,"_analyze",lambda *args,**kwargs: calls.append(True))
    files = [("working_host",("host.json",working_host_bytes,"application/json"))]
    data = {"host_xy_confirmed":"true"}
    if kind == "missing_confirm":
        data = {}
    elif kind == "false_confirm":
        data["host_xy_confirmed"] = "false"
    elif kind == "wrong_confirm":
        data["host_xy_confirmed"] = "1"
    elif kind == "bad_host":
        files = [("working_host",("host.json",b'{"units":"mm","units":"m"}',"application/json"))]
    elif kind == "wrong_extension":
        files = [("working_host",("host.txt",working_host_bytes,"text/plain"))]
    elif kind == "extra_field":
        data["offset_x_mm"] = "100"
    elif kind == "duplicate_file":
        files *= 2
    elif kind == "missing_host":
        files = [("wrong",("host.json",working_host_bytes,"application/json"))]
    else:
        files = [("working_host",("host.json",b'{"schema_version":"revit-reference-probe/v1"}',"application/json"))]
    response = client.post(f"/api/engineering-examples/{example.EXAMPLE_ID}/boundary-trim",files=files,data=data)
    assert response.status_code == 422,response.text
    assert not calls


@pytest.mark.parametrize("content_length,status",[(str(33*1024*1024),413),("-1",422),("wat",422)])
def test_boundary_trim_wire_limit_is_checked_before_multipart_parse(content_length,status):
    response = client.post(f"/api/engineering-examples/{example.EXAMPLE_ID}/boundary-trim",content=b"not a multipart",
        headers={"Content-Type":"multipart/form-data; boundary=xyz","Content-Length":content_length})
    assert response.status_code == status


def test_boundary_trim_missing_originals_and_concurrency_remain_fail_closed(monkeypatch,working_host_bytes):
    args = {"files":{"working_host":("host.json",working_host_bytes,"application/json")},
            "data":{"host_xy_confirmed":"true"}}
    url = f"/api/engineering-examples/{example.EXAMPLE_ID}/boundary-trim"
    with endpoint._calculation_lock:
        assert client.post(url,**args).status_code == 409
    monkeypatch.delenv(example.ENV_NAME,raising=False)
    assert client.post(url,**args).status_code == 503
    assert not endpoint._calculation_lock.locked()
