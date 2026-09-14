# -*- coding: utf-8 -*-
"""Compile and exercise the full-plate portable adapter on real IronPython 2.7."""
from __future__ import print_function, unicode_literals

import argparse
import json
import math
import os
import platform
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lib-dir", required=True)
    parser.add_argument("--packet", required=True)
    parser.add_argument("--require-ironpython", action="store_true")
    args = parser.parse_args()
    if args.require_ironpython and (platform.python_implementation() != "IronPython" or sys.version_info[:2] != (2, 7)):
        raise ValueError("Real IronPython 2.7 required")
    sys.path.insert(0, args.lib_dir)
    import qm_plate_packet as packet_module
    import qm_revit_plate_trial  # noqa: F401 -- exercise actual runtime imports
    from qm_revit_probe import serialize_report_utf8
    for name in ("qm_plate_packet.py", "qm_revit_plate_trial.py"):
        path = os.path.join(args.lib_dir, name)
        with open(path, "rb") as stream:
            compile(stream.read(), path, "exec")
    button = os.path.join(os.path.dirname(args.lib_dir), "QMonitoring.tab", "Diagnostics.panel",
                          "FullPlateTrial.pushbutton", "script.py")
    with open(button, "rb") as stream:
        compile(stream.read(), button, "exec")
    packet = packet_module.load_packet(args.packet)
    materials = {}
    for direction in packet["directions"]:
        for run in direction["runs"]:
            materials[packet_module.material_key(run)] = {"element_id": run["diameter_mm"],
                "nominal_diameter_mm": run["diameter_mm"], "model_diameter_mm": run["diameter_mm"]}
    host = {"element_id": 1, "top_faces": [{"plane": {"origin_mm": [0, 0, 0], "normal": [0, 0, 1]}}],
        "bottom_faces": [{"plane": {"origin_mm": [0, 0, -1000], "normal": [0, 0, -1]}}],
        "covers": {side: {"distance_mm": 25} for side in ("top", "bottom", "other")}}
    # Synthetic host/profile only for testing arithmetic and Unicode, not actual RVT binding.
    placement = {"offset_x_mm": 100, "offset_y_mm": 200, "confirmed": True,
        "axis_depths_mm": dict(zip(packet_module.DIRECTIONS, (100, 200, 100, 200)))}
    plan = packet_module.make_plan(packet, host, materials, placement)
    sets = []
    for run in plan["runs"]:
        bars = []
        for axis in run["axes"]:
            bars.append({"curves": [{"kind": "Line", "start_mm": axis["start_mm"][:],
                "end_mm": axis["end_mm"][:], "length_mm": math.sqrt(sum((a-b)**2
                    for a, b in zip(axis["start_mm"], axis["end_mm"])))}]})
        sets.append({"host_id": 1, "quantity": run["bar_count"], "number_of_bar_positions": run["bar_count"],
            "layout_rule": run["layout_rule"], "hook_type_ids": [-1, -1],
            "bar_type": materials[packet_module.material_key(run)], "bars": bars})
    result = packet_module.compare_readback(plan, {"sets": sets})
    assert result["status"] == "matches"
    encoded = serialize_report_utf8({"packet": packet, "result": result, "unicode": "Арматура, мм²; Виктор"})
    assert json.loads(encoded.decode("utf-8"))["packet"] == packet
    sets[-1]["bars"][0]["curves"][0]["end_mm"][0] += 10
    assert packet_module.compare_readback(plan, {"sets": sets})["status"] == "differs"
    print("PASS: {0} {1}; all four directions, {2} runs, {3} bars; Unicode; changed-axis rejection; NO REVIT API".format(
        platform.python_implementation(), platform.python_version(), len(sets), result["physical_bar_count"]))


if __name__ == "__main__":
    main()
