# -*- coding: utf-8 -*-
"""Native view-specific family instances only; independent post-Commit readback."""
from __future__ import division, unicode_literals

import copy
import math
import traceback

from qm_revit_probe import element_id, text_type
from qm_revit_trial import failure_recorder, rollback_scope
from qm_trial_worksharing import classify_document
from qm_workflow_81 import (VERSION, REPORT_SCHEMA, DISCLAIMER, source_components,
    parameter_catalog, validate_binding, bind_values, symbol_catalog)


def preflight(document, DB, view):
    if (document.IsFamilyDocument or document.IsReadOnly or document.IsModifiable
            or text_type(document.Application.VersionNumber) != "2024"):
        raise ValueError("Open a writable Revit 2024 project without an active transaction")
    if (not isinstance(view, DB.ViewPlan) or view.IsTemplate or view.Document != document
            or abs(abs(view.ViewDirection.Z)-1) > 1e-8):
        raise ValueError("Open a horizontal floor/ceiling/structural plan, not a template or 3D view")
    sharing = classify_document(document, DB)
    if sharing["status"] == "blocked":
        raise ValueError("Unsupported worksharing mode: "+"; ".join(sharing["issues"]))
    return sharing


def _workset(document):
    return element_id(document.GetWorksetTable().GetActiveWorksetId()) if document.IsWorkshared else None


def _symbol(document, DB, identifier, role):
    for row in symbol_catalog(document, DB, role):
        if row["id"] == identifier:
            symbol = document.GetElement(DB.ElementId(identifier))
            if document.IsWorkshared:
                owner = text_type(DB.WorksharingUtils.GetCheckoutStatus(document, symbol.Id))
                if owner == "OwnedByOtherUser":
                    raise ValueError("Chosen family type belongs to another user")
            return symbol, row
    raise ValueError("Chosen family type no longer exists or has unsupported category/placement")


def _start(document, DB, failures):
    group = DB.TransactionGroup(document, "QMonitoring 8.1 - SOURCE view families, NOT Rebar")
    group.IsFailureHandlingForcedModal = True
    if group.Start() != DB.TransactionStatus.Started:
        raise ValueError("Transaction group failed to start")
    tx = DB.Transaction(document, "QMonitoring 8.1 view-family instances")
    try:
        if tx.Start() != DB.TransactionStatus.Started:
            raise ValueError("Transaction failed to start")
        options = tx.GetFailureHandlingOptions().SetClearAfterRollback(True)
        options = options.SetForcedModalHandling(True).SetFailuresPreprocessor(failure_recorder(DB, failures))
        tx.SetFailureHandlingOptions(options)
        return group, tx
    except Exception:
        _rollback(group, tx, DB)
        raise


def _rollback(group, tx, DB):
    inner_ok, inner = (True, {}) if tx is None else rollback_scope(tx, DB, allow_committed=True)
    if not inner_ok:
        return False, {"status": "inner_rollback_unconfirmed", "inner": inner}
    if group is None:
        return True, inner
    return rollback_scope(group, DB)


def _dispose(group, tx):
    for value in (tx, group):
        if value is not None:
            try:
                value.Dispose()
            except Exception:
                pass


def _point(DB, view, xy):
    return DB.XYZ(xy[0]/304.8, xy[1]/304.8, view.Origin.Z)


def _line_points(row):
    x, y = row["center_xy_mm"]
    half = row["length_mm"]/2
    return ([x-half, y], [x+half, y]) if row["direction"].endswith("X") else ([x, y-half], [x, y+half])


def _create(document, DB, view, symbol, record, row):
    if not symbol.IsActive:
        symbol.Activate()
        document.Regenerate()
    point = _point(DB, view, row["center_xy_mm"])
    if record["placement"] == "CurveBasedDetail":
        start, end = _line_points(row)
        result = document.Create.NewFamilyInstance(DB.Line.CreateBound(_point(DB, view, start), _point(DB, view, end)), symbol, view)
    else:
        result = document.Create.NewFamilyInstance(point, symbol, view)
        if record["role"] == "zone":
            basis = result.GetTransform().BasisX
            delta = row["rotation_rad"]-math.atan2(basis.Y, basis.X)
            if abs(delta) > 1e-8:
                axis = DB.Line.CreateBound(point, DB.XYZ(point.X, point.Y, point.Z+1))
                DB.ElementTransformUtils.RotateElement(document, result.Id, axis, delta)
    return result


