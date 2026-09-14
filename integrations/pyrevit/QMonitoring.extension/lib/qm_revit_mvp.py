# -*- coding: utf-8 -*-
"""Actual Rebar in a NEW explicitly named demo copy; production gates stay closed."""
from __future__ import division

import datetime
import hashlib
import os
import traceback

from qm_revit_probe import Probe, element_id, text_type
from qm_mvp_packet import MVP_VERSION, WARNING, compare_demo_readback, make_demo_plan, mesh_signature, validate_packet
from qm_core_trial import same
from qm_revit_cad import linked_source_file, read_cad_geometry
from qm_revit_trial import (create_trial_rebar, document_ids, failure_recorder, find_obstacles,
                            read_core_sets, reference_snapshot, rollback_scope, solid_report)
from qm_trial_geometry import validate_prism


def file_digest(path):
    if not os.path.isfile(path) or os.path.getsize(path) > 1024 * 1024 * 1024:
        raise ValueError("Expected an existing local RVT under 1 GiB")
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            result.update(block)
    return result.hexdigest()


def validate_copy_path(document, destination):
    source = text_type(document.PathName)
    if not source or not os.path.isabs(source) or not os.path.isfile(source):
        raise ValueError("Open the saved local reference RVT")
    if os.path.basename(source).upper().startswith("QM_DEMO_"):
        raise ValueError("Do not add a second demo to a demo model; reopen the reference RVT")
    if (not os.path.isabs(destination) or os.path.splitext(destination)[1].lower() != ".rvt"
            or not os.path.basename(destination).upper().startswith("QM_DEMO_")
            or not os.path.isdir(os.path.dirname(destination)) or os.path.exists(destination)
            or os.path.normcase(os.path.abspath(source)) == os.path.normcase(os.path.abspath(destination))):
        raise ValueError("Choose a NEW local QM_DEMO_*.rvt file, never an existing model")
    return source


def live_preflight(document, DB, packet):
    probe = Probe(document, DB)
    floor, reference = reference_snapshot(probe)
    validate_prism(reference["floor"], solid_report(probe, floor))
    types, type_data = {}, {}
    for diameter, row in packet["host"]["bar_types"].items():
        element = document.GetElement(DB.ElementId(row["element_id"]))
        if not isinstance(element, DB.Structure.RebarBarType):
            raise ValueError("Missing RebarBarType")
        types[diameter], type_data[diameter] = element, probe.bar_type(element)
    plan = make_demo_plan(packet, reference["floor"], type_data)
    cad = document.GetElement(DB.ElementId(packet["cad"]["element_id"]))
    if not isinstance(cad, DB.ImportInstance) or not cad.IsLinked or text_type(cad.UniqueId) != packet["cad"]["unique_id"]:
        raise ValueError("The source CAD link is missing/replaced")
    same(probe.transform(cad.GetTransform()), packet["cad"]["instance_transform"])
    disk = linked_source_file(probe, cad)
    if disk.get("status") != "read" or disk["sha256"] != packet["source_sha256"]["dxf"]:
        raise ValueError("Linked DXF is unavailable or changed; no cached fallback")
    signatures = [mesh_signature(read_cad_geometry(probe, cad, mode)) for mode in ("symbol", "instance")]
    if any(value != packet["cad"]["mesh_signature"] for value in signatures):
        raise ValueError("Live CAD mesh differs from the verified source snapshot")
    if probe.issues:
        raise ValueError("Live reference/type reading is incomplete")
    floor_box = reference["floor"]["bbox_mm"]
    reservation = {"host_id": packet["host"]["element_id"], "reservation_mm": {
        "min_mm": [v-100 for v in floor_box["min_mm"]], "max_mm": [v+100 for v in floor_box["max_mm"]]}}
    audit = {}
    obstacles = find_obstacles(probe, reservation, audit)
    # Only the previously measured laboratory example is excluded. It remains in
    # the copy (never deleted), hidden in the new demo view. NOT a production rule.
    fixture_ids = set([reference["area"]["element_id"]] + reference["area"]["rebar_in_system_ids"])
    excluded = [row for row in obstacles if row["element_id"] in fixture_ids]
    blocking = [row for row in obstacles if row["element_id"] not in fixture_ids]
    if blocking:
        raise ValueError("Other existing physical/unclassified geometry occupies the slab: " +
                         ", ".join(str(row["element_id"]) for row in blocking[:20]))
    report = {"live_cad_snapshot": signatures[0], "source_file": disk,
              "host_solid": "six_face_prism_verified", "reference_fixture_excluded_from_demo": excluded,
              "reference_fixture_deleted": False, "obstacle_audit": audit,
              "existing_reference_bar_collisions": "not_checked_demo_fixture_only",
              "engineering_approved": False}
    return probe, floor, types, reference, plan, report, fixture_ids


