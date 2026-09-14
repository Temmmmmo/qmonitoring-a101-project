# -*- coding: utf-8 -*-
"""CAD metadata and bounded comparison of two readbacks; never infer a CAD layer."""
from __future__ import division

import math
from itertools import permutations, product

from qm_revit_probe import element_id, element_name, text_type

SYMBOL_METHOD = "symbol-geometry-with-instance-transform-chain-once"
INSTANCE_METHOD = "root-instance-geometry-then-nested-symbol-chain"
COMPARE_TOLERANCE_MM = 0.01
MAX_COMPARE_CHECKS = 1000000


def style_info(probe, obj, inherited=None):
    """Keep the raw ID/status. Only an explicit invalid ID may inherit a parent layer."""
    row = {"graphics_style_id": None, "graphics_style_status": "read_error",
           "layer": None, "layer_source": "unresolved"}
    try:
        raw = obj.GraphicsStyleId
        row["graphics_style_id"] = element_id(raw)
        if row["graphics_style_id"] == -1:
            row["graphics_style_status"] = "invalid_id"
            if inherited is not None:
                row["layer"], row["layer_source"] = inherited, "parent"
            return row
        style = probe.doc.GetElement(raw)
        if style is None:
            row["graphics_style_status"] = "not_found"
            return row
        category = style.GraphicsStyleCategory
        if category is None:
            row["graphics_style_status"] = "no_category"
            return row
        row.update({"graphics_style_status": "resolved", "layer": element_name(category, probe.DB),
                    "layer_source": "graphics_style", "category_id": element_id(category.Id)})
    except Exception as exc:
        row["error"] = text_type(exc)[:1024]
    return row


def material_info(probe, obj):
    """Material identity/color is evidence, not a substitute for layer identity."""
    row = {"element_id": None, "status": "not_available"}
    try:
        raw = obj.MaterialElementId
        row["element_id"] = element_id(raw)
        if row["element_id"] == -1:
            row["status"] = "invalid_id"
            return row
        material = probe.doc.GetElement(raw)
        if material is None:
            row["status"] = "not_found"
            return row
        row["name"] = element_name(material, probe.DB)
        color = material.Color
        row["color_rgb"] = [int(color.Red), int(color.Green), int(color.Blue)]
        row["status"] = "read"
    except Exception as exc:
        row["error"] = text_type(exc)[:1024]
    return row


def layer_catalog(probe, instance):
    """Inspect the CAD category separately; never relabel a mesh from this inventory."""
    result = {"status": "partial", "layers": [], "issues": []}
    try:
        category = instance.Category
        if category is None:
            raise ValueError("CAD instance has no category")
        result["category_name"] = element_name(category, probe.DB)
        for subcategory in category.SubCategories:
            if len(result["layers"]) >= 1000:
                raise ValueError("CAD subcategory limit exceeded")
            row = {"category_id": element_id(subcategory.Id), "name": element_name(subcategory, probe.DB)}
            result["layers"].append(row)
            try:
                style = subcategory.GetGraphicsStyle(probe.DB.GraphicsStyleType.Projection)
                row["projection_style_id"] = element_id(style.Id) if style is not None else None
                color = subcategory.LineColor
                row["color_rgb"] = [int(color.Red), int(color.Green), int(color.Blue)]
            except Exception as exc:
                row["error"] = text_type(exc)[:1024]
                result["issues"].append("Cannot read metadata for CAD subcategory")
        if not result["issues"]:
            result["status"] = "collected"
    except Exception as exc:
        result["issues"].append(text_type(exc)[:1024])
    return result


def all_triangles(geometry):
    """Known KLEENKA payload is stored once; other/unknown meshes stay diagnostic."""
    for triangle in geometry["triangles_mm"]:
        yield triangle
    for mesh in geometry["meshes"]:
        for triangle in mesh["triangles_mm"]:
            yield triangle


def compare_geometry_reads(symbol, instance):
    """A bijection of mesh triangles, including duplicates, NOT a layer/calibration proof.

    The tolerance is fixed, no fitting/rounding/translation. Triangle order and vertex
    winding may differ. Comparison has a work limit; a limit is never called a match.
    """
    result = {"status": "not_checked", "tolerance_mm": COMPARE_TOLERANCE_MM,
              "placement_eligible": False, "layer_identity_verified": False,
              "scope": "two API mesh readbacks only; no DXF calibration or layer certification"}
    try:
        if symbol["method"] != SYMBOL_METHOD or instance["method"] != INSTANCE_METHOD:
            raise ValueError("Expected two distinct CAD reading methods")
        for geometry in (symbol, instance):
            if (geometry["mesh_read_complete"] is not True or geometry["units"] != "mm"
                    or geometry["coordinate_system"] != "revit-internal-origin-and-axes"):
                raise ValueError("Mesh reading incomplete or coordinate systems differ")
        first, second = list(all_triangles(symbol)), list(all_triangles(instance))
        result["symbol_triangle_count"], result["instance_triangle_count"] = len(first), len(second)
        if not first or not second:
            raise ValueError("Empty mesh readback")
        if len(first) != len(second):
            result["status"] = "differs"
            return result

        def key(triangle):
            if len(triangle) != 3 or any(len(p) != 3 for p in triangle):
                raise ValueError("Invalid triangle shape")
            if any(isinstance(v, bool) or math.isnan(v) or math.isinf(v) for p in triangle for v in p):
                raise ValueError("Non-finite mesh point")
            return tuple(int(math.floor(sum(p[i] for p in triangle) / (3 * COMPARE_TOLERANCE_MM)))
                         for i in range(3))

        def error(a, b):
            return min(max(math.sqrt(sum((x-y)**2 for x, y in zip(p, q))) for p, q in zip(a, order))
                       for order in permutations(b))

        buckets = {}
        for i, triangle in enumerate(second):
            buckets.setdefault(key(triangle), []).append(i)
        remaining, checks, maximum = set(range(len(second))), 0, 0.0
        for triangle in first:
            center, found = key(triangle), None
            for delta in product((-1, 0, 1), repeat=3):
                indices = buckets.get(tuple(a+b for a, b in zip(center, delta)), ())
                for i in indices:
                    checks += 1
                    if checks > MAX_COMPARE_CHECKS:
                        raise ValueError("Mesh comparison work limit exceeded")
                    if i not in remaining:
                        continue
                    difference = error(triangle, second[i])
                    if difference <= COMPARE_TOLERANCE_MM:
                        found = i
                        maximum = max(maximum, difference)
                        break
                if found is not None:
                    break
            if found is None:
                result["status"] = "differs"
                result["matched_triangle_count"] = len(second) - len(remaining)
                return result
            remaining.remove(found)
        result.update({"status": "matches", "matched_triangle_count": len(first),
                       "max_vertex_error_mm": maximum, "candidate_checks": checks})
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        result["reason"] = text_type(exc)
    return result
