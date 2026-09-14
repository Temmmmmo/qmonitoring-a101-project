# -*- coding: utf-8 -*-
"""Bounded worksharing policy for rollback trials, not a central-state rollback.

Autodesk Revit 2024: Worksharing Overview / Editing Elements in Worksets /
Elements in Worksets. Cached ownership is evidence only; live editing requires
explicitly confirmed checkout. Never Save, Sync, Reload, checkout worksets or
relinquish somebody's pre-existing work. Revit may borrow related elements.
"""
from __future__ import division, unicode_literals

import ntpath

from qm_revit_probe import element_id, text_type


def classify_document(document, DB):
    result = {"is_workshared": bool(document.IsWorkshared), "mode": "non_workshared",
              "status": "supported", "issues": []}
    if not result["is_workshared"]:
        return result
    result["mode"], result["status"] = "unknown_workshared", "blocked"
    try:
        result["is_detached"] = bool(document.IsDetached)
        result["is_cloud"] = bool(document.IsModelInCloud)
        if result["is_detached"]:
            result["mode"], result["status"] = "detached_with_worksets", "supported"
            return result
        if result["is_cloud"]:
            result["mode"] = "cloud_workshared"
            raise ValueError("Cloud worksharing is outside this trial; open a detached copy preserving worksets")
        path = text_type(document.PathName)
        central = document.GetWorksharingCentralModelPath()
        if central is None:
            raise ValueError("No central model identity; detach a copy preserving worksets")
        central_path = text_type(DB.ModelPathUtils.ConvertModelPathToUserVisiblePath(central))
        result.update({"path": path, "central_path": central_path})
        if central.CloudPath:
            result["mode"] = "cloud_workshared"
            raise ValueError("Cloud central model path is outside this trial; use a detached copy preserving worksets")
        if central.ServerPath:
            result["mode"] = "server_workshared"
            raise ValueError("Revit Server worksharing is outside this trial; use a detached copy preserving worksets")
        if not path or not central_path:
            raise ValueError("Local/central file identity is incomplete")
        info = DB.BasicFileInfo.Extract(path)
        try:
            result["file_info"] = {"is_local": bool(info.IsLocal), "is_central": bool(info.IsCentral),
                                   "central_path": text_type(info.CentralPath)}
        finally:
            info.Dispose()
        stored = result["file_info"]
        def normalize(value):
            return ntpath.normcase(ntpath.normpath(value))
        if stored["is_central"] or normalize(path) == normalize(central_path):
            result["mode"] = "central_workshared"
            raise ValueError("Do not run directly in central; use a local or detached copy preserving worksets")
        if not stored["is_local"] or normalize(stored["central_path"]) != normalize(central_path):
            raise ValueError("Saved-file local identity disagrees with the open document")
        result["mode"], result["status"] = "live_local", "supported_with_explicit_ownership_consent"
    except Exception as exc:
        result["issues"].append(text_type(exc))
    return result


def _workset(document, DB, value):
    workset = document.GetWorksetTable().GetWorkset(value)
    if workset is None:
        raise ValueError("Missing workset")
    return {"id": int(workset.Id.IntegerValue), "name": text_type(workset.Name),
            "kind": text_type(workset.Kind), "is_open": bool(workset.IsOpen),
            "is_editable_cached": bool(workset.IsEditable), "owner_cached": text_type(workset.Owner)}


def _element_state(document, DB, element, role):
    info = DB.WorksharingUtils.GetWorksharingTooltipInfo(document, element.Id)
    return {"element_id": element_id(element.Id), "role": role,
            "workset_id": int(element.WorksetId.IntegerValue),
            "checkout_status_cached": text_type(DB.WorksharingUtils.GetCheckoutStatus(document, element.Id)),
            "updates_status_cached": text_type(DB.WorksharingUtils.GetModelUpdatesStatus(document, element.Id)),
            "owner_cached": text_type(info.Owner)}


def _snapshot(document, DB, floor, bar_types, live):
    result = {"host_workset": _workset(document, DB, floor.WorksetId),
              "active_workset": _workset(document, DB, document.GetWorksetTable().GetActiveWorksetId()),
              "elements": []}
    if live:
        result["elements"].append(_element_state(document, DB, floor, "selected-host"))
        seen = {element_id(floor.Id)}
        for item in bar_types:
            if element_id(item.Id) not in seen:
                seen.add(element_id(item.Id))
                result["elements"].append(_element_state(document, DB, item, "read-only-bar-type"))
    return result


