# -*- coding: utf-8 -*-
"""Mandatory-rollback experiments. JSON mode commits ONLY inside a rollback group.

DB and the .NET List[Curve] factory are injected; no Autodesk dependency outside Revit.
"""
from __future__ import division

import datetime
import traceback

from qm_revit_probe import PROBE_VERSION, Probe, element_id, identity, text_type
from qm_trial_geometry import close, compare_trial, make_trial_plan, validate_prism
from qm_trial_input import validate_trial_input


def solid_report(probe, floor):
    DB = probe.DB
    options = DB.Options()
    options.DetailLevel = DB.ViewDetailLevel.Fine
    geometry = floor.get_Geometry(options)
    solids = []
    for item in geometry:
        if isinstance(item, DB.GeometryInstance):
            raise ValueError("Nested host geometry is outside this experiment")
        if isinstance(item, DB.Solid) and item.Volume > 0:
            solids.append(item)
    if len(solids) != 1:
        raise ValueError("Expected exactly one positive-volume floor solid")
    solid = solids[0]
    faces = []
    for face in solid.Faces:
        plane = ({"origin_mm": probe.point(face.Origin), "normal": probe.vector(face.FaceNormal)}
                 if isinstance(face, DB.PlanarFace) else None)
        faces.append({"plane": plane, "edge_loops": [
            [probe.curve(edge.AsCurve()) for edge in loop] for loop in face.EdgeLoops]})
    # Revit solid volume is cubic internal feet, not a length parameter.
    return {"faces": faces, "volume_mm3": float(solid.Volume) * probe.mm(1.0) ** 3}


def reference_snapshot(probe):
    floor = probe.resolve_reference(407801, probe.DB.Floor, "TEST_SLAB_01")
    area = probe.resolve_reference(407878, probe.DB.Structure.AreaReinforcement, "AR_TEST_TOP_X_001")
    floor_data = probe.floor(floor)
    area_data = probe.area(area, floor_data)
    if probe.issues:
        raise ValueError("Reference readback is incomplete; inspect reference_issues")
    if area_data["host_id"] != floor_data["element_id"]:
        raise ValueError("Reference area has a different host")
    if area_data["reference_comparison"]["status"] != "matches":
        raise ValueError("Manual reference no longer matches the confirmed geometry")
    # Stable built-in IDs from the received Revit 2024 report, not localized labels.
    values = {p["parameter_id"]: p.get("value") for p in area_data["parameters"]}
    for number, expected in ((-1018100, 1), (-1018102, 0), (-1018103, 0), (-1018104, 0)):
        if values.get(number) != expected:
            raise ValueError("Reference must have only Top Major enabled")
    measurement = area_data["measurement"]
    if any(abs(a - b) > 1e-8 for a, b in zip(measurement["along_unit_vector"], [1, 0, 0])):
        raise ValueError("Reference must run along internal X")
    for bar in area_data["physical_bars"]:
        depth = bar["top_face_depths"]
        if not close(depth.get("axis_depth_min_mm"), 37.5) or not close(
                depth.get("axis_depth_max_mm"), 37.5):
            raise ValueError("Reference bar elevation differs from top cover plus radius")
    return floor, {"floor": floor_data, "area": area_data}


