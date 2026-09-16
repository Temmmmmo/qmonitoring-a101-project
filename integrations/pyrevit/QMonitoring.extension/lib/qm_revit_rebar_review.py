# -*- coding: utf-8 -*-
"""Native whole-party Rebar review with post-Commit readback and explicit keep."""
from __future__ import division, unicode_literals

import copy
import datetime
import json
import math
import time
import traceback

from qm_rebar_review import (REPORT_SCHEMA, VERSION, compare_native, flat_outer_host, make_review_plan,
    manual_axis_depth_policy, validate_axis_depth_policy)
from qm_revit_probe import Probe, element_id, text_type
from qm_revit_trial import create_trial_rebar, document_ids, failure_recorder, read_trial_rebar, rollback_scope
from qm_trial_worksharing import (assign_new_rebar_workset, authorize_trial_worksharing,
    finish_trial_worksharing, inspect_trial_worksharing, ownership_notice, verify_new_rebar_worksets)


def constrain_review_axis(probe, floor, rebar, run, diagnostic):
    """Revit 2024 fixed native-face offsets, exclusively on this NEW Rebar.

    Candidate offsets are calibrated against the native axis, not guessed from
    cover or bar diameter. Missing capabilities/geometry reject the whole party.
    This is placement consistency, not an engineering collision certificate.
    """
    DB = probe.DB
    diagnostic.update({"bar_id":run["bar_id"],"direction":run["direction"],
        "element_id":element_id(rebar.Id),"status":"not_checked","handles":[]})
    manager = None
    try:
        native = read_trial_rebar(probe,rebar)
        bars = native.get("bars", [])
        if len(bars) != 1 or len(bars[0].get("curves", [])) != 1:
            raise ValueError("Constraint calibration requires one native straight axis")
        curve = bars[0]["curves"][0]
        if curve.get("kind") != "Line":
            raise ValueError("Constraint calibration requires a native Line")
        a,b = curve["start_mm"],curve["end_mm"]
        expected = run["axes"][0]
        u,v = expected["start_mm"],expected["end_mm"]
        def distance(p,q):
            return math.sqrt(sum((x-y)**2 for x,y in zip(p,q)))
        if distance(a,v)+distance(b,u) < distance(a,u)+distance(b,v):
            u,v = v,u
        diagnostic["before_endpoint_delta_mm"] = [distance(a,u),distance(b,v)]
        if max(diagnostic["before_endpoint_delta_mm"]) <= 0.01:
            diagnostic["status"] = "native_axis_already_matches_no_constraints_changed"
            return
        manager = rebar.GetRebarConstraintsManager()
        tangent = [(b[i]-a[i])/distance(a,b) for i in range(3)]
        target_tangent = [(v[i]-u[i])/distance(u,v) for i in range(3)]
        if any(abs(x-y) > 1e-8 for x,y in zip(tangent,target_tangent)):
            raise ValueError("Native axis rotated; cannot calibrate straight-bar handles")
        midpoint = [(x+y)/2 for x,y in zip(a,b)]
        wanted_mid = [(x+y)/2 for x,y in zip(u,v)]
        normal = run["normal"]
        edge_normal = [normal[1]*tangent[2]-normal[2]*tangent[1],
            normal[2]*tangent[0]-normal[0]*tangent[2],
            normal[0]*tangent[1]-normal[1]*tangent[0]]
        known = {"StartOfBar":(a,u,tangent),"EndOfBar":(b,v,tangent),
            "RebarPlane":(midpoint,wanted_mid,normal),"Edge":(midpoint,wanted_mid,edge_normal)}
        selected, seen = [], set()
        for handle in manager.GetAllHandles():
            kind = text_type(handle.GetHandleType())
            row = {"type":kind,"candidates":[]}
            diagnostic["handles"].append(row)
            current = manager.GetCurrentConstraintOnHandle(handle)
            row["previous_constraint_type"] = text_type(current.GetConstraintType()) if current is not None else None
            row["previous_target_element_ids"] = [element_id(current.GetTargetElement(i).Id)
                for i in range(current.NumberOfTargets)] if current is not None else []
            if kind == "OutOfPlaneExtent" and current is None:
                row["status"] = "inactive_single_layout_extent_no_external_constraint"
                continue
            if kind not in known or kind in seen or (kind == "Edge" and handle.GetEdgeNumber() != 0):
                raise ValueError("Unsupported or duplicate straight Rebar handle: "+kind)
            seen.add(kind)
            actual_point,wanted_point,movement = known[kind]
            candidates = []
            for candidate in manager.GetConstraintCandidatesForHandle(handle,floor.Id):
                item = {"type":text_type(candidate.GetConstraintType())}
                row["candidates"].append(item)
                if not candidate.IsFixedDistanceToHostFace() or candidate.NumberOfTargets != 1:
                    continue
                reference = candidate.GetTargetHostFaceReference()
                item["target_element_id"] = element_id(reference.ElementId)
                if item["target_element_id"] != element_id(floor.Id):
                    continue
                item["target_face_reference"] = reference.ConvertToStableRepresentation(probe.doc)
                face = floor.GetGeometryObjectFromReference(reference)
                if not isinstance(face,DB.PlanarFace):
                    continue
                face_normal = [face.FaceNormal.X,face.FaceNormal.Y,face.FaceNormal.Z]
                if abs(abs(sum(x*y for x,y in zip(face_normal,movement)))-1) > 1e-8:
                    continue
                origin = [DB.UnitUtils.ConvertFromInternalUnits(value,DB.UnitTypeId.Millimeters)
                    for value in (face.Origin.X,face.Origin.Y,face.Origin.Z)]
                signed = sum((actual_point[i]-origin[i])*face_normal[i] for i in range(3))
                offset = DB.UnitUtils.ConvertFromInternalUnits(candidate.GetDistanceToTargetHostFace(),DB.UnitTypeId.Millimeters)
                item.update({"offset_mm":offset,"axis_to_face_signed_mm":signed})
                if not all(not math.isnan(value) and not math.isinf(value) for value in (signed,offset)):
                    continue
                # A zero distance cannot establish the API's offset sign. Use
                # another genuine face candidate or reject, never guess a sign.
                if abs(signed) < 0.01 or abs(abs(signed)-abs(offset)) > 0.01:
                    continue
                sign = 1 if signed*offset > 0 else -1
                target = offset+sign*sum((wanted_point[i]-actual_point[i])*face_normal[i] for i in range(3))
                candidates.append((abs(offset),candidate,target,item))
            if not candidates:
                raise ValueError("No calibrated fixed selected-Floor face target for "+kind)
            candidates.sort(key=lambda item:item[0])
            _,candidate,target,item = candidates[0]
            row["selected"] = dict(item,expected_offset_mm=target)
            selected.append((kind,target,row))
        if seen != set(known):
            raise ValueError("Incomplete straight Rebar handle inventory")
        for kind,target,row in selected:
            # A previous assignment can invalidate other handles/candidates.
            # Reacquire by exact handle type and native stable FACE reference.
            fresh_handles = [h for h in manager.GetAllHandles()
                if text_type(h.GetHandleType()) == kind and (kind != "Edge" or h.GetEdgeNumber() == 0)]
            if len(fresh_handles) != 1 or not fresh_handles[0].IsValid():
                raise ValueError("Constraint handle invalidated during assignment: "+kind)
            handle = fresh_handles[0]
            fresh_candidates = [c for c in manager.GetConstraintCandidatesForHandle(handle,floor.Id)
                if c.IsValid() and c.IsFixedDistanceToHostFace() and c.NumberOfTargets == 1
                and element_id(c.GetTargetHostFaceReference().ElementId) == element_id(floor.Id)
                and c.GetTargetHostFaceReference().ConvertToStableRepresentation(probe.doc)
                    == row["selected"]["target_face_reference"]]
            if len(fresh_candidates) != 1:
                raise ValueError("Calibrated native face candidate invalidated or ambiguous: "+kind)
            candidate = fresh_candidates[0]
            candidate.SetDistanceToTargetHostFace(DB.UnitUtils.ConvertToInternalUnits(target,DB.UnitTypeId.Millimeters))
            manager.SetPreferredConstraintForHandle(handle,candidate)
            preferred = manager.GetPreferredConstraintOnHandle(handle)
            if preferred is None or not preferred.IsFixedDistanceToHostFace() or preferred.NumberOfTargets != 1:
                raise ValueError("Revit did not retain fixed native-face preference")
            if element_id(preferred.GetTargetHostFaceReference().ElementId) != element_id(floor.Id):
                raise ValueError("Preferred constraint target left the selected Floor")
            if preferred.GetTargetHostFaceReference().ConvertToStableRepresentation(probe.doc) != row["selected"]["target_face_reference"]:
                raise ValueError("Revit changed preferred native face target")
            retained_offset = DB.UnitUtils.ConvertFromInternalUnits(preferred.GetDistanceToTargetHostFace(),DB.UnitTypeId.Millimeters)
            if abs(retained_offset-target) > 0.01:
                raise ValueError("Revit changed preferred native face offset")
            row["status"] = "fixed_selected_floor_preference_assigned"
        diagnostic["status"] = "fixed_preferences_assigned_pending_whole_native_readback"
    finally:
        if manager is not None:
            manager.Dispose()