def inspect_parameters(document, DB, view, symbol_id, role, confirmed=False):
    """Temporary instance -> parameter inventory -> confirmed full rollback.

    No parameter names or types are invented. This can activate the chosen symbol
    temporarily; the outer group is rolled back before returning to the UI.
    """
    if confirmed is not True:
        raise ValueError("Explicit consent for the temporary rollback inspection is required")
    preflight(document, DB, view)
    symbol, record = _symbol(document, DB, symbol_id, role)
    active, group, tx = _workset(document), None, None
    try:
        group, tx = _start(document, DB, [])
        row = {"center_xy_mm": [0, 0], "length_mm": 1000, "rotation_rad": 0, "direction": "bottom-X"}
        instance = _create(document, DB, view, symbol, record, row)
        document.Regenerate()
        result = {"symbol": record, "parameters": parameter_catalog(instance, DB)}
        if _workset(document) != active:
            raise ValueError("Active workset changed")
        okay, state = _rollback(group, tx, DB)
        if not okay:
            raise ValueError("Inspection rollback NOT confirmed: "+text_type(state))
        result["rollback_confirmed"] = True
        return result
    except Exception:
        okay, state = _rollback(group, tx, DB)
        if not okay:
            raise ValueError("Inspection rollback NOT confirmed; do not Save/Sync: "+text_type(state))
        raise
    finally:
        _dispose(group, tx)


def probe_binding(document, DB, view, symbol_id, role, binding, row, confirmed=False, profile_id=None):
    """Temp instance -> real write of the chosen binding -> Regenerate -> confirmed rollback.

    Answers "will this family actually hold these values" on ONE instance, before a
    whole batch is created and thrown away. A rejected probe is reported, not raised:
    the caller shows it and lets the user pick another family or parameter.
    """
    if confirmed is not True:
        raise ValueError("Explicit consent for the temporary write probe is required")
    preflight(document, DB, view)
    symbol, record = _symbol(document, DB, symbol_id, role)
    active, group, tx = _workset(document), None, None
    try:
        group, tx = _start(document, DB, [])
        instance = _create(document, DB, view, symbol, record, row)
        validate_binding(binding, parameter_catalog(instance, DB), role, record["placement"], profile_id)
        observed, message = {}, None
        try:
            bind_values(instance, DB, row, binding, True)
            # Regenerate before readback: constraint-driven parameters keep the value
            # that was Set until the family is re-solved, so an immediate read can lie.
            document.Regenerate()
            observed = bind_values(instance, DB, row, binding, False)
        except Exception as exc:
            message = text_type(exc)
        result = {"symbol": record, "role": role, "values": observed, "source_id": row.get("source_id"),
            "status": "holds" if message is None else "rejected", "message": message}
        if _workset(document) != active:
            raise ValueError("Active workset changed")
        okay, state = _rollback(group, tx, DB)
        if not okay:
            raise ValueError("Probe rollback NOT confirmed: "+text_type(state))
        result["rollback_confirmed"] = True
        return result
    except Exception:
        okay, state = _rollback(group, tx, DB)
        if not okay:
            raise ValueError("Probe rollback NOT confirmed; do not Save/Sync: "+text_type(state))
        raise
    finally:
        _dispose(group, tx)


def _read_geometry(instance, view, record, row):
    if element_id(instance.OwnerViewId) != element_id(view.Id):
        raise ValueError("Created family is not owned by the target view")
    if text_type(instance.Symbol.UniqueId) != record["unique_id"]:
        raise ValueError("Created family type differs from the selected binding")
    if record["placement"] == "CurveBasedDetail":
        expected = _line_points(row)
        curve = instance.Location.Curve
        for index in (0, 1):
            point = curve.GetEndPoint(index)
            if max(abs(point.X*304.8-expected[index][0]), abs(point.Y*304.8-expected[index][1])) > 0.01:
                raise ValueError("Family line endpoint readback differs")
    else:
        point = instance.Location.Point
        if max(abs(point.X*304.8-row["center_xy_mm"][0]), abs(point.Y*304.8-row["center_xy_mm"][1])) > 0.01:
            raise ValueError("Family insertion point readback differs")
        if record["role"] == "zone":
            basis = instance.GetTransform().BasisX
            if (abs(basis.X-math.cos(row["rotation_rad"])) > 1e-8
                    or abs(basis.Y-math.sin(row["rotation_rad"])) > 1e-8 or abs(basis.Z) > 1e-8):
                raise ValueError("Family local X orientation readback differs")