def obstacle_candidate(probe, element, host_id, host_sketch_id):
    """Classify by API identity/category/ownership, NEVER by name or a captured ID."""
    DB = probe.DB
    row = identity(element, DB)
    row["category"] = None
    category = None
    try:
        category = element.Category
        if category is not None:
            row["category"] = {"id": element_id(category.Id), "name": text_type(category.Name),
                               "type": text_type(category.CategoryType)}
    except Exception as exc:
        row["category_read_error"] = text_type(exc)
    try:
        row["owner_view_id"] = element_id(element.OwnerViewId)
    except Exception as exc:
        row["owner_view_read_error"] = text_type(exc)
    try:
        row["bbox_mm"] = probe.bbox(element)
    except Exception as exc:
        row["bbox_read_error"] = text_type(exc)
    reason = None
    if row["element_id"] == host_id:
        reason = "selected_host"
    elif isinstance(element, DB.ImportInstance):
        reason = "cad_reference"
    elif isinstance(element, DB.Sketch):
        try:
            row["sketch_owner_id"] = element_id(element.OwnerId)
            if row["element_id"] == host_sketch_id and row["sketch_owner_id"] == host_id:
                reason = "selected_host_sketch"
        except Exception as exc:
            row["sketch_owner_read_error"] = text_type(exc)
    elif isinstance(element, DB.View):
        reason = "view_definition"
    elif row["category"] is not None and "category_read_error" not in row:
        if row["category"]["id"] == int(DB.BuiltInCategory.OST_SectionBox):
            reason = "section_box_category"
        elif row["category"]["id"] == int(DB.BuiltInCategory.OST_Cameras):
            # A 3D view camera is reported as CategoryType.Model in Revit 2024.
            # It is not a physical security-camera family; do not filter by name.
            reason = "view_camera_category"
        elif category.CategoryType == DB.CategoryType.Annotation:
            reason = "annotation_category"
    row["decision"] = "ignored_non_obstacle" if reason else "blocks_trial"
    row["reason"] = reason or "physical_or_unclassified_candidate"
    return row


def find_obstacles(probe, plan, audit=None):
    """Conservative full-depth box with a narrow, recorded nonphysical allowlist."""
    DB, doc = probe.DB, probe.doc
    audit = audit if audit is not None else {}
    audit.update({"scope": "conservative-bbox-reservation-not-exact-collision", "ignored": [],
                  "host_sketch_id": None})
    try:
        host = doc.GetElement(DB.ElementId(plan["host_id"]))
        audit["host_sketch_id"] = element_id(host.SketchId)
    except Exception as exc:
        # An unavailable ownership check never authorizes skipping a sketch.
        audit["host_sketch_read_error"] = text_type(exc)
    # Unloaded linked RVTs can hide geometry. Do not certify a free window in their presence.
    links = list(DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance)
                 .WhereElementIsNotElementType())
    if links:
        raise ValueError("Linked RVT models require a separate collision check")

    def point(values):
        return DB.XYZ(*[DB.UnitUtils.ConvertToInternalUnits(v, DB.UnitTypeId.Millimeters) for v in values])

    outline = DB.Outline(point(plan["reservation_mm"]["min_mm"]),
                         point(plan["reservation_mm"]["max_mm"]))
    try:
        candidates = list(DB.FilteredElementCollector(doc).WhereElementIsNotElementType()
                          .WherePasses(DB.BoundingBoxIntersectsFilter(outline)))
    finally:
        outline.Dispose()
    result = []
    for element in candidates:
        row = obstacle_candidate(probe, element, plan["host_id"], audit["host_sketch_id"])
        if row["decision"] == "ignored_non_obstacle":
            audit["ignored"].append(row)
        else:
            result.append(row)
    audit["candidate_count"] = len(candidates)
    audit["blocking_count"] = len(result)
    return result


def create_trial_rebar(probe, floor, bar_type, plan, curve_list_factory):
    DB = probe.DB

    def point(values):
        return DB.XYZ(*[DB.UnitUtils.ConvertToInternalUnits(v, DB.UnitTypeId.Millimeters) for v in values])

    curves = curve_list_factory()
    curves.Add(DB.Line.CreateBound(point(plan["axes"][0]["start_mm"]),
                                   point(plan["axes"][0]["end_mm"])))
    rebar = DB.Structure.Rebar.CreateFromCurves(
        probe.doc, DB.Structure.RebarStyle.Standard, bar_type, None, None, floor,
        DB.XYZ(*plan["normal"]), curves, DB.Structure.RebarHookOrientation.Right,
        DB.Structure.RebarHookOrientation.Left, True, True)
    if rebar is None:
        raise ValueError("Revit returned no Rebar; no substitute geometry was created")
    accessor = rebar.GetShapeDrivenAccessor()
    try:
        accessor.SetLayoutAsNumberWithSpacing(
            plan["bar_count"], DB.UnitUtils.ConvertToInternalUnits(
                plan["spacing_mm"], DB.UnitTypeId.Millimeters), True, True, True)
    finally:
        accessor.Dispose()
    return rebar


