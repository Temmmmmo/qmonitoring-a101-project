# -*- coding: utf-8 -*-
"""Native whole-party Rebar review with post-Commit readback and explicit keep."""
from __future__ import division, unicode_literals

import copy
import datetime
import json
import time
import traceback

from qm_rebar_review import (REPORT_SCHEMA, VERSION, compare_native, flat_outer_host, make_review_plan,
    manual_axis_depth_policy, validate_axis_depth_policy)
from qm_revit_probe import Probe, element_id, text_type
from qm_revit_trial import create_trial_rebar, document_ids, failure_recorder, read_trial_rebar, rollback_scope
from qm_trial_worksharing import (assign_new_rebar_workset, authorize_trial_worksharing,
    finish_trial_worksharing, inspect_trial_worksharing, ownership_notice, verify_new_rebar_worksets)


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


def run_rebar_review(document, DB, floor, primitives, bar_types, depths, curve_list_factory,
                     keep_confirmation, copy_confirmed=False, worksharing_consent=False,
                     checkout_id_set_factory=None, preview=None, axis_depth_policy=None):
    """Create every straight bar, Commit/read back, then keep or roll back all."""
    report = {"schema_version":REPORT_SCHEMA,"version":VERSION,
        "created_utc":datetime.datetime.utcnow().isoformat()+"Z","units":"mm",
        "status":"blocked_preflight","mode":"explicit-keep-after-commit-readback",
        "placement_eligible":False,"engineering_approval":False,"issues":[],"commit_failures":[],
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
        stage = "pre_commit_readback"
        report["pre_commit_readback"] = _read_created(probe,report["created_element_ids"],identities)
        verify_new_rebar_worksets(document,DB,report["created_element_ids"],worksharing,"pre_commit")
        report["pre_commit_comparison"] = compare_native(plan,report["pre_commit_readback"])
        report["timing_seconds"]["pre_commit_readback_cumulative"] = time.time()-started
        if report["pre_commit_comparison"]["status"] != "matches" or _blocking_read_issues(probe,report):
            raise ValueError("Whole-party native readback differs before Commit")
        stage = "commit"
        returned = transaction.Commit()
        final = transaction.GetStatus()
        report["commit"] = {"returned":text_type(returned),"final":text_type(final)}
        if returned != DB.TransactionStatus.Committed or final != returned or report["commit_failures"]:
            raise ValueError("Commit failed or posted a warning/error; whole party is rejected")
        ended = True
        stage = "post_commit_readback"
        report["post_commit_readback"] = _read_created(probe,report["created_element_ids"],identities)
        verify_new_rebar_worksets(document,DB,report["created_element_ids"],worksharing,"post_commit")
        report["post_commit_comparison"] = compare_native(plan,report["post_commit_readback"])
        report["timing_seconds"]["post_commit_readback_cumulative"] = time.time()-started
        if report["post_commit_comparison"]["status"] != "matches" or _blocking_read_issues(probe,report):
            raise ValueError("Whole-party native readback differs after Commit")
        if probe.floor(floor) != before or any(probe.bar_type(item) != types[key] for key,item in bar_types.items()):
            raise ValueError("Host/type changed after Commit; diagnostic party cannot be kept")
        if _blocking_read_issues(probe,report):
            raise ValueError("Incomplete native host/type reread after Commit")
        if preview is not None:
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
