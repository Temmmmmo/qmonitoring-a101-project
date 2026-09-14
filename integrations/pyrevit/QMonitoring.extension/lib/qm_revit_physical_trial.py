# -*- coding: utf-8 -*-
"""Physical-plan diagnostic wrapper; existing host/readback/rollback stays intact."""
from __future__ import division

import copy
import datetime

from qm_physical_packet import VERSION, private_execution_packet, validate_packet
from qm_revit_plate_trial import run_plate_trial

REPORT_SCHEMA = "revit-physical-bar-plan-trial-report/v1"


def new_report(packet):
    """Keep complete ownership before any type/profile selection can be cancelled."""
    validate_packet(packet)
    return {"schema_version": REPORT_SCHEMA, "version": VERSION,
        "created_utc": datetime.datetime.utcnow().isoformat() + "Z", "units": "mm",
        "status": "blocked_setup", "mode": "commit-readback-rollback", "placement_eligible": False,
        "engineering_approval": False, "case_id": packet["case_id"],
        "source_report_sha256": packet["source_report_sha256"], "raw_report_sha256": packet["raw_report_sha256"],
        "expected": copy.deepcopy(packet["expected"]), "source_blockers": copy.deepcopy(packet["source_blockers"]),
        "manual_joint_tasks": copy.deepcopy(packet["manual_joint_tasks"]),
        "unresolved_intersection_pair_count": len(packet["manual_joint_tasks"]),
        "physical_plan": copy.deepcopy(packet), "issues": [],
        "execution_contract_notice": "The nested execution report is private transport only. Its historical zone_count/zone_id fields refer to execution groups, NOT LayoutZones or source ownership. Complete many-owner provenance is physical_plan.",
        "not_checked": ["working-host-and-dxf-registration-until-explicit-trial",
                        "engineering-xy-depth-profile", "three-dimensional-background-contact",
                        "resolution-of-listed-same-direction-intersections", "construction-acceptance",
                        "permanent-placement"],
        "diagnostic_scope": "Whole physical plan, with unresolved intersections retained. No hidden omission, permanent save, automatic depth choice, or claim of a completed parametric layout."}


def run_physical_trial(document, DB, floor, packet, bar_types, placement, curve_list_factory,
                       loop_list_factory, copy_confirmed=False, preview=None, view=None,
                       worksharing_consent=False, checkout_id_set_factory=None):
    report = new_report(packet)
    # Validation preserves all owners; projection is local and never written as a public packet.
    execution_packet = private_execution_packet(packet)
    execution = run_plate_trial(document, DB, floor, execution_packet, bar_types, placement,
        curve_list_factory, loop_list_factory, copy_confirmed=copy_confirmed, preview=preview, view=view,
        worksharing_consent=worksharing_consent, checkout_id_set_factory=checkout_id_set_factory)
    report["execution"] = execution
    report["status"] = execution["status"]
    report["issues"] = copy.deepcopy(execution["issues"])
    report["read_issues"] = copy.deepcopy(execution.get("read_issues", []))
    for key in ("worksharing", "ownership_notice"):
        if key in execution:
            report[key] = copy.deepcopy(execution[key])
    report["placement"] = copy.deepcopy(placement)
    if "host" in execution:
        report["host"] = copy.deepcopy(execution["host"])
    if "host_id" in execution:
        report["host_id"] = execution["host_id"]
    # Match each API element to its EXACT run and every physical owner, including multi-owner bars.
    runs = [run for direction in packet["directions"] for run in direction["runs"]]
    report["temporary_execution_mapping"] = [
        {"element_id": element_id, "run_id": run["id"],
         "execution_group_id": run["execution_group_id"], "bar_sources": copy.deepcopy(run["bar_sources"])}
        for element_id, run in zip(execution["temporary_element_ids"], runs)]
    if len(execution["temporary_element_ids"]) > len(runs):
        raise ValueError("Unexpected extra execution elements; do not treat as validated readback")
    return report
