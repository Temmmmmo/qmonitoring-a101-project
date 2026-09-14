# -*- coding: utf-8 -*-
"""Whole-plate native Rebar trial; never saves or retains document changes.

Source engineering blockers remain blockers even when every API readback matches.
No test-model element IDs, automatic placement profile or partial batch acceptance.
"""
from __future__ import division

import copy
import datetime
import traceback

from qm_plate_packet import VERSION, compare_readback, make_plan, material_key
from qm_revit_probe import Probe, element_id, text_type
from qm_revit_trial import (create_trial_rebar, document_ids, failure_recorder,
                            read_core_sets, rollback_scope)
from qm_trial_worksharing import (assign_new_rebar_workset, authorize_trial_worksharing,
    finish_trial_worksharing, inspect_trial_worksharing, ownership_notice, verify_new_rebar_worksets)


def preserve_setup_host_snapshot(report, setup_report):
    """Fill missing setup evidence without replacing a native execution snapshot.

    A different native host id also forbids borrowing the setup solid/issues.
    Explicit native values (including None/empty issues) are never overwritten.
    """
    native_host = report.get("host")
    if "host_id" not in report and isinstance(native_host, dict) and "element_id" in native_host:
        report["host_id"] = native_host["element_id"]
    setup_host = setup_report.get("host")
    setup_id = setup_report.get("host_id", setup_host.get("element_id") if isinstance(setup_host, dict) else None)
    if "host_id" in report and report["host_id"] != setup_id:
        return report
    for key in ("host", "host_id", "read_issues", "host_solid"):
        if key not in report and key in setup_report:
            report[key] = copy.deepcopy(setup_report[key])
    return report


def check_host_geometry(probe, floor, plan, loop_list_factory):
    """Conservative envelopes against the actual solid, including voids/openings.

    Square envelopes overestimate round bars, so rejection can be conservative.
    Containment does not validate anchorage, background, collisions or layer order.
    """
    DB = probe.DB
    options = DB.Options()
    options.DetailLevel = DB.ViewDetailLevel.Fine
    geometry = floor.get_Geometry(options)
    solids = []
    for item in geometry:
        if isinstance(item, DB.GeometryInstance):
            raise ValueError("Nested floor geometry is unsupported in this trial")
        if isinstance(item, DB.Solid) and item.Volume > 0:
            solids.append(item)
    if len(solids) != 1:
        raise ValueError("Expected one positive-volume host solid")
    host = solids[0]
    covers, host_box = plan["host"]["covers"], plan["host"]["bbox_mm"]
    violations, outside_count = [], 0

    def point(values):
        return DB.XYZ(*[DB.UnitUtils.ConvertToInternalUnits(v, DB.UnitTypeId.Millimeters) for v in values])

    for bar in plan["bars"]:
        box = bar["body_bbox_mm"]
        lo = [box["min_mm"][i] - covers["other" if i < 2 else "bottom"]["distance_mm"] for i in range(3)]
        hi = [box["max_mm"][i] + covers["other" if i < 2 else "top"]["distance_mm"] for i in range(3)]
        reason, outside_mm3 = None, None
        if any(lo[i] < host_box["min_mm"][i] - 0.01 or hi[i] > host_box["max_mm"][i] + 0.01 for i in range(3)):
            reason = "outside_host_bounding_box_with_cover"
        else:
            loop, envelope, difference = DB.CurveLoop(), None, None
            try:
                points = [(lo[0], lo[1], lo[2]), (hi[0], lo[1], lo[2]),
                          (hi[0], hi[1], lo[2]), (lo[0], hi[1], lo[2])]
                for a, b in zip(points, points[1:] + points[:1]):
                    loop.Append(DB.Line.CreateBound(point(a), point(b)))
                loops = loop_list_factory()
                loops.Add(loop)
                envelope = DB.GeometryCreationUtilities.CreateExtrusionGeometry(loops, DB.XYZ.BasisZ,
                    DB.UnitUtils.ConvertToInternalUnits(hi[2] - lo[2], DB.UnitTypeId.Millimeters))
                difference = DB.BooleanOperationsUtils.ExecuteBooleanOperation(envelope, host, DB.BooleanOperationsType.Difference)
                outside_mm3 = float(difference.Volume) * probe.mm(1.0) ** 3
                if outside_mm3 > 0.1:
                    reason = "outside_solid_or_inside_opening_with_cover"
            finally:
                for temporary in (difference, envelope, loop):
                    if temporary is not None:
                        temporary.Dispose()
        if reason:
            outside_count += 1
            if len(violations) < 100:
                violations.append({"run_id": bar["run_id"], "direction": bar["direction"],
                    "position_index": bar["position_index"], "reason": reason,
                    "envelope_mm": {"min_mm": lo, "max_mm": hi}, "outside_volume_mm3": outside_mm3})
    return {"status": "blocked" if outside_count else "contained_conservative_envelopes",
        "checked_physical_bars": len(plan["bars"]), "outside_count": outside_count,
        "examples": violations, "examples_limit": 100,
        "scope": "conservative square bar envelopes with cover versus actual host solid; not anchorage/collision approval"}