def _blocking_read_issues(probe, report):
    """Only excluded cover metadata is optional; native geometry/types stay hard."""
    optional = set(("floor/cover/top", "floor/cover/bottom", "floor/cover/other"))
    report["native_read_issues"] = copy.deepcopy(probe.issues)
    report["optional_cover_metadata_issues"] = [copy.deepcopy(row) for row in probe.issues
        if row.get("section") in optional]
    return [row for row in probe.issues if row.get("section") not in optional]


def _read_created(probe, ids, identities):
    rows = []
    DB = probe.DB
    for value in ids:
        rebar = probe.doc.GetElement(DB.ElementId(value))
        if not isinstance(rebar, DB.Structure.Rebar):
            raise ValueError("Created diagnostic Rebar is missing or replaced")
        row = read_trial_rebar(probe, rebar)
        if row.get("element_id") != value:
            raise ValueError("Created diagnostic Rebar element identity differs after readback")
        comment = rebar.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        text = comment.AsString() if comment is not None else None
        if not isinstance(text, text_type) or text != identities[value]:
            raise ValueError("Created diagnostic Rebar identity comment differs")
        parts = json.loads(text[len("QM REBAR REVIEW MVP "):])
        row["review_direction"], row["review_bar_id"] = parts
        rows.append(row)
    return rows