def create_demo_view(document, DB, floor, created_ids, fixture_ids, ids_factory, label):
    candidates = [t for t in DB.FilteredElementCollector(document).OfClass(DB.ViewFamilyType)
                  .WhereElementIsElementType() if t.ViewFamily == DB.ViewFamily.ThreeDimensional]
    if not candidates:
        raise ValueError("No 3D ViewFamilyType in the test model")
    view = DB.View3D.CreateIsometric(document, candidates[0].Id)
    view.Name = "QM DEMO - NOT FOR CONSTRUCTION - " + label
    view.DetailLevel = DB.ViewDetailLevel.Fine
    view.IsSectionBoxActive = True
    box = floor.get_BoundingBox(None)
    margin = DB.UnitUtils.ConvertToInternalUnits(200, DB.UnitTypeId.Millimeters)
    section = DB.BoundingBoxXYZ()
    section.Min = DB.XYZ(box.Min.X-margin, box.Min.Y-margin, box.Min.Z-margin)
    section.Max = DB.XYZ(box.Max.X+margin, box.Max.Y+margin, box.Max.Z+margin)
    view.SetSectionBox(section)
    view.SetElementOverrides(floor.Id, DB.OverrideGraphicSettings().SetSurfaceTransparency(80))
    hidden = ids_factory()
    for identifier in fixture_ids:
        element = document.GetElement(DB.ElementId(identifier))
        if element is not None and element.CanBeHidden(view):
            hidden.Add(element.Id)
    if hidden.Count:
        view.HideElements(hidden)
    for identifier in created_ids:
        bar = document.GetElement(DB.ElementId(identifier))
        bar.SetUnobscuredInView(view, True)
    return view