def run_plate_trial(document, DB, floor, packet, bar_types, placement, curve_list_factory,
                    loop_list_factory, copy_confirmed=False, preview=None, view=None,
                    worksharing_consent=False, checkout_id_set_factory=None):
    report = {"schema_version": "revit-full-plate-trial-report/v1", "version": VERSION,
        "created_utc": datetime.datetime.utcnow().isoformat() + "Z", "units": "mm",
        "status": "blocked_preflight", "mode": "commit-readback-rollback", "placement_eligible": False,
        "source_report_sha256": packet.get("source_report_sha256"), "source_blockers": packet.get("source_blockers"),
        "placement": placement, "expected": packet.get("expected"), "issues": [], "commit_failures": [],
        "temporary_element_ids": [], "rollback": {"status": "not_started"},
        "not_checked": ["source-DXF-to-host-calibration", "background-and-additions-3d-collisions",
                        "xy-layer-order-engineering-approval", "phase-depth-contact-compatibility",
                        "anchorage-and-demand-engineering-acceptance", "stock-cutting", "permanent-placement"]}
    probe = Probe(document, DB)
    report["read_issues"] = probe.issues
    transaction, group, before_ids, before = None, None, None, None
    ended, stage, worksharing = False, "preflight", None
    try:
        if not copy_confirmed or placement.get("confirmed") is not True:
            raise ValueError("Explicit COPY, coordinate and depth confirmation is required")
        if (document.IsFamilyDocument or document.IsReadOnly or document.IsModifiable
                or text_type(document.Application.VersionNumber) != "2024"):
            raise ValueError("Open a writable Revit 2024 project COPY with no active transaction")
        if not isinstance(floor, DB.Floor) or floor.Document != document:
            raise ValueError("Select a floor in the active document")
        report["host_id"] = element_id(floor.Id)
        before = probe.floor(floor)
        # Retain the actual preflight read even when types, covers or placement
        # validation reject the plan before any transaction is opened.
        report["host"] = before
        types = {}
        for key, item in bar_types.items():
            if not isinstance(item, DB.Structure.RebarBarType) or item.Document != document:
                raise ValueError("Bar types must belong to the active model")
            types[key] = probe.bar_type(item)
        if probe.issues:
            raise ValueError("Incomplete host/type readback")
        worksharing = inspect_trial_worksharing(document, DB, floor, list(bar_types.values()))
        report["worksharing"] = worksharing
        if worksharing["status"] == "blocked":
            raise ValueError("Worksharing preflight: " + "; ".join(worksharing["issues"]))
        plan = make_plan(packet, before, types, placement)
        plan["host"] = before
        report["bar_type_mapping"] = types
        report["geometry_check"] = check_host_geometry(probe, floor, plan, loop_list_factory)
        if report["geometry_check"]["status"] != "contained_conservative_envelopes":
            raise ValueError("Whole batch blocked: bars/cover outside the actual host or in openings; nothing clipped or omitted")
        stage = "worksharing_authorization"
        authorize_trial_worksharing(document, DB, floor, list(bar_types.values()), worksharing,
            worksharing_consent, checkout_id_set_factory)
        if worksharing["mode"] == "live_local":
            # Checkout can refresh cached model state. Never execute a pre-checkout plan
            # against a changed host/type, even when cached ownership looks current.
            if probe.floor(floor) != before or any(probe.bar_type(item) != types[key] for key, item in bar_types.items()):
                raise ValueError("Host/type changed during checkout; recompute the complete trial before creating Rebar")
            if probe.issues:
                raise ValueError("Incomplete host/type reread after checkout")
        before_ids = document_ids(document, DB)
        stage = "transaction_group"
        group = DB.TransactionGroup(document, "QMonitoring FULL PLATE - ALWAYS ROLLBACK")
        group.IsFailureHandlingForcedModal = True
        if group.Start() != DB.TransactionStatus.Started:
            raise ValueError("Rollback group did not start")
        stage = "transaction"
        transaction = DB.Transaction(document, "QMonitoring all four directions - NOT FOR CONSTRUCTION")
        if transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError("Rebar transaction did not start")
        recorder = failure_recorder(DB, report["commit_failures"])
        options = transaction.GetFailureHandlingOptions().SetClearAfterRollback(True)
        options = options.SetForcedModalHandling(True).SetFailuresPreprocessor(recorder)
        transaction.SetFailureHandlingOptions(options)
        stage = "create_all_runs"
        for run in plan["runs"]:
            if worksharing["mode"] == "live_local":
                run["allow_new_shape"] = False  # Never create shared RebarShape types in live-local mode.
            rebar = create_trial_rebar(probe, floor, bar_types[material_key(run)], run, curve_list_factory)
            report["temporary_element_ids"].append(element_id(rebar.Id))
            assign_new_rebar_workset(document, DB, rebar, worksharing)
            parameter = rebar.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if parameter is None or parameter.IsReadOnly or not parameter.Set("QM FULL PLATE TRIAL - NOT FOR CONSTRUCTION | " + run["id"]):
                raise ValueError("Cannot label a temporary Rebar run")
            if view is not None and not document.IsWorkshared:
                rebar.SetUnobscuredInView(view, True)
        if view is not None and not document.IsWorkshared:
            # Only the selected host in this existing view; group rollback restores overrides.
            overrides = view.GetElementOverrides(floor.Id)
            overrides.SetSurfaceTransparency(85)
            view.SetElementOverrides(floor.Id, overrides)
        document.Regenerate()
        stage = "pre_commit_readback"
        report["readback"] = read_core_sets(probe, report["temporary_element_ids"])
        verify_new_rebar_worksets(document, DB, report["temporary_element_ids"], worksharing, "pre_commit")
        report["comparison"] = compare_readback(plan, report["readback"])
        if report["comparison"]["status"] != "matches" or probe.issues:
            raise ValueError("Whole-batch readback differs before Commit")
        stage = "commit"
        returned = transaction.Commit()
        report["commit"] = {"returned": text_type(returned), "final": text_type(transaction.GetStatus())}
        if returned != DB.TransactionStatus.Committed or transaction.GetStatus() != returned or report["commit_failures"]:
            raise ValueError("Commit did not finish cleanly; whole batch is rejected")
        stage = "post_commit_readback"
        report["post_commit_readback"] = read_core_sets(probe, report["temporary_element_ids"])
        verify_new_rebar_worksets(document, DB, report["temporary_element_ids"], worksharing, "post_commit")
        report["post_commit_comparison"] = compare_readback(plan, report["post_commit_readback"])
        if report["post_commit_comparison"]["status"] != "matches" or probe.issues:
            raise ValueError("Whole-batch readback differs after Commit")
        if preview is not None:
            stage = "preview_before_rollback"
            preview(report["temporary_element_ids"])
    except Exception as exc:
        report["issues"].append({"stage": stage, "message": text_type(exc), "traceback": traceback.format_exc()})
    finally:
        if transaction is not None:
            ended, report["inner_end"] = rollback_scope(transaction, DB, allow_committed=True)
        if group is not None:
            if transaction is None or ended:
                ended, report["rollback"] = rollback_scope(group, DB)
            else:
                ended = False
                report["rollback"] = {"status": "unconfirmed", "reason": "inner_transaction_not_closed"}
                try:
                    group.Dispose()
                except Exception as exc:
                    report["rollback"]["dispose_error"] = text_type(exc)
        if worksharing is not None:
            finish_trial_worksharing(document, DB, floor, list(bar_types.values()), worksharing,
                may_read=(group is None and transaction is None) or ended)
            report["ownership_notice"] = ownership_notice(worksharing)
    if group is None and transaction is None:
        return report
    report["status"] = "rollback_unconfirmed"
    if not ended:
        return report  # Pending failure handling forbids additional model reads.
    try:
        after_ids = document_ids(document, DB)
        after_probe = Probe(document, DB)
        after = after_probe.floor(document.GetElement(DB.ElementId(before["element_id"])))
        restored = before_ids == after_ids and before == after and not after_probe.issues
        report["restoration"] = {"verified": restored, "added_ids": sorted(after_ids - before_ids),
            "removed_ids": sorted(before_ids - after_ids), "host_unchanged": before == after,
            "scope": "local document element IDs and host snapshot only; NOT central ownership or all UI state",
            "central_ownership_restored": worksharing.get("central_ownership_restored")}
        report["status"] = ("restoration_failed" if not restored else "failed_rolled_back" if report["issues"]
                            else "passed_rolled_back")
    except Exception as exc:
        report["status"] = "restoration_failed"
        report["issues"].append({"stage": "restoration", "message": text_type(exc)})
    return report
