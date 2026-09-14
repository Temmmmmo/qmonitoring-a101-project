# -*- coding: utf-8 -*-
"""Run the real probe serializer/file writer in Python 2.7 or 3, without Revit.

Example: ipy verify_revit_probe_runtime.py --lib-dir path/to/QMonitoring.extension/lib
No dependencies. Temporary synthetic files only; no model or network access.
"""
from __future__ import print_function, unicode_literals

import argparse
import copy
import json
import math
import os
import platform
import shutil
import sys
import tempfile


def check_cad_comparison():
    """Exercise bounded mesh matching in IronPython, without Autodesk API or private data."""
    from qm_cad_diagnostics import INSTANCE_METHOD, SYMBOL_METHOD, compare_geometry_reads

    triangle = [[0., 0., 0.], [100., 0., 0.], [0., 100., 0.]]
    # Unknown layers deliberately stay unknown even when both readings match.
    first = {"method": SYMBOL_METHOD, "mesh_read_complete": True, "units": "mm",
             "coordinate_system": "revit-internal-origin-and-axes", "triangles_mm": [],
             "meshes": [{"layer": None, "triangles_mm": [triangle, copy.deepcopy(triangle)]}]}
    second = copy.deepcopy(first)
    second["method"] = INSTANCE_METHOD
    second["meshes"][0]["triangles_mm"][0].reverse()
    matched = compare_geometry_reads(first, second)
    assert matched["status"] == "matches" and matched["matched_triangle_count"] == 2
    assert not matched["placement_eligible"] and not matched["layer_identity_verified"]
    second["meshes"][0]["triangles_mm"][0][0][2] += 10
    assert compare_geometry_reads(first, second)["status"] == "differs"
    second["mesh_read_complete"] = False
    assert compare_geometry_reads(first, second)["status"] == "not_checked"
    second["mesh_read_complete"] = True
    second["meshes"][0]["triangles_mm"][0][0][2] = float("nan")
    assert compare_geometry_reads(first, second)["status"] == "not_checked"
    print("PASS: CAD triangle winding, duplicates, mismatch, incomplete/NaN rejection; layers remain unverified")