def presentation_deviations(plan, rows, comparison):
    """Allow measured geometry deviations only; never reinterpret a FAIL as PASS."""
    allowed = set(("absolute_native_axis_xyz", "native_length_mm", "complete_physical_count_or_derived_mass"))
    if len(rows) != len(plan["runs"]) or comparison["physical_bar_count"] != len(rows):
        raise ValueError("Presentation requires the complete native inventory")
    if any(not set(issue["checks"]).issubset(allowed) for issue in comparison["issues"]):
        raise ValueError("Presentation cannot keep identity/type/count or API read failures")
    changed, maximum, length_maximum = 0, 0.0, 0.0
    for wanted,actual in zip(plan["runs"],rows):
        curve = actual["bars"][0]["curves"][0]
        a,b = curve["start_mm"],curve["end_mm"]
        values = a+b+[curve["length_mm"]]+[actual["bar_type"][key]
            for key in ("nominal_diameter_mm","model_diameter_mm")]
        if len(a) != 3 or len(b) != 3 or any(math.isnan(value) or math.isinf(value) for value in values):
            raise ValueError("Presentation requires finite native axis/length/diameters")
        def distance(p,q):
            return math.sqrt(sum((x-y)**2 for x,y in zip(p,q)))
        length = distance(a,b)
        if length <= 0 or abs(length-curve["length_mm"]) > 0.01:
            raise ValueError("Native curve length is not internally consistent with its endpoints")
        expected = wanted["axes"][0]
        direct = [distance(a,expected["start_mm"]),distance(b,expected["end_mm"])]
        reverse = [distance(a,expected["end_mm"]),distance(b,expected["start_mm"])]
        delta = max(direct if sum(direct) <= sum(reverse) else reverse)
        length_delta = abs(length-wanted["length_mm"])
        maximum, length_maximum = max(maximum,delta),max(length_maximum,length_delta)
        changed += delta > 0.01 or length_delta > 0.01
    mass = comparison["mass_from_final_axes_kg"]
    if math.isnan(mass) or math.isinf(mass) or mass < 0:
        raise ValueError("Presentation requires a finite derived actual mass")
    return {"status":"deviations_measured" if comparison["status"] == "differs" else "no_axis_deviations_measured",
        "deviating_bar_count":changed,"max_endpoint_delta_mm":maximum,
        "max_length_delta_mm":length_maximum,"actual_mass_from_axes_kg":mass,
        "physical_bar_count":len(rows),"engineering_approval":False,
        "warning":"Presentation model only: native geometry deviations are NOT approved; actual host/cover/collisions remain not_checked"}