def inspect_trial_worksharing(document, DB, floor, bar_types=()):
    report = {"document": classify_document(document, DB), "status": "blocked",
        "consent_received": False, "checkout_attempted": False, "created_rebar_worksets": [],
        "central_ownership_restored": None, "observed_ownership_unchanged": None,
        "ownership_scope": "Selected host, selected read-only bar types and host/active worksets only; not all central permissions",
        "warnings": [], "issues": []}
    mode = report["document"]["mode"]
    report["mode"] = mode
    if mode == "non_workshared":
        report["status"] = "not_applicable"
        return report
    if report["document"]["status"] == "blocked":
        report["issues"] = list(report["document"]["issues"])
        return report
    live = mode == "live_local"
    try:
        report["before"] = _snapshot(document, DB, floor, bar_types, live)
        for key in ("host_workset", "active_workset"):
            workset = report["before"][key]
            if workset["kind"] != "UserWorkset" or not workset["is_open"]:
                raise ValueError("Host and active worksets must be open user worksets; no workset will be opened or changed automatically")
            if live and workset["owner_cached"] and workset["owner_cached"] != text_type(document.Application.Username):
                raise ValueError("Workset belongs to another user: " + workset["name"])
        if live:
            if (element_id(floor.GroupId) != -1 or element_id(floor.AssemblyInstanceId) != -1
                    or floor.DesignOption is not None):
                raise ValueError("Live-local trial does not borrow grouped, assembled or design-option hosts; use a detached copy")
            _require_current(report["before"])
            if report["before"]["elements"][0]["checkout_status_cached"] == "OwnedByOtherUser":
                raise ValueError("The selected floor is owned by another user")
            report["warnings"].append("Checkout requests ONLY the selected Floor, but Revit can borrow related elements. Local rollback does NOT return central ownership. No automatic relinquish/sync will run.")
        report["status"] = "awaiting_ownership_consent" if live else "detached_ready"
    except Exception as exc:
        report["issues"].append(text_type(exc))
    return report


def _require_current(snapshot):
    for item in snapshot["elements"]:
        if item["updates_status_cached"] != "CurrentWithCentral":
            raise ValueError("Element {0} is not current with central ({1}); update it manually before a new trial".format(
                item["element_id"], item["updates_status_cached"]))


def authorize_trial_worksharing(document, DB, floor, bar_types, report, consent, id_set_factory=None):
    """Called only AFTER all geometry checks, immediately BEFORE local transactions."""
    if report["status"] == "blocked":
        raise ValueError("Worksharing preflight: " + "; ".join(report["issues"]))
    if report["mode"] != "live_local":
        return
    if callable(consent):
        consent = consent(report)
    if consent is not True:
        raise ValueError("Separate explicit live-local ownership consent is required; no checkout was attempted")
    report["consent_received"] = True
    # Refresh cached diagnostics before checkout; never treat the cache as permission.
    current = inspect_trial_worksharing(document, DB, floor, bar_types)
    report["immediately_before_checkout"] = current.get("before")
    if current["status"] != "awaiting_ownership_consent":
        raise ValueError("Worksharing state changed before checkout: " + "; ".join(current["issues"]))
    if id_set_factory is None:
        from System.Collections.Generic import HashSet
        def id_set_factory():
            return HashSet[DB.ElementId]()
    ids = id_set_factory()
    ids.Add(floor.Id)

    class DoNotWaitForCentral(DB.ICentralLockedCallback):
        def ShouldWaitForLockAvailability(self):
            return False

    options = DB.TransactWithCentralOptions()
    try:
        options.SetLockCallback(DoNotWaitForCentral())
        report["requested_checkout_ids"] = [element_id(floor.Id)]
        report["checkout_attempted"] = True
        report["central_ownership_restored"] = False
        obtained = DB.WorksharingUtils.CheckoutElements(document, ids, options)
        report["returned_checkout_ids"] = sorted(element_id(item) for item in obtained)
        if report["returned_checkout_ids"] != report["requested_checkout_ids"]:
            raise ValueError("Selected floor checkout was not confirmed exactly; no Rebar transaction will start")
        report["after_checkout"] = _snapshot(document, DB, floor, bar_types, True)
        _require_current(report["after_checkout"])
        if report["after_checkout"]["elements"][0]["checkout_status_cached"] != "OwnedByCurrentUser":
            raise ValueError("Selected floor ownership is not confirmed after checkout")
        report["status"] = "checkout_confirmed"
    except Exception as exc:
        report["status"] = "checkout_failed" if report["checkout_attempted"] else "authorization_failed"
        report["issues"].append(text_type(exc))
        raise
    finally:
        options.Dispose()


