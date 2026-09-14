"""Private source delivery/one-click contract; miniature bytes test transport, not engineering."""
import hashlib
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from rebar.application import engineering_example as example
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
        return "recovery"

    monkeypatch.setattr(example, "analyze_assistant_sources", inspect)
    monkeypatch.setattr(example, "recover_physical_layout", recover)
    monkeypatch.setattr(example, "physical_web_report", lambda *args: {"front": [], "placement_eligible": False})
    response = client.post(f"/api/engineering-examples/{example.EXAMPLE_ID}/analyze")
    assert response.status_code == 200, response.text
    assert not response.json()["placement_eligible"]
    assert response.json()["engineering_example"]["id"] == example.EXAMPLE_ID
    assert all(not path.exists() for path in seen)
    assert all((root / example.EXAMPLE_ID / name).exists() for name in contents)


def test_concurrent_requests_are_bounded_and_lock_recovers(monkeypatch):
    with endpoint._calculation_lock:
        response = client.post(f"/api/engineering-examples/{example.EXAMPLE_ID}/analyze")
        assert response.status_code == 409
    monkeypatch.delenv(example.ENV_NAME, raising=False)
    assert client.post(f"/api/engineering-examples/{example.EXAMPLE_ID}/analyze").status_code == 503
    assert not endpoint._calculation_lock.locked()