def read_trial_rebar(probe, rebar):
    data = identity(rebar, probe.DB)
    data.update({"host_id": element_id(rebar.GetHostId()), "quantity": int(rebar.Quantity),
                 "number_of_bar_positions": int(rebar.NumberOfBarPositions),
                 "layout_rule": text_type(rebar.LayoutRule), "bars": [],
                 "bar_type": probe.bar_type(probe.doc.GetElement(rebar.GetTypeId())),
                 "hook_type_ids": [element_id(rebar.GetHookTypeId(i)) for i in (0, 1)]})
    if not 0 <= data["number_of_bar_positions"] <= 1002:
        raise ValueError("Unexpected number of trial positions")
    for index in range(data["number_of_bar_positions"]):
        if rebar.DoesBarExistAtPosition(index):
            # Rebar overload has MultiplanarOption; RebarInSystem overload does not.
            # Both return FINAL curves. No extra position or moved-bar transform.
            curves = rebar.GetTransformedCenterlineCurves(
                False, False, False, probe.DB.Structure.MultiplanarOption.IncludeAllMultiplanarCurves,
                index)
            data["bars"].append({"position_index": index, "curves": [probe.curve(c) for c in curves]})
    return data


def document_ids(document, DB):
    # Types too: a temporary RebarShape must disappear together with the new set.
    # Revit requires a filter before extraction; an unfiltered collector is invalid.
    instances = DB.FilteredElementCollector(document).WhereElementIsNotElementType().ToElementIds()
    types = DB.FilteredElementCollector(document).WhereElementIsElementType().ToElementIds()
    return set(element_id(i) for i in list(instances) + list(types))


def failure_recorder(DB, failures):
    """Abort on ANY newly posted warning/error; record it, never resolve or dismiss it."""
    class Recorder(DB.IFailuresPreprocessor):
        def PreprocessFailures(self, accessor):
            try:
                messages = list(accessor.GetFailureMessages())
                for message in messages:
                    failures.append({"severity": text_type(message.GetSeverity()),
                                     "description": text_type(message.GetDescriptionText()),
                                     "failing_ids": [element_id(i) for i in message.GetFailingElementIds()]})
                if not messages:
                    return DB.FailureProcessingResult.Continue
            except Exception as exc:
                failures.append({"read_error": text_type(exc)})
            return DB.FailureProcessingResult.ProceedWithRollBack
    return Recorder()


def rollback_scope(scope, DB, allow_committed=False):
    """Close an inner transaction or outer group; never treat Pending as success."""
    result = {"status": "unconfirmed"}
    ended = False
    try:
        status = scope.GetStatus()
        returned = scope.RollBack() if status == DB.TransactionStatus.Started else status
        final_status = scope.GetStatus()
        rolled_back = returned == DB.TransactionStatus.RolledBack and final_status == returned
        committed = allow_committed and text_type(returned) == "Committed" and final_status == returned
        ended = rolled_back or committed
        result = {"status": "rolled_back" if rolled_back else "committed" if committed else "unconfirmed",
                  "returned": text_type(returned), "final": text_type(final_status)}
    except Exception as exc:
        result["message"] = text_type(exc)
    finally:
        try:
            scope.Dispose()
        except Exception as exc:
            ended = False
            result.update({"status": "unconfirmed", "dispose_error": text_type(exc)})
    return ended, result