def assign_new_rebar_workset(document, DB, rebar, report):
    """Modify only the newly created Rebar, never a workset or active workset."""
    if report["mode"] == "non_workshared":
        return
    wanted = report["before"]["host_workset"]["id"]
    if int(rebar.WorksetId.IntegerValue) != wanted:
        parameter = rebar.get_Parameter(DB.BuiltInParameter.ELEM_PARTITION_PARAM)
        if parameter is None or parameter.IsReadOnly or not parameter.Set(wanted):
            raise ValueError("Cannot assign the temporary Rebar to the selected host workset")
    actual = int(rebar.WorksetId.IntegerValue)
    if actual != wanted:
        raise ValueError("Temporary Rebar workset readback differs")
    report["created_rebar_worksets"].append({"element_id": element_id(rebar.Id), "workset_id": actual})


def verify_new_rebar_worksets(document, DB, ids, report, stage):
    if report["mode"] == "non_workshared":
        return
    wanted = report["before"]["host_workset"]["id"]
    values = []
    for value in ids:
        element = document.GetElement(DB.ElementId(value))
        actual = int(element.WorksetId.IntegerValue)
        values.append({"element_id": value, "workset_id": actual})
        if actual != wanted:
            raise ValueError("Temporary Rebar workset changed at " + stage)
    report[stage + "_rebar_worksets"] = values


def finish_trial_worksharing(document, DB, floor, bar_types, report, may_read):
    if report["mode"] == "non_workshared":
        return
    if not may_read:
        report["after_status"] = "not_checked_pending_transaction"
        return
    if "before" not in report:
        return
    try:
        live = report["mode"] == "live_local"
        report["after"] = _snapshot(document, DB, floor, bar_types, live)
        report["observed_ownership_unchanged"] = report["before"] == report["after"]
        report["active_workset_unchanged"] = report["before"]["active_workset"]["id"] == report["after"]["active_workset"]["id"]
        report["after_status"] = "read_cached_scope_only" if live else "read_detached_worksets"
        # Same cached values do not prove that all central permissions were restored.
        report["central_ownership_restored"] = False if live and report["checkout_attempted"] else None
    except Exception as exc:
        report["after_status"] = "read_failed"
        report["issues"].append(text_type(exc))


def ownership_notice(report):
    if report.get("mode") == "live_local" and report.get("checkout_attempted"):
        return "ВНИМАНИЕ: локальная геометрия откатывается, но владение плитой/связанными элементами в центральной модели могло остаться. Возврат владения НЕ подтверждён. Автоматических Sync/Save/Relinquish нет; проверь владение обычными средствами Revit."
    return ""


def confirm_live_local(report, forms):
    """Separate UI consent, invoked by the runtime only after geometry passed."""
    workset = report["before"]["host_workset"]
    return forms.alert("Это ЖИВАЯ ЛОКАЛЬНАЯ совместная модель, не detached-копия.\n"
        "Запрошу заимствование только выбранной плиты id={0}. Сам Revit может дополнительно "
        "заимствовать связанные элементы.\n"
        "Новые временные стержни: рабочий набор «{1}» (id={2}), как у плиты. "
        "Существующие виды/типы и активный рабочий набор не меняются.\n"
        "После проверки геометрия откатится, НО ВЛАДЕНИЕ В ЦЕНТРАЛЬНОЙ МОЖЕТ ОСТАТЬСЯ. "
        "Автоматических Sync/Save/Reload/Relinquish нет. Возврат владения проверяется вручную.\n"
        "Чтобы не затрагивать центральную вообще, нажми Нет и открой копию: "
        "Detach from Central → Preserve Worksets.\n"
        "Разрешить этот запрос заимствования и временный запуск?".format(
            report["before"]["elements"][0]["element_id"], workset["name"], workset["id"]), yes=True, no=True) is True