def run_rebar_review(document, DB, floor, primitives, bar_types, depths, curve_list_factory,
                     keep_confirmation, copy_confirmed=False, worksharing_consent=False,
                     checkout_id_set_factory=None, preview=None, axis_depth_policy=None, review_mode="strict"):
    """Create every straight bar, Commit/read back, then keep or roll back all."""
    report = {"schema_version":REPORT_SCHEMA,"version":VERSION,
        "created_utc":datetime.datetime.utcnow().isoformat()+"Z","units":"mm",
        "status":"blocked_preflight","mode":"explicit-keep-after-commit-readback",
        "review_mode":review_mode,
        "placement_eligible":False,"engineering_approval":False,"issues":[],"commit_failures":[],
        "axis_constraint_control":{"policy":"new-rebar-fixed-selected-floor-faces/v1",
            "global_settings_changed":False,"maximum_repair_passes":1,
            "engineering_approval":False,"bars":[]},
        "created_element_ids":[],"rollback":{"status":"not_started"},
        "source_schema":primitives.get("input_schema"),"case_id":primitives.get("case_id"),
        "axis_depth_policy":copy.deepcopy(axis_depth_policy),"axis_depths_mm":copy.deepcopy(depths),
        "computed_axis_depths_mm":copy.deepcopy(axis_depth_policy.get("computed_depths_mm")) if axis_depth_policy else None,
        "axis_depth_revalidation":{"status":"not_checked"},
        "source_checks":copy.deepcopy(primitives.get("trim_graphics",{}).get("checks")),
        "source_blockers":copy.deepcopy(primitives.get("source_blockers")),
        "not_checked":["holes","cover","height-steps","background-rebar-collisions", "collisions-at-user-selected-four-axis-depths",
            "engineering-coverage-acceptance","construction-release","automatic-save-or-sync"],
        "mvp_scope":"selected native flat Floor outer contour and thickness; holes/cover excluded; diagnostic straight Rebar review only"}
    started = time.time()
    report["batch_limit"] = {"maximum_physical_bar_count":5000,
        "duration_limit_seconds":None,"duration_policy":"measured and reported; no hidden timeout inside a Revit transaction"}
    probe = Probe(document,DB)
    transaction,group,before_ids,before,worksharing = None,None,None,None,None
    ended,stage,kept = False,"preflight",False
    identities = {}
    try:
        if review_mode not in ("strict", "presentation"):
            raise ValueError("Unknown Rebar review mode")
        if axis_depth_policy is None:
            axis_depth_policy = manual_axis_depth_policy(depths)
            axis_depth_policy["user_confirmed"] = copy_confirmed is True
            report["axis_depth_policy"] = copy.deepcopy(axis_depth_policy)
        if copy_confirmed is not True:
            raise ValueError("Explicit confirmation of a disposable/local COPY is required")
        if (document is None or document.IsFamilyDocument or document.IsReadOnly or document.IsModifiable
                or text_type(document.Application.VersionNumber) != "2024"):
            raise ValueError("Open a writable Revit 2024 project COPY with no active transaction")
        if not isinstance(floor,DB.Floor) or floor.Document != document:
            raise ValueError("Select exactly one native Floor from the active document")
        before = probe.floor(floor)
        report["host_before"] = copy.deepcopy(before)
        report["host_policy"] = flat_outer_host(before)
        types = {}
        for key,item in bar_types.items():
            if not isinstance(item,DB.Structure.RebarBarType) or item.Document != document:
                raise ValueError("Selected RebarBarTypes must belong to the active model")
            types[key] = probe.bar_type(item)
        if _blocking_read_issues(probe,report):
            raise ValueError("Incomplete native host/type readback")
        report["bar_type_mapping"] = copy.deepcopy(types)
        validate_axis_depth_policy(primitives,report["host_policy"],types,depths,axis_depth_policy)
        plan = make_review_plan(primitives,report["host_policy"],types,depths)
        report["expected"] = copy.deepcopy(plan["expected"])
        report["axis_depths_mm"] = copy.deepcopy(depths)
        report["tolerance_mm"] = plan["tolerance_mm"]
        report["derived_mass_formula"] = plan["mass_formula"]
        report["timing_seconds"] = {"preflight_and_plan":time.time()-started}
        worksharing = inspect_trial_worksharing(document,DB,floor,list(bar_types.values()))
        report["worksharing"] = worksharing
        if worksharing["status"] == "blocked":
            raise ValueError("Worksharing preflight: "+"; ".join(worksharing["issues"]))
        stage = "worksharing_authorization"
        authorize_trial_worksharing(document,DB,floor,list(bar_types.values()),worksharing,
            worksharing_consent,checkout_id_set_factory)
        fresh_floor = probe.floor(floor)
        fresh_types = dict((key,probe.bar_type(item)) for key,item in bar_types.items())
        if fresh_floor != before or fresh_types != types:
            raise ValueError("Host/type changed before creation; recompute whole review")
        if _blocking_read_issues(probe,report):
            raise ValueError("Incomplete native host/type reread before creation")
        validate_axis_depth_policy(primitives,flat_outer_host(fresh_floor),fresh_types,depths,axis_depth_policy)
        report["axis_depth_revalidation"] = {"status":"consistent_with_current_native_host_and_types",
            "mode":axis_depth_policy["mode"],"engineering_approval":False}
        before_ids = document_ids(document,DB)
        stage = "transaction_group"
        group = DB.TransactionGroup(document,"QMonitoring REBAR REVIEW MVP - NOT FOR CONSTRUCTION")
        group.IsFailureHandlingForcedModal = True
        if group.Start() != DB.TransactionStatus.Started:
            raise ValueError("Rebar review group did not start")
        stage = "transaction"
        transaction = DB.Transaction(document,"QMonitoring whole straight party - REVIEW ONLY")
        if transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError("Rebar review transaction did not start")
        recorder = failure_recorder(DB,report["commit_failures"])
        options = transaction.GetFailureHandlingOptions().SetClearAfterRollback(True)
        transaction.SetFailureHandlingOptions(options.SetForcedModalHandling(True).SetFailuresPreprocessor(recorder))
        stage = "create_whole_party"
        for run in plan["runs"]:
            try:
                rebar = create_trial_rebar(probe,floor,bar_types[run["material_key"]],run,curve_list_factory)
            except ValueError as exc:
                if "existing compatible straight RebarShape" in text_type(exc):
                    raise ValueError("Rebar Review requires an existing compatible straight RebarShape; it never creates a new shape")
                raise
            value = element_id(rebar.Id)
            report["created_element_ids"].append(value)
            assign_new_rebar_workset(document,DB,rebar,worksharing)
            comment = rebar.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            identity = "QM REBAR REVIEW MVP "+json.dumps(
                [run["direction"],run["bar_id"]],ensure_ascii=False,separators=(",",":"))
            identities[value] = identity
            if comment is None or comment.IsReadOnly or not comment.Set(identity):
                raise ValueError("Cannot bind created Rebar to its exact source bar ID")
        report["timing_seconds"]["authorization_and_create"] = time.time()-started-report["timing_seconds"]["preflight_and_plan"]
        if len(report["created_element_ids"]) != len(plan["runs"]):
            raise ValueError("Incomplete native party creation")
        document.Regenerate()
        if review_mode == "strict":
            stage = "constrain_new_axes"
            for value,run in zip(report["created_element_ids"],plan["runs"]):
                constraint_diagnostic = {}
                report["axis_constraint_control"]["bars"].append(constraint_diagnostic)
                constrain_review_axis(probe,floor,document.GetElement(DB.ElementId(value)),run,constraint_diagnostic)
            document.Regenerate()
        else:
            report["axis_constraint_control"]["status"] = "skipped_explicit_presentation_mode"
        controls = report["axis_constraint_control"]
        controls["fixed_preferences_assigned_count"] = sum(row["status"] == "fixed_preferences_assigned_pending_whole_native_readback"
            for row in controls["bars"])
        controls["unchanged_bar_count"] = len(controls["bars"])-controls["fixed_preferences_assigned_count"]
        stage = "pre_commit_readback"
        report["pre_commit_readback"] = _read_created(probe,report["created_element_ids"],identities)
        verify_new_rebar_worksets(document,DB,report["created_element_ids"],worksharing,"pre_commit")
        report["pre_commit_comparison"] = compare_native(plan,report["pre_commit_readback"])
        report["timing_seconds"]["pre_commit_readback_cumulative"] = time.time()-started
        if review_mode == "presentation":
            report["pre_commit_presentation_deviations"] = presentation_deviations(plan,report["pre_commit_readback"],report["pre_commit_comparison"])
        if (review_mode == "strict" and report["pre_commit_comparison"]["status"] != "matches") or _blocking_read_issues(probe,report):
            raise ValueError("Whole-party native readback differs before Commit")
        stage = "commit"
        returned = transaction.Commit()
        final = transaction.GetStatus()
        report["commit"] = {"returned":text_type(returned),"final":text_type(final)}
        if returned != DB.TransactionStatus.Committed or final != returned or report["commit_failures"]:
            raise ValueError("Commit failed or posted a warning/error; whole party is rejected")
        ended = True
        if review_mode == "presentation" and preview is not None:
            stage = "optional_presentation_view"
            try:
                preview(report["created_element_ids"])
            except Exception as exc:
                if getattr(exc,"rollback_unconfirmed",False) or document.IsModifiable:
                    raise
                report["preview_error"] = {"message":text_type(exc),"traceback":traceback.format_exc()}
            if document.IsModifiable:
                raise ValueError("Presentation callback left an active transaction; cannot keep party")
        stage = "post_commit_readback"
        report["post_commit_readback"] = _read_created(probe,report["created_element_ids"],identities)
        verify_new_rebar_worksets(document,DB,report["created_element_ids"],worksharing,"post_commit")
        report["post_commit_comparison"] = compare_native(plan,report["post_commit_readback"])
        report["timing_seconds"]["post_commit_readback_cumulative"] = time.time()-started
        if review_mode == "presentation":
            report["presentation_deviations"] = presentation_deviations(plan,report["post_commit_readback"],report["post_commit_comparison"])
        if (review_mode == "strict" and report["post_commit_comparison"]["status"] != "matches") or _blocking_read_issues(probe,report):
            raise ValueError("Whole-party native readback differs after Commit")
        if probe.floor(floor) != before or any(probe.bar_type(item) != types[key] for key,item in bar_types.items()):
            raise ValueError("Host/type changed after Commit; diagnostic party cannot be kept")
        if _blocking_read_issues(probe,report):
            raise ValueError("Incomplete native host/type reread after Commit")
        if review_mode == "strict" and preview is not None:
            preview(report["created_element_ids"])
        stage = "explicit_keep_confirmation"
        keep = keep_confirmation(report) if callable(keep_confirmation) else keep_confirmation
        report["keep_confirmation_received"] = keep is True
        if keep is not True:
            raise ValueError("Diagnostic Rebar was not explicitly kept; rolling back the whole party")
        stage = "assimilate_group"
        returned = group.Assimilate()
        final = group.GetStatus()
        report["assimilation"] = {"returned":text_type(returned),"final":text_type(final)}
        if returned != DB.TransactionStatus.Committed or final != returned:
            raise ValueError("TransactionGroup assimilation was not confirmed")
        group.Dispose()
        group = None
        kept = True
        report["status"] = "kept_diagnostic_rebar_review"
        if review_mode == "presentation" and report["post_commit_comparison"]["status"] == "differs":
            report["status"] = "kept_presentation_rebar_with_deviations"
        report["kept_element_ids"] = list(report["created_element_ids"])
        report["keep_notice"] = "Diagnostic Rebar retained in this COPY only; not engineering approval; model was not saved or synchronized"
    except Exception as exc:
        report["issues"].append({"stage":stage,"message":text_type(exc),"traceback":traceback.format_exc()})
    finally:
        report.setdefault("timing_seconds", {})["total"] = time.time()-started
        if transaction is not None and not ended:
            ended,report["inner_end"] = rollback_scope(transaction,DB)
        elif transaction is not None:
            try:
                transaction.Dispose()
            except Exception as exc:
                ended = False
                report["issues"].append({"stage":"transaction_dispose","message":text_type(exc)})
        if group is not None:
            if ended:
                ended,report["rollback"] = rollback_scope(group,DB)
            else:
                report["rollback"] = {"status":"unconfirmed","reason":"inner_transaction_not_closed"}
                try:
                    group.Dispose()
                except Exception as exc:
                    report["rollback"]["dispose_error"] = text_type(exc)
        if worksharing is not None:
            finish_trial_worksharing(document,DB,floor,list(bar_types.values()),worksharing,
                may_read=kept or (group is None and transaction is None) or ended)
            report["ownership_notice"] = ("ВНИМАНИЕ: диагностические Rebar оставлены в локальной модели; "
                "владение Floor/связанными элементами в central могло остаться. Автоматических "
                "Save/Sync/Relinquish нет; проверь владение вручную."
                if kept and worksharing.get("mode") == "live_local" else ownership_notice(worksharing))
    if kept:
        return report
    if transaction is not None or group is not None:
        report["status"] = "rollback_unconfirmed"
        if not ended:
            return report
        try:
            after_ids = document_ids(document,DB)
            after_probe = Probe(document,DB)
            after = after_probe.floor(document.GetElement(DB.ElementId(before["element_id"])))
            restored = before_ids == after_ids and before == after and not _blocking_read_issues(after_probe,report)
            report["restoration"] = {"verified":restored,"added_ids":sorted(after_ids-before_ids),
                "removed_ids":sorted(before_ids-after_ids),"host_unchanged":before==after}
            report["status"] = "failed_rolled_back" if restored else "restoration_failed"
        except Exception as exc:
            report["status"] = "restoration_failed"
            report["issues"].append({"stage":"restoration","message":text_type(exc)})
    return report