def place_source_families(document, DB, view, packet, direction, selections,
                          offset_xy_mm=(0, 0), confirmed=False, progress=None):
    """One instance of the chosen detail AND annotation per source component.

    Only newly created instances are assigned instance parameters. Family shape
    correctness (constraints/visibility/array formula) remains a native review;
    parameter readback is NOT engineering or actual physical geometry acceptance.
    """
    report = {"schema_version": REPORT_SCHEMA, "version": VERSION, "status": "blocked_preflight",
        "placement_eligible": False, "engineering_approval": False, "structural_elements_created": 0,
        "disclaimer": DISCLAIMER, "issues": [], "commit_failures": [], "created_instance_ids": [],
        "checkout_requested": False, "save_requested": False, "synchronization_requested": False,
        "live_workflow_verified": False, "not_checked": ["family-visible-shape-matches-parameters",
            "physical-bar-coverage-and-collisions", "source-to-current-underlay-identity", "structural-placement"]}
    group, tx, complete = None, None, False
    attempted = []
    try:
        if confirmed is not True:
            raise ValueError("Confirm SOURCE detail/annotation family placement on the CURRENT view")
        report["worksharing"] = preflight(document, DB, view)
        rows = source_components(packet, direction, offset_xy_mm)
        report["source_packet"] = copy.deepcopy(packet)
        report["source_components"] = copy.deepcopy(rows)
        report["source_component_count"] = len(rows)
        report["view"] = {"id": element_id(view.Id), "unique_id": text_type(view.UniqueId)}
        report["bindings"] = copy.deepcopy(selections)
        report["family_profiles"] = dict((role, {"profile_id": selections[role].get("profile_id", "generic-explicit"), "field_storage": ({"step_mm": "annotation-only", "bar_count": "annotation-only", "source_id": "zone-comments"} if selections[role].get("profile_id") else "all-fields-bound")}) for role in selections)
        symbols, records = {}, {}
        if set(selections) != set(("zone", "annotation")):
            raise ValueError("Select both detail and annotation families")
        for role in ("zone", "annotation"):
            selection = selections[role]
            symbols[role], records[role] = _symbol(document, DB, selection["symbol_id"], role)
            if records[role] != selection["inspection"]["symbol"] or selection["inspection"].get("rollback_confirmed") is not True:
                raise ValueError("Family identity changed since the rollback inspection")
            validate_binding(selection["binding"], selection["inspection"]["parameters"], role, records[role]["placement"], selection.get("profile_id"))
        active_workset = _workset(document)
        group, tx = _start(document, DB, report["commit_failures"])
        for index, row in enumerate(rows):
            if progress is not None and progress(index, len(rows)) is False:
                raise ValueError("User cancelled source-family placement")
            for role in ("zone", "annotation"):
                instance = _create(document, DB, view, symbols[role], records[role], row)
                current_catalog = parameter_catalog(instance, DB)
                binding = selections[role]["binding"]
                validate_binding(binding, current_catalog, role, records[role]["placement"], selections[role].get("profile_id"))
                bind_values(instance, DB, row, binding, True)
                attempted.append({"element_id": element_id(instance.Id), "role": role, "source_id": row["source_id"]})
        document.Regenerate()
        if _workset(document) != active_workset:
            raise ValueError("Active workset changed; rolling back")
        returned = tx.Commit()
        if returned != DB.TransactionStatus.Committed or tx.GetStatus() != returned:
            raise ValueError("Commit not confirmed")
        # Derive expected complete identity sequence afresh, not from draw mapping.
        expected = []
        for row in source_components(packet, direction, offset_xy_mm):
            for role in ("zone", "annotation"):
                expected.append((row, role))
        if len(attempted) != len(expected):
            raise ValueError("Missing source family instance")
        seen = set()
        for index, pair in enumerate(expected):
            row, role = pair
            mapping = attempted[index]
            identifier = mapping["element_id"]
            if identifier in seen or mapping["role"] != role or mapping["source_id"] != row["source_id"]:
                raise ValueError("Duplicate or missing source-instance identity")
            seen.add(identifier)
            instance = document.GetElement(DB.ElementId(identifier))
            if instance is None:
                raise ValueError("Created source family disappeared after Commit")
            _read_geometry(instance, view, records[role], row)
            binding = selections[role]["binding"]
            validate_binding(binding, parameter_catalog(instance, DB), role, records[role]["placement"], selections[role].get("profile_id"))
            bind_values(instance, DB, row, binding, False)
        if _workset(document) != active_workset:
            raise ValueError("Active workset changed after Commit")
        returned = group.Assimilate()
        if returned != DB.TransactionStatus.Committed or group.GetStatus() != returned:
            raise ValueError("Transaction group completion not confirmed")
        complete = True
        report["status"] = "source_view_families_created"
        report["created_instance_ids"] = sorted(seen)
        report["source_instance_mapping"] = attempted
        report["readback"] = {"status": "matches", "instance_count": len(seen), "xy_tolerance_mm": 0.01,
            "scope": "instance parameters, source IDs, view ownership, location and orientation only"}
        report["active_workset_unchanged"] = True
    except Exception as exc:
        report["issues"].append({"message": text_type(exc), "traceback": traceback.format_exc()})
        if group is not None and not complete:
            okay, state = _rollback(group, tx, DB)
            report["rollback"] = state
            report["status"] = "failed_rolled_back" if okay else "rollback_unconfirmed"
            report["attempted_instance_mapping"] = attempted
    finally:
        _dispose(group, tx)
    return report
