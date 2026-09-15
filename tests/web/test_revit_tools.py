"""Public Revit installation bundles, HTTP allowlist and installed-resource path."""
from copy import deepcopy
import ast
import hashlib
from io import BytesIO
import importlib
import json
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

from fastapi.testclient import TestClient
import pytest

from rebar.application import revit_installation as installation

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "integrations/pyrevit"
web_app = importlib.import_module("rebar.web.app")
client = TestClient(web_app.app)


def test_revit_page_and_navigation_are_available_without_private_results():
    response = client.get("/revit")
    assert response.status_code == 200
    assert "Подключение Revit" in response.text
    assert "родительскую папку" in response.text
    assert "не является пакетом для Plan Preview" in response.text
    assert "Source Workflow 8.1" in response.text
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert 'href="/revit"' in client.get("/").text
    assert 'href="/revit"' in client.get("/composite").text
    for name in ("revit.js", "revit.css"):
        static = client.get("/static/" + name)
        assert static.status_code == 200
        assert static.headers["cache-control"] == "no-store, max-age=0"
    javascript = client.get("/static/revit.js").text
    assert "textContent" in javascript and "innerHTML" not in javascript
    assert "encodeURIComponent(tool.version)" in javascript


def test_catalog_describes_four_data_free_nonstructural_tools_and_missing_features():
    response = client.get("/api/revit/tools")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    result = response.json()
    assert result["schema_version"] == installation.CATALOG_SCHEMA
    assert result["placement_eligible"] is result["engineering_approval"] is False
    assert result["workflow"]["new_forms_structural_export"] == "not_available"
    assert result["workflow"]["snapshot_upload_inspection"] == "available"
    assert {row["id"] for row in result["tools"]} == {
        "working-host-probe", "working-rebar-probe", "plan-preview", "source-workflow-81"}
    for row in result["tools"]:
        assert row["capabilities"]["creates_structural_rebar"] is False
        assert row["capabilities"]["saves_or_syncs_model"] is False
        assert row["capabilities"]["includes_project_data"] is False
        assert row["extension_directory"] != "QMonitoring.extension"
        assert row["version"].startswith(row["runtime_version"] + "-")
        assert row["compatibility"]["revit_major_versions"] == ["2024"]


def test_source_graphics_download_has_matching_new_runtime_and_complete_helper():
    row = next(t for t in client.get("/api/revit/tools").json()["tools"] if t["id"] == "plan-preview")
    assert row["runtime_version"] == "0.2.3"
    assert "source-isofields-zones/v1" in row["accepted_input_schemas"]
    assert "graphic-bar-plan-draft/v1" in row["accepted_input_schemas"]
    assert "graphic-bar-plan-pruned/v1" in row["accepted_input_schemas"]
    assert "первый запуск в настоящем Revit" in row["verification"]
    with ZipFile(BytesIO(client.get(row["download_url"]).content)) as archive:
        helper = archive.read("QMonitoringPreview.extension/lib/qm_revit_source_preview.py")
        assert b"source-isofields-zones/v1" in helper
        assert b"readback_source_views" in helper
        assert b"previously_covered_geometry_lost" in archive.read("QMonitoringPreview.extension/lib/qm_revit_pruned_preview.py")
    assert "Скачать изополя + исходные зоны" in client.get("/revit").text