def run_mvp_demo(document, DB, curve_list_factory, ids_factory, packet, copy_path,
                 demo_confirmed=False, activate_view=None):
    report = {"schema_version": "qmonitoring-revit-demo-result/v1", "version": MVP_VERSION,
              "warning": WARNING, "placement_eligible": False, "engineering_approved": False,
              "status": "blocked_preflight", "copy_created": False, "demo_saved": False,
              "commit": {"status": "not_started"}, "group": {"status": "not_started"},
              "issues": [], "commit_failures": [], "created_element_ids": []}
    stage, group, transaction, baseline_ids, source, source_hash = "preflight", None, None, None, None, None
    inner_closed, kept = True, False
    try:
        validate_packet(packet)
        if not demo_confirmed:
            raise ValueError("Explicit DEMO confirmation required")
        if (document is None or document.IsFamilyDocument or document.IsWorkshared or document.IsReadOnly
                or document.IsModifiable or document.IsModified):
            raise ValueError("Open an unmodified saved non-workshared reference project with no transaction")
        if text_type(document.Application.VersionNumber) != "2024":
            raise ValueError("This MVP targets Revit 2024 only")
        source = validate_copy_path(document, copy_path)
        source_hash = file_digest(source)
        report.update(source_path=source, copy_path=copy_path, packet_scope=packet["scope"], hypotheses=packet["hypotheses"])
        live_preflight(document, DB, packet)  # No file/model write until the live reference is checked.
        stage = "save_new_copy"
        options = DB.SaveAsOptions()
        try:
            options.OverwriteExistingFile = False
            document.SaveAs(copy_path, options)
        finally:
            options.Dispose()
        if os.path.normcase(os.path.abspath(text_type(document.PathName))) != os.path.normcase(os.path.abspath(copy_path)):
            raise ValueError("SaveAs did not switch the active document to the new demo copy")
        report["copy_created"] = True
        stage = "copy_preflight"
        probe, floor, types, before, plan, preflight, fixtures = live_preflight(document, DB, packet)
        report.update(preflight=preflight, expected=packet["expected"], actual_run_count=len(plan["runs"]))
        baseline_ids = document_ids(document, DB)
        group = DB.TransactionGroup(document, WARNING)
        group.IsFailureHandlingForcedModal = True
        if group.Start() != DB.TransactionStatus.Started:
            raise ValueError("Demo transaction group did not start")
        stage = "create"
        transaction = DB.Transaction(document, WARNING)
        if transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError("Demo transaction did not start")
        inner_closed = False
        recorder = failure_recorder(DB, report["commit_failures"])
        opts = transaction.GetFailureHandlingOptions().SetClearAfterRollback(True).SetForcedModalHandling(True).SetFailuresPreprocessor(recorder)
        transaction.SetFailureHandlingOptions(opts)
        for run in plan["runs"]:
            bar = create_trial_rebar(probe, floor, types[str(run["diameter_mm"])], run, curve_list_factory)
            report["created_element_ids"].append(element_id(bar.Id))
            parameter = bar.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if parameter is None or parameter.IsReadOnly or not parameter.Set(WARNING + " / " + run["zone_id"]):
                raise ValueError("Cannot mark generated Rebar as DEMO")
        document.Regenerate()
        readback = read_core_sets(probe, report["created_element_ids"])
        report["pre_commit_comparison"] = compare_demo_readback(plan, readback)
        if report["pre_commit_comparison"]["status"] != "matches":
            raise ValueError("Pre-commit axes differ")
        stage = "view"
        view = create_demo_view(document, DB, floor, report["created_element_ids"], fixtures, ids_factory,
                                datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S-%f"))
        report["view_id"] = element_id(view.Id)
        stage = "commit"
        returned = transaction.Commit()
        inner_closed = returned == DB.TransactionStatus.Committed and transaction.GetStatus() == returned
        report["commit"] = {"status": text_type(returned)}
        if not inner_closed or report["commit_failures"]:
            raise ValueError("Commit failed or reported warnings")
        stage = "post_commit_readback"
        report["readback"] = read_core_sets(probe, report["created_element_ids"])
        report["comparison"] = compare_demo_readback(plan, report["readback"])
        if report["comparison"]["status"] != "matches":
            raise ValueError("Revit changed final geometry; rolling the demo back")
        _, after_reference = reference_snapshot(probe)
        if after_reference != before:
            raise ValueError("Reference slab/example changed during creation")
        transaction.Dispose()
        transaction = None
        stage = "keep_demo_in_copy"
        returned = group.Assimilate()
        kept = returned == DB.TransactionStatus.Committed and group.GetStatus() == returned
        report["group"] = {"status": "committed_demo_copy_only" if kept else "unconfirmed"}
        if not kept:
            raise ValueError("Demo group did not finish synchronously")
        group.Dispose()
        group = None
        if activate_view is not None:
            try:
                activate_view(view)
            except Exception as exc:
                report["view_activation_warning"] = text_type(exc)
        stage = "save_demo_copy"
        if os.path.normcase(os.path.abspath(text_type(document.PathName))) != os.path.normcase(os.path.abspath(copy_path)):
            raise ValueError("Active document path changed; refusing Save")
        document.Save()
        report["comparison_after_save"] = compare_demo_readback(plan, read_core_sets(probe, report["created_element_ids"]))
        if report["comparison_after_save"]["status"] != "matches" or document.IsModified:
            raise ValueError("Saved demo readback or modification state differs")
        report["demo_saved"] = True
        report["status"] = "demo_saved_and_readback_matches"
    except Exception as exc:
        report["issues"].append({"stage": stage, "message": text_type(exc), "traceback": traceback.format_exc()})
        if kept:
            report["status"] = "demo_kept_but_save_or_verification_failed"
    finally:
        if transaction is not None:
            inner_closed, report["transaction_cleanup"] = rollback_scope(transaction, DB, allow_committed=True)
        if group is not None:
            if inner_closed:
                _, report["group"] = rollback_scope(group, DB)
            else:
                report["group"] = {"status": "unconfirmed", "reason": "inner_transaction_not_closed"}
        if baseline_ids is not None and not kept:
            try:
                restored = document_ids(document, DB) == baseline_ids
                _, restored_reference = reference_snapshot(Probe(document, DB))
                restored = restored and restored_reference == before
                report["restoration_verified"] = restored and report["group"]["status"] == "rolled_back"
                report["status"] = "failed_demo_rolled_back" if report["restoration_verified"] else "rollback_unconfirmed"
            except Exception as exc:
                report["status"] = "rollback_unconfirmed"
                report["issues"].append({"stage": "restoration", "message": text_type(exc)})
        if source_hash is not None:
            try:
                report["source_file_unchanged"] = file_digest(source) == source_hash
                if not report["source_file_unchanged"]:
                    report["status"] = "source_file_change_detected"
            except Exception as exc:
                report["source_file_unchanged"] = None
                report["issues"].append({"stage": "source_fingerprint", "message": text_type(exc)})
                report["status"] = "source_file_integrity_unconfirmed"
    return report