def check_trial_geometry(lib_dir):
    """Exercise new pure geometry on the real Python engine, without mocking Autodesk."""
    from qm_trial_geometry import compare_trial, make_trial_plan, validate_prism

    extension = os.path.dirname(os.path.abspath(lib_dir))
    # Explicit delivery list: this Linux/.NET bridge can misclassify dirs in os.walk.
    paths = ["lib/" + name + ".py" for name in (
        "qm_probe_geometry", "qm_revit_probe", "qm_trial_geometry", "qm_revit_trial", "qm_trial_input", "qm_core_trial",
        "qm_revit_cad", "qm_cad_diagnostics")]
    paths += ["QMonitoring.tab/Diagnostics.panel/" + name + ".pushbutton/script.py"
              for name in ("ReferenceProbe", "CreationTrial", "JsonTrial", "CoreTrial", "CadProbe")]
    for relative in paths:
        path = os.path.join(extension, relative)
        with open(path, "rb") as source:
            compile(source.read(), path, "exec")

    def line(a, b):
        return {"kind": "Line", "start_mm": list(a), "end_mm": list(b),
                "length_mm": math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))}

    lo, hi = [0, 0, -300], [23600, 14000, 0]
    faces = []
    for axis in range(3):
        other = [i for i in range(3) if i != axis]
        for side in (0, 1):
            points = []
            for u, v in ((0, 0), (1, 0), (1, 1), (0, 1)):
                p = list(lo)
                p[axis] = (lo, hi)[side][axis]
                p[other[0]], p[other[1]] = (lo, hi)[u][other[0]], (lo, hi)[v][other[1]]
                points.append(p)
            normal = [0, 0, 0]
            normal[axis] = 1 if side else -1
            faces.append({"plane": {"origin_mm": points[0], "normal": normal}, "edge_loops": [
                [line(a, b) for a, b in zip(points, points[1:] + points[:1])]]})
    floor = {"element_id": 407801, "bbox_mm": {"min_mm": lo, "max_mm": hi},
             "top_faces": [faces[5]], "bottom_faces": [faces[4]],
             "covers": {side: {"distance_mm": 25} for side in ("top", "bottom", "other")}}
    solid = {"faces": faces, "volume_mm3": 23600 * 14000 * 300}
    assert validate_prism(floor, solid)["status"] == "passed"
    bar_type = {"element_id": 165163, "nominal_diameter_mm": 25, "model_diameter_mm": 25}
    plan = make_trial_plan(floor, bar_type)
    assert plan["axes"][0]["start_mm"] == [1000, 1012.5, -37.5]
    assert plan["axes"][-1]["end_mm"] == [4900, 1787.5, -37.5]
    readback = {"host_id": 407801, "quantity": 9, "number_of_bar_positions": 9,
                "layout_rule": "NumberWithSpacing", "bar_type": bar_type, "hook_type_ids": [-1, -1],
                "bars": [{"curves": [line(a["start_mm"], a["end_mm"])]} for a in plan["axes"]]}
    assert compare_trial(plan, readback)["status"] == "matches"
    readback["bars"][0]["curves"][0]["start_mm"][0] += 10
    assert compare_trial(plan, readback)["status"] == "differs"
    solid["faces"][0]["edge_loops"] *= 2
    try:
        validate_prism(floor, solid)
        raise AssertionError("An inner loop was accepted")
    except ValueError:
        pass
    print("PASS: 6 trial checks (compile, prism, plan, match, mismatch, opening); no Revit API exercised")
    from qm_trial_input import _reject_constant, _unique_object, load_trial_input, validate_trial_input

    sample = load_trial_input(os.path.join(os.path.dirname(extension), "samples", "single-zone-trial.json"))
    plan = make_trial_plan(floor, bar_type, sample)
    assert plan["axes"][-1]["end_mm"] == [4900, 1787.5, -37.5]
    sample["zone"].update({"bar_count": 5, "spacing_mm": 100, "length_mm": 2500})
    assert make_trial_plan(floor, bar_type, sample)["axes"][-1]["end_mm"] == [3500, 1412.5, -37.5]
    for bad in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'):
        try:
            json.loads(bad, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            raise AssertionError("Invalid JSON accepted")
        except ValueError:
            pass
    sample["zone"]["bar_count"] = True
    try:
        validate_trial_input(sample)
        raise AssertionError("Boolean accepted as bar count")
    except ValueError:
        pass
    print("PASS: 6 JSON checks (sample, parameters, duplicate, NaN, Infinity, boolean); 13 Python files compiled")
    from qm_core_trial import compare_core_trial, load_core_input, make_core_plan

    data = load_core_input(os.path.join(os.path.dirname(extension), "samples", "core-axis-trial.json"))
    type18 = {"element_id": 165160, "nominal_diameter_mm": 18, "model_diameter_mm": 18}
    plan = make_core_plan(floor, type18, data)
    assert len(plan["runs"]) == 2 and plan["physical_bar_count"] == 6
    assert plan["runs"][0]["axes"][0]["start_mm"] == [1280, 1100, -34]
    sets = []
    for index, run in enumerate(plan["runs"]):
        sets.append({"element_id": 100 + index, "host_id": 407801, "quantity": 3,
                     "number_of_bar_positions": 3, "layout_rule": "NumberWithSpacing", "bar_type": type18,
                     "hook_type_ids": [-1, -1],
                     "bars": [{"curves": [line(a["start_mm"], a["end_mm"])]} for a in run["axes"]]})
    assert compare_core_trial(plan, {"sets": sets})["status"] == "matches"
    sets[1]["bars"][0]["curves"][0]["start_mm"][1] -= 100
    assert compare_core_trial(plan, {"sets": sets})["status"] == "differs"
    print("PASS: core packet, two runs, physical readback and 100/200 mismatch rejection")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lib-dir", required=True)
    parser.add_argument("--require-ironpython", action="store_true")
    args = parser.parse_args()
    if args.require_ironpython:
        assert platform.python_implementation() == "IronPython", "Expected the real IronPython engine"
        assert platform.python_version() == "2.7.12", "Expected the specialist's engine version"
    sys.path.insert(0, os.path.abspath(args.lib_dir))
    from qm_revit_probe import PROBE_VERSION, serialize_report_utf8, write_report_json

    print("Probe {0}; {1} {2}".format(PROBE_VERSION, platform.python_implementation(),
                                     platform.python_version()))
    # Latin-1 superscript + Cyrillic hits the failing Python 2 ASCII-encoder path.
    report = {"floor": {"parameters": [
        {"name": "Площадь сечения", "display_value": "12345678 см²"},
        {"name": "Диаметр", "display_value": "Ø25 × 3900 мм; ≤ 100 мм"},
        {"name": "Строка", "display_value": '"Тест"\\путь\n\t\x00 🏗'},
    ]}, "связь": "Верхнее армирование вдоль ОСИ X.dxf",
        "issues": [{"message": "Не удалось прочитать защитный слой: 25 мм"}],
        "values": [96.875, 775.0, 9, 2 ** 40, True, False, None, ""]}
    if platform.python_implementation() == "IronPython":
        import clr  # noqa: F401 -- exercise the actual .NET string bridge
        from System import String
        report["native_dotnet_string"] = String.Format("{0}", "Плита 300 мм; см²")
    legacy_failed = False
    try:
        json.dumps(report, ensure_ascii=True, allow_nan=False, indent=2).encode("utf-8")
    except UnicodeEncodeError:
        legacy_failed = True
    if args.require_ironpython:
        assert legacy_failed, "The pre-fix code must reproduce the reported error"
    print("Legacy ASCII error reproduced: {0}".format(legacy_failed))
    encoded = serialize_report_utf8(report)
    assert json.loads(encoded.decode("utf-8")) == report
    checks = 1
    # The real writer must work with a Unicode directory and filename as well.
    directory = tempfile.mkdtemp(prefix="qm-probe-runtime-")
    try:
        unicode_directory = os.path.join(directory, "Проверка JSON")
        os.mkdir(unicode_directory)
        destination = os.path.join(unicode_directory, "отчёт.json")
        write_report_json(destination, report)
        with open(destination, "rb") as saved:
            assert json.loads(saved.read().decode("utf-8")) == report
        checks += 1
        try:
            write_report_json(destination, {"overwrite": True})
            raise AssertionError("Existing file was overwritten")
        except OSError:
            pass
        with open(destination, "rb") as saved:
            assert json.loads(saved.read().decode("utf-8")) == report
        checks += 1
        invalid = [float("nan"), float("inf"), -float("inf"), object()]
        circular = []
        circular.append(circular)
        invalid.append(circular)
        for index, value in enumerate(invalid):
            invalid_path = os.path.join(unicode_directory, "invalid-{0}.json".format(index))
            try:
                write_report_json(invalid_path, {"value": value})
                raise AssertionError("Invalid value was accepted")
            except (ValueError, TypeError):
                pass
            assert not os.path.exists(invalid_path), "Serialization failure left a partial file"
            checks += 1
        try:
            write_report_json(os.path.join(unicode_directory, "model.rvt"), report)
            raise AssertionError("Non-JSON extension was accepted")
        except ValueError:
            pass
        checks += 1
        from qm_revit_cad import hash_local_dxf

        dxf = os.path.join(unicode_directory, "Верхнее Х.dxf")
        with open(dxf, "wb") as synthetic:
            synthetic.write(b"synthetic DXF bytes")
        fingerprint = hash_local_dxf(dxf)
        assert fingerprint["status"] == "read" and fingerprint["size_bytes"] == 19
        import hashlib

        assert fingerprint["sha256"] == hashlib.sha256(b"synthetic DXF bytes").hexdigest()
        for rejected in ("//server/share/file.dxf", "relative.dxf", destination):
            try:
                hash_local_dxf(rejected)
                raise AssertionError("Unsafe CAD fingerprint path accepted")
            except ValueError:
                pass
        print("PASS: CAD module import, Unicode DXF fingerprint and unsafe-path rejection; no Revit API exercised")
    finally:
        # Only our freshly-created synthetic temporary directory, never a project directory.
        # IronPython's .NET Core POSIX stat/rmdir bridge can misclassify directories.
        # Do not let test cleanup hide the actual serializer/writer exception.
        if platform.python_implementation() == "IronPython":
            from System.IO import Directory
            Directory.Delete(directory, True)
        else:
            shutil.rmtree(directory)
    print("PASS: {0} serialization/file checks; no Revit API exercised".format(checks))
    check_trial_geometry(args.lib_dir)
    check_cad_comparison()


if __name__ == "__main__":
    main()