def run_trial(document, DB, curve_list_factory, copy_confirmed=False, runtime=None, trial_input=None):
    commit_mode = trial_input is not None
    report = {"schema_version": "revit-creation-trial/v1", "probe_version": PROBE_VERSION,
              "created_utc": datetime.datetime.utcnow().isoformat() + "Z", "runtime": runtime or {},
              "units": "mm", "coordinate_system": "revit-internal-origin-and-axes",
              "mode": "mandatory-rollback", "placement_eligible": False, "issues": [],
              "status": "blocked_preflight", "rollback": {"status": "not_started"},
              "not_checked": ["commit-time-validation", "source-dxf-calibration", "global-grid-phase",
                              "xy-layer-order", "full-plate-collisions", "demand-and-anchorage",
                              "engineering-acceptance"]}
    probe = Probe(document, DB)
    if commit_mode:
        report.update({"schema_version": "revit-json-creation-trial/v1",
                       "mode": "commit-readback-rollback", "commit": {"status": "not_attempted"},
                       "commit_failures": [], "group_rollback": {"status": "not_started"}})
    report["reference_issues"] = probe.issues
    stage = "preflight"
    transaction = None
    group = None
    before_ids = None
    rollback_ok = False
    try:
        if not copy_confirmed:
            raise ValueError("Explicit confirmation of a test COPY is required")
        if commit_mode:
            report["input"] = validate_trial_input(trial_input)
        if document is None or document.IsFamilyDocument or document.IsWorkshared:
            raise ValueError("Open the non-workshared project copy, not a family or shared model")
        if document.IsReadOnly or document.IsModifiable:
            raise ValueError("Document must be writable with no open transaction")
        report["document"] = {"title": text_type(document.Title), "is_workshared": False,
                              "is_modified_before": bool(document.IsModified)}
        report["revit"] = {key: text_type(getattr(document.Application, key)) for key in
                           ("VersionNumber", "VersionBuild", "SubVersionNumber")}
        if report["revit"]["VersionNumber"] != "2024":
            raise ValueError("This experiment targets Revit 2024 only")
        if not callable(getattr(DB.Structure.Rebar, "GetTransformedCenterlineCurves", None)):
            raise ValueError("The required final-centerline API is unavailable")
        floor, before = reference_snapshot(probe)
        report["reference_before"] = before
        solid = solid_report(probe, floor)
        report["host_solid"] = solid
        report["host_check"] = validate_prism(before["floor"], solid)
        bar_type = document.GetElement(DB.ElementId(165163))
        if not isinstance(bar_type, DB.Structure.RebarBarType):
            raise ValueError("Reference type 165163 is not a RebarBarType")
        plan = make_trial_plan(before["floor"], probe.bar_type(bar_type), trial_input)
        report["plan"] = plan
        report["obstacle_check"] = {}
        report["obstacles"] = find_obstacles(probe, plan, report["obstacle_check"])
        if report["obstacles"]:
            raise ValueError("Test reservation is occupied; no automatic relocation or deletion")
        before_ids = document_ids(document, DB)
        if commit_mode:
            stage = "transaction_group"
            group = DB.TransactionGroup(document, "QMonitoring JSON trial - ALWAYS ROLLBACK")
            group.IsFailureHandlingForcedModal = True
            if group.Start() != DB.TransactionStatus.Started:
                raise ValueError("Revit did not start the rollback group")
        stage = "transaction"
        transaction = DB.Transaction(document, "QMonitoring trial - ALWAYS ROLLBACK")
        if transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError("Revit did not start the trial transaction")
        # Clears only messages of the discarded test, never commits/resolves production failures.
        options = transaction.GetFailureHandlingOptions().SetClearAfterRollback(True)
        if commit_mode:
            # Keep the managed interface wrapper alive through Commit and cleanup.
            recorder = failure_recorder(DB, report["commit_failures"])
            options = options.SetForcedModalHandling(True).SetFailuresPreprocessor(recorder)
        transaction.SetFailureHandlingOptions(options)
        stage = "create"
        rebar = create_trial_rebar(probe, floor, bar_type, plan, curve_list_factory)
        report["temporary_element_id"] = element_id(rebar.Id)
        stage = "regenerate"
        document.Regenerate()
        stage = "readback"
        report["readback"] = read_trial_rebar(probe, rebar)
        report["comparison"] = compare_trial(plan, report["readback"])
        if commit_mode:
            if report["comparison"]["status"] != "matches":
                raise ValueError("Pre-commit axes differ; Commit was not attempted")
            stage = "commit"
            report["commit"]["status"] = "attempted"
            returned = transaction.Commit()
            final_status = transaction.GetStatus()
            report["commit"].update({"returned": text_type(returned), "final": text_type(final_status)})
            if returned != DB.TransactionStatus.Committed or final_status != returned:
                report["commit"]["status"] = "not_committed"
                raise ValueError("Inner Commit did not complete synchronously as Committed")
            report["commit"]["status"] = "committed"
            if report["commit_failures"]:
                raise ValueError("Commit posted failures; the test is not accepted")
            stage = "post_commit_readback"
            # Reacquire by ID: do not substitute pre-Commit curves for real post-Commit readback.
            committed_rebar = document.GetElement(DB.ElementId(report["temporary_element_id"]))
            if not isinstance(committed_rebar, DB.Structure.Rebar):
                raise ValueError("Committed test Rebar is missing")
            report["post_commit_readback"] = read_trial_rebar(probe, committed_rebar)
            report["post_commit_comparison"] = compare_trial(plan, report["post_commit_readback"])
            report["post_commit_comparison"]["scope"] = "after inner Commit, before mandatory group rollback"
            report["not_checked"].remove("commit-time-validation")
        # These are persistent document warnings, not all pending commit-time failures.
        report["document_warnings_before_rollback"] = [text_type(w.GetDescriptionText())
                                                      for w in document.GetWarnings()]
    except Exception as exc:
        report["issues"].append({"stage": stage, "error_type": type(exc).__name__,
                                 "message": text_type(exc), "traceback": traceback.format_exc()})
    finally:
        if transaction is not None:
            rollback_ok, report["rollback"] = rollback_scope(transaction, DB, allow_committed=commit_mode)
        if group is not None:
            report["inner_transaction_end"] = report["rollback"]
            if transaction is None or rollback_ok:
                rollback_ok, report["group_rollback"] = rollback_scope(group, DB)
            else:
                # Do not call RollBack on a group while its inner transaction is Pending.
                rollback_ok = False
                report["group_rollback"] = {"status": "unconfirmed", "reason": "inner_transaction_not_closed"}
                try:
                    group.Dispose()
                except Exception as exc:
                    report["group_rollback"]["dispose_error"] = text_type(exc)
            report["rollback"] = report["group_rollback"]
    if transaction is None and group is None:
        return report
    report["status"] = "rollback_unconfirmed"
    if not rollback_ok:
        return report  # No further model reads in possible failure/pending state.
    try:
        after_ids = document_ids(document, DB)
        report["restoration"] = {"added_ids": sorted(after_ids - before_ids),
                                  "removed_ids": sorted(before_ids - after_ids)}
        after_probe = Probe(document, DB)
        _, after = reference_snapshot(after_probe)
        report["restoration"]["reference_unchanged"] = before == after
        report["document"]["is_modified_after"] = bool(document.IsModified)
        restored = before_ids == after_ids and before == after
        report["restoration"]["verified"] = restored
        if not restored:
            report["status"] = "restoration_failed"
        elif (report["issues"] or report.get("comparison", {}).get("status") != "matches"
              or (commit_mode and (report["commit"]["status"] != "committed"
                                   or report.get("post_commit_comparison", {}).get("status") != "matches"))):
            report["status"] = "failed_rolled_back"
        else:
            report["status"] = "passed_rolled_back"
    except Exception as exc:
        report["status"] = "restoration_failed"
        report["issues"].append({"stage": "restoration", "message": text_type(exc)})
    return report