@pytest.mark.parametrize("tool", installation.TOOLS, ids=lambda tool: tool["id"])
def test_download_is_attachment_exact_hash_deterministic_whitelist_and_dependency_complete(tool):
    metadata, built = installation.build_tool_bundle(tool, SOURCE)
    assert installation.build_tool_bundle(tool, SOURCE) == (metadata, built)
    response = client.get(metadata["download_url"])
    assert response.status_code == 200 and response.content == built
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == f'attachment; filename="{metadata["filename"]}"'
    assert response.headers["x-content-sha256"] == hashlib.sha256(built).hexdigest()
    assert response.headers["etag"] == '"' + metadata["sha256"] + '"'
    assert response.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert response.headers["x-content-type-options"] == "nosniff"
    with ZipFile(BytesIO(built)) as archive:
        names = archive.namelist()
        expected = {name for name, _ in installation._entries(tool, SOURCE)} | {"manifest.json"}
        assert set(names) == expected and len(names) == len(expected)
        assert len([name for name in names if name.endswith(".pushbutton/script.py")]) == 1
        assert not any(name.endswith((".rvt", ".dxf", ".pdf", ".shk")) for name in names)
        assert set(name for name in names if name.endswith(".json")) == {"manifest.json"}
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["bundle_version"] == metadata["version"]
        for row in manifest["files"]:
            assert hashlib.sha256(archive.read(row["path"])).hexdigest() == row["sha256"]
            assert len(archive.read(row["path"])) == row["bytes"]
        modules = set(tool["modules"])
        for name in names:
            if not name.endswith(".py"):
                continue
            tree = ast.parse(archive.read(name).decode("utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("qm_"):
                    assert node.module + ".py" in modules, f"Missing runtime dependency: {node.module}"
        readme = archive.read("README.md").decode("utf-8")
        assert tool["extension"] + ".extension" in readme and "Старые QMonitoring-вкладки останутся" in readme
        assert "рабочей плиты 11020633" not in readme


@pytest.mark.parametrize("url", (
    "/api/revit/tools/unknown/0.1.0/download",
    "/api/revit/tools/working-host-probe/unknown/download",
    "/api/revit/tools/..%2F..%2Fsettings/0/download",
    "/api/revit/tools/working-rebar-probe/..%2F..%2Fsecret/download",
    "/api/revit/tools/working-host-probe/catalog.json/download",
))
def test_unknown_version_or_path_traversal_is_404(url):
    response = client.get(url)
    assert response.status_code == 404
    assert "attachment" not in response.headers.get("content-disposition", "")


def test_changed_source_produces_new_content_addressed_version(tmp_path):
    tool = installation.TOOLS[1]
    first, _ = installation.build_tool_bundle(tool, SOURCE)
    for name in tool["modules"]:
        path = tmp_path / installation.SOURCE_EXTENSION / "lib" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((SOURCE / installation.SOURCE_EXTENSION / "lib" / name).read_bytes())
    for name in ("script.py", "bundle.yaml"):
        relative = f"{installation.SOURCE_EXTENSION}/{installation.SOURCE_TAB}/Diagnostics.panel/{tool['button']}.pushbutton/{name}"
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((SOURCE / relative).read_bytes())
    changed = tmp_path / installation.SOURCE_EXTENSION / "lib" / tool["runtime_module"]
    changed.write_bytes(changed.read_bytes() + b"\n# changed public source\n")
    second, _ = installation.build_tool_bundle(tool, tmp_path)
    assert first["version"] != second["version"] and first["download_url"] != second["download_url"]


def test_installed_resources_serve_without_checkout_or_writes(tmp_path, monkeypatch):
    root = tmp_path / "web"
    outputs = installation.build_distributable_bundles(SOURCE, root / "revit_bundles")
    assert len(outputs) == 5
    monkeypatch.setattr(installation.resources, "files", lambda package: root)
    monkeypatch.setattr(installation, "_development_checkout", lambda: pytest.fail("Installed package cannot need repository"))
    monkeypatch.setattr(Path, "write_bytes", lambda *args: pytest.fail("Runtime must never write"))
    monkeypatch.setattr(Path, "mkdir", lambda *args, **kwargs: pytest.fail("Runtime must never create a directory"))
    catalog = installation.revit_tool_catalog()
    for row in catalog["tools"]:
        metadata, content = installation.revit_tool_download(row["id"], row["version"])
        assert metadata == row and len(content) == row["bytes"]


def test_missing_installed_resources_fail_closed_with_clear_503(tmp_path, monkeypatch):
    monkeypatch.setattr(installation.resources, "files", lambda package: tmp_path)
    monkeypatch.setattr(installation, "_development_checkout", lambda: None)
    response = client.get("/api/revit/tools")
    assert response.status_code == 503 and "отсутствуют" in response.json()["detail"]
    assert response.headers["cache-control"] == "no-store, max-age=0"


def test_corrupted_download_is_not_served_even_if_catalog_is_present(tmp_path, monkeypatch):
    root = tmp_path / "revit_bundles"
    installation.build_distributable_bundles(SOURCE, root)
    catalog = json.loads((root / "catalog.json").read_bytes())
    row = catalog["tools"][0]
    (root / row["filename"]).write_bytes(b"not the original public zip")
    monkeypatch.setattr(installation.resources, "files", lambda package: tmp_path)
    response = client.get(row["download_url"])
    assert response.status_code == 503 and "Контрольная сумма" in response.json()["detail"]
    assert "attachment" not in response.headers.get("content-disposition", "")
    assert client.get("/api/revit/tools").status_code == 503


@pytest.mark.parametrize("change", ("schema", "permission", "missing-tool", "unknown-tool", "filename", "structural"))
def test_invalid_packaged_manifest_cannot_expand_whitelist(change):
    catalog, _ = installation.build_tool_catalog(SOURCE)
    catalog = deepcopy(catalog)
    if change == "schema":
        catalog["schema_version"] = "untrusted"
    elif change == "permission":
        catalog["placement_eligible"] = True
    elif change == "missing-tool":
        catalog["tools"].pop()
    elif change == "unknown-tool":
        catalog["tools"][0]["id"] = "structural-create"
    elif change == "filename":
        catalog["tools"][0]["filename"] = "../private.zip"
    else:
        catalog["tools"][0]["capabilities"]["creates_structural_rebar"] = True
    with pytest.raises(installation.RevitBundleUnavailableError):
        installation._checked_catalog(catalog)


def test_distribution_builder_is_stdlib_only_without_importing_rebar_or_solver(tmp_path):
    code = """import runpy, sys
from pathlib import Path
helpers = runpy.run_path(sys.argv[1])
files = helpers['build_distributable_bundles'](Path(sys.argv[2]), Path(sys.argv[3]))
assert len(files) == 5
assert not any(name == 'rebar' or name.startswith('rebar.') or name.startswith('scipy') for name in sys.modules)
"""
    subprocess.run([sys.executable, "-I", "-S", "-c", code, str(Path(installation.__file__)),
        str(SOURCE), str(tmp_path / "generated")], check=True, capture_output=True, text=True)


def test_sdist_manifest_contains_every_exact_public_dependency_without_private_globs():
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    for tool in installation.TOOLS:
        for name in tool["modules"]:
            assert "include integrations/pyrevit/QMonitoring.extension/lib/" + name in manifest
        for name in ("script.py", "bundle.yaml"):
            relative = f"integrations/pyrevit/QMonitoring.extension/QMonitoring.tab/{tool.get('panel', 'Diagnostics')}.panel/{tool['button']}.pushbutton/{name}"
            assert "include " + relative in manifest
    assert "recursive-include" not in manifest and "artifacts" not in manifest


def test_dockerfile_copies_every_catalog_button_without_excluding_workflow():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "integrations" not in ignored and "*.pushbutton" not in ignored
    for tool in installation.TOOLS:
        panel = tool.get("panel", "Diagnostics")
        source = (f"integrations/pyrevit/QMonitoring.extension/QMonitoring.tab/"
            f"{panel}.panel/{tool['button']}.pushbutton")
        assert f"COPY {source} ./{source}" in dockerfile


def test_catalog_runtime_versions_match_packaged_native_modules():
    for tool in installation.TOOLS:
        tree = ast.parse((SOURCE / installation.SOURCE_EXTENSION / "lib" / tool["runtime_module"]).read_text(encoding="utf-8"))
        versions = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets)]
        assert versions == [tool["runtime_version"]]
