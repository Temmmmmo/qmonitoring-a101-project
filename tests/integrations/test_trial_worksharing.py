"""Worksharing doubles: ownership persists independently of local transaction rollback."""
from __future__ import annotations

import copy
import importlib
from types import SimpleNamespace

import pytest

import test_full_plate_trial as full_fixtures
from test_physical_plan_trial import physical_module, physical_packet  # noqa: F401 -- fixture registration

packet = full_fixtures.packet
packet_module = full_fixtures.packet_module
placement = full_fixtures.placement
runtime_harness = full_fixtures.runtime_harness


class WorksetId:
    def __init__(self, value):
        self.IntegerValue = value


class IdSet(list):
    def Add(self, item):
        self.append(item)


@pytest.fixture
def shared(request, monkeypatch):
    runtime, doc, db, floor, bar_type, state = request.getfixturevalue("runtime_harness")
    module = importlib.import_module("qm_trial_worksharing")
    doc.IsWorkshared, doc.IsDetached, doc.IsModelInCloud = True, False, False
    doc.PathName = r"C:\work\local.rvt"
    doc.Application.Username = "Виктор"
    floor.WorksetId = WorksetId(7)
    floor.GroupId = floor.AssemblyInstanceId = db.ElementId(-1)
    floor.DesignOption = None
    bar_type.Id, bar_type.WorksetId = db.ElementId(888), WorksetId(3)
    central = SimpleNamespace(ServerPath=False, CloudPath=False)
    doc.GetWorksharingCentralModelPath = lambda: central
    central_path = r"\\server\work\central.rvt"
    db.ModelPathUtils = SimpleNamespace(ConvertModelPathToUserVisiblePath=lambda _: central_path)
    info = SimpleNamespace(IsLocal=True, IsCentral=False, CentralPath=central_path,
                           Dispose=lambda: state.calls.append("dispose_file_info"))
    db.BasicFileInfo = SimpleNamespace(Extract=lambda _: info)
    sets = {number: SimpleNamespace(Id=WorksetId(number), Name="Набор " + str(number),
        Kind="UserWorkset", IsOpen=True, IsEditable=False, Owner="") for number in (7, 8)}
    table = SimpleNamespace(GetWorkset=lambda value: sets[value.IntegerValue], GetActiveWorksetId=lambda: WorksetId(8))
    doc.GetWorksetTable = lambda: table
    state.owner = "NotOwned"
    state.updated = "CurrentWithCentral"
    state.type_updated = "CurrentWithCentral"
    state.checkout_result = "success"
    state.rebars = {}
    state.assigned = []

    def forbidden(*args, **kwargs):
        pytest.fail("No Save/Sync/Reload/workset checkout/relinquish or active-workset changes")

    doc.Save = doc.SaveAs = doc.SynchronizeWithCentral = doc.ReloadLatest = forbidden
    table.SetActiveWorksetId = forbidden

    def checkout(document, ids, options):
        assert document is doc
        assert [value.Value for value in ids] == [999]
        assert options.callback.ShouldWaitForLockAvailability() is False
        assert "start_group" not in state.calls
        state.calls.append("checkout")
        if state.checkout_result == "denied":
            return []
        if state.checkout_result == "network":
            raise RuntimeError("Central file is not reachable")
        state.owner = "OwnedByCurrentUser"  # Group rollback intentionally does NOT reset this.
        if state.checkout_result == "stale":
            state.updated = "UpdatedInCentral"
        if state.checkout_result == "extra":
            return [floor.Id, db.ElementId(777)]
        return [floor.Id]

    db.WorksharingUtils = SimpleNamespace(CheckoutElements=checkout,
        GetCheckoutStatus=lambda d, value: state.owner if value.Value == 999 else "OwnedByOtherUser",
        GetModelUpdatesStatus=lambda d, value: state.updated if value.Value == 999 else state.type_updated,
        GetWorksharingTooltipInfo=lambda d, value: SimpleNamespace(Owner=("Виктор" if state.owner == "OwnedByCurrentUser" else "")
            if value.Value == 999 else "Типы читает другой"),
        CheckoutWorksets=forbidden, RelinquishOwnership=forbidden)

    class Options:
        def SetLockCallback(self, callback):
            self.callback = callback

        def Dispose(self):
            state.calls.append("dispose_checkout_options")

    db.TransactWithCentralOptions, db.ICentralLockedCallback = Options, object
    db.BuiltInParameter.ELEM_PARTITION_PARAM = "workset"
    original_create = runtime.create_trial_rebar

    def create(probe, f, t, run, factory):
        rebar = original_create(probe, f, t, run, factory)
        rebar.WorksetId = WorksetId(8)
        original_parameter = rebar.get_Parameter

        def set_workset(value):
            state.assigned.append((rebar.Id.Value, value))
            rebar.WorksetId = WorksetId(value)
            return True

        rebar.get_Parameter = lambda name: (SimpleNamespace(IsReadOnly=False, Set=set_workset)
            if name == "workset" else original_parameter(name))
        rebar.SetUnobscuredInView = lambda *args: pytest.fail("No display mutation in shared model")
        state.rebars[rebar.Id.Value] = rebar
        return rebar

    monkeypatch.setattr(runtime, "create_trial_rebar", create)
    doc.GetElement = lambda value: state.rebars.get(value.Value, floor if value.Value == 999 else bar_type)
    return SimpleNamespace(runtime=runtime, doc=doc, DB=db, floor=floor, bar_type=bar_type, state=state,
                           helper=module, central=central, info=info, worksets=sets, table=table)


def invoke(case, packet, placement, **kwargs):
    return case.runtime.run_plate_trial(case.doc, case.DB, case.floor, packet, {"A500|18": case.bar_type}, placement,
        lambda: [], lambda: [], copy_confirmed=True, checkout_id_set_factory=IdSet, **kwargs)


@pytest.mark.parametrize("mode", ["non_workshared", "detached_with_worksets", "live_local", "central_workshared",
                                  "cloud_workshared", "server_workshared", "unknown_workshared"])
def test_model_classification_is_explicit_and_does_not_change_worksets(shared, mode):
    if mode == "non_workshared":
        shared.doc.IsWorkshared = False
    elif mode == "detached_with_worksets":
        shared.doc.IsDetached = True
    elif mode == "central_workshared":
        shared.info.IsCentral = True
    elif mode == "cloud_workshared":
        shared.doc.IsModelInCloud = True
    elif mode == "server_workshared":
        shared.central.ServerPath = True
    elif mode == "unknown_workshared":
        shared.info.IsLocal = False
    result = shared.helper.classify_document(shared.doc, shared.DB)
    assert result["mode"] == mode
    assert (result["status"] == "blocked") == (mode in {"central_workshared", "cloud_workshared", "server_workshared", "unknown_workshared"})
    assert "checkout" not in shared.state.calls


def test_detached_with_worksets_runs_normally_without_any_central_calls(shared, packet, placement):
    shared.doc.IsDetached = True
    shared.worksets[7].Owner = "Другой (старые метаданные)"

    def forbidden(*args):
        pytest.fail("Detached mode must never access central permissions or saved-file identity")

    shared.DB.WorksharingUtils = SimpleNamespace(GetCheckoutStatus=forbidden, GetModelUpdatesStatus=forbidden,
        CheckoutElements=forbidden, GetWorksharingTooltipInfo=forbidden)
    shared.DB.BasicFileInfo.Extract = forbidden
    shared.doc.GetWorksharingCentralModelPath = forbidden
    result = invoke(shared, packet, placement)
    assert result["status"] == "passed_rolled_back"
    assert result["worksharing"]["mode"] == "detached_with_worksets"
    assert result["worksharing"]["central_ownership_restored"] is None
    assert result["worksharing"]["active_workset_unchanged"] is True
    assert len(shared.state.assigned) == 4
    assert all(value == 7 for _, value in shared.state.assigned)
    assert len(shared.worksets) == 2 and shared.doc.IsWorkshared is True


def test_live_checkout_is_separately_confirmed_after_geometry_and_ownership_survives_rollback(shared, packet, placement):
    consent_calls = []

    def consent(report):
        assert report["mode"] == "live_local" and not report["checkout_attempted"]
        assert "start_group" not in shared.state.calls
        consent_calls.append(True)
        return True

    result = invoke(shared, packet, placement, worksharing_consent=consent)
    assert result["status"] == "passed_rolled_back" and consent_calls == [True]
    assert result["restoration"]["verified"] is True
    assert result["restoration"]["central_ownership_restored"] is False
    assert "NOT central ownership" in result["restoration"]["scope"]
    assert shared.state.owner == "OwnedByCurrentUser"
    sharing = result["worksharing"]
    assert sharing["before"]["elements"][0]["checkout_status_cached"] == "NotOwned"
    assert sharing["after"]["elements"][0]["checkout_status_cached"] == "OwnedByCurrentUser"
    assert sharing["observed_ownership_unchanged"] is False
    assert sharing["central_ownership_restored"] is False and result["ownership_notice"]
    assert len(sharing["post_commit_rebar_worksets"]) == 4
    assert all(run["allow_new_shape"] is False for run in shared.state.made)
    assert shared.state.calls.index("checkout") < shared.state.calls.index("start_group")
    assert shared.state.ids == {999, 888}


@pytest.mark.parametrize("consent", [False, None, 1, "yes"])
def test_live_consent_is_never_inferred_from_copy_or_placement_confirmation(shared, packet, placement, consent):
    result = invoke(shared, packet, placement, worksharing_consent=consent)
    assert result["status"] == "blocked_preflight"
    assert not result["worksharing"]["checkout_attempted"]
    assert "checkout" not in shared.state.calls and "start_group" not in shared.state.calls


def test_geometry_failure_never_prompts_or_contacts_central(shared, packet, placement):
    shared.state.geometry_blocked = True

    def forbidden(_):
        pytest.fail("No ownership prompt before a full geometry pass")

    result = invoke(shared, packet, placement, worksharing_consent=forbidden)
    assert result["status"] == "blocked_preflight" and "checkout" not in shared.state.calls


@pytest.mark.parametrize("failure", ["other_owner", "stale", "deleted", "stale_type", "unknown_status",
                                     "closed", "non_user", "workset_owner", "grouped", "assembly", "option"])
def test_unsafe_live_state_blocks_without_borrowing(shared, packet, placement, failure):
    if failure == "other_owner":
        shared.state.owner = "OwnedByOtherUser"
    elif failure in {"stale", "deleted", "unknown_status"}:
        shared.state.updated = {"stale": "UpdatedInCentral", "deleted": "DeletedInCentral", "unknown_status": "unknown"}[failure]
    elif failure == "stale_type":
        shared.state.type_updated = "UpdatedInCentral"
    elif failure == "closed":
        shared.worksets[7].IsOpen = False
    elif failure == "non_user":
        shared.worksets[7].Kind = "ViewWorkset"
    elif failure == "workset_owner":
        shared.worksets[8].Owner = "Другой"
    elif failure == "grouped":
        shared.floor.GroupId = shared.DB.ElementId(5)
    elif failure == "assembly":
        shared.floor.AssemblyInstanceId = shared.DB.ElementId(6)
    else:
        shared.floor.DesignOption = object()
    result = invoke(shared, packet, placement, worksharing_consent=True)
    assert result["status"] == "blocked_preflight"
    assert result["worksharing"]["status"] == "blocked"
    assert "checkout" not in shared.state.calls
    assert result["host_id"] == 999 and result["host"] is not None


@pytest.mark.parametrize("failure", ["denied", "network", "stale", "extra"])
def test_checkout_failure_keeps_possible_ownership_change_in_report_without_transactions(shared, packet, placement, failure):
    shared.state.checkout_result = failure
    result = invoke(shared, packet, placement, worksharing_consent=True)
    assert result["status"] == "blocked_preflight" and "start_group" not in shared.state.calls
    assert result["worksharing"]["checkout_attempted"] is True
    assert result["worksharing"]["central_ownership_restored"] is False
    assert result["worksharing"]["after_status"] == "read_cached_scope_only"
    assert result["ownership_notice"]
    assert "dispose_checkout_options" in shared.state.calls


def test_live_existing_view_is_not_modified(shared, packet, placement):
    view = SimpleNamespace(GetElementOverrides=lambda *_: pytest.fail("Do not edit existing shared view"),
        SetElementOverrides=lambda *_: pytest.fail("Do not edit existing shared view"))
    result = invoke(shared, packet, placement, worksharing_consent=True, view=view)
    assert result["status"] == "passed_rolled_back"


def test_pending_transaction_does_not_read_worksharing_state_after_failure(shared, packet, placement):
    shared.state.commit = "Pending"
    result = invoke(shared, packet, placement, worksharing_consent=True)
    assert result["status"] == "rollback_unconfirmed"
    assert result["worksharing"]["after_status"] == "not_checked_pending_transaction"
    assert "after" not in result["worksharing"]
    assert result["ownership_notice"]


def test_failed_workset_assignment_rolls_back_entire_batch(shared, packet, placement, monkeypatch):
    original = shared.runtime.assign_new_rebar_workset

    def broken(document, DB, rebar, report):
        rebar.get_Parameter = lambda _: SimpleNamespace(IsReadOnly=True)
        return original(document, DB, rebar, report)

    monkeypatch.setattr(shared.runtime, "assign_new_rebar_workset", broken)
    result = invoke(shared, packet, placement, worksharing_consent=True)
    assert result["status"] == "failed_rolled_back"
    assert shared.state.ids == {999, 888} and len(shared.state.made) == 1
    assert result["ownership_notice"]


def test_physical_wrapper_exposes_ownership_without_changing_public_source_packet(shared, request, placement):
    packet = request.getfixturevalue("physical_packet")
    original = copy.deepcopy(packet)
    runtime = importlib.import_module("qm_revit_physical_trial")
    result = runtime.run_physical_trial(shared.doc, shared.DB, shared.floor, packet,
        {"A500|18": shared.bar_type}, placement, lambda: [], lambda: [], copy_confirmed=True,
        worksharing_consent=True, checkout_id_set_factory=IdSet)
    assert result["status"] == "passed_rolled_back"
    assert result["worksharing"] == result["execution"]["worksharing"]
    assert result["ownership_notice"] == result["execution"]["ownership_notice"]
    assert packet == original and result["physical_plan"] == original
    assert result["placement_eligible"] is result["engineering_approval"] is False


def test_separate_ui_consent_names_host_workset_and_nonrollbackable_ownership(shared):
    report = shared.helper.inspect_trial_worksharing(shared.doc, shared.DB, shared.floor, [shared.bar_type])
    prompts = []
    forms = SimpleNamespace(alert=lambda message, **kw: (prompts.append((message, kw)), False)[1])
    assert shared.helper.confirm_live_local(report, forms) is False
    assert len(prompts) == 1
    message, kwargs = prompts[0]
    assert "id=999" in message and "Набор 7" in message and "ВЛАДЕНИЕ В ЦЕНТРАЛЬНОЙ МОЖЕТ ОСТАТЬСЯ" in message
    assert "Preserve Worksets" in message and kwargs == {"yes": True, "no": True}
    assert "checkout" not in shared.state.calls


@pytest.mark.parametrize("change", ["host_owner", "cloud_mode", "workset_owner"])
def test_state_change_during_consent_is_rechecked_before_checkout(shared, packet, placement, change):
    def consent(_):
        if change == "host_owner":
            shared.state.owner = "OwnedByOtherUser"
        elif change == "cloud_mode":
            shared.doc.IsModelInCloud = True
        else:
            shared.worksets[7].Owner = "Другой"
        return True

    result = invoke(shared, packet, placement, worksharing_consent=consent)
    assert result["status"] == "blocked_preflight"
    assert "checkout" not in shared.state.calls and "start_group" not in shared.state.calls


def test_native_host_change_at_checkout_requires_fresh_full_geometry_plan(shared, packet, placement, monkeypatch):
    original = shared.runtime.Probe.floor

    def changed(probe, floor):
        result = original(probe, floor)
        if "checkout" in shared.state.calls:
            result["covers"]["top"]["distance_mm"] += 1
        return result

    monkeypatch.setattr(shared.runtime.Probe, "floor", changed)
    result = invoke(shared, packet, placement, worksharing_consent=True)
    assert result["status"] == "blocked_preflight" and "start_group" not in shared.state.calls
    assert result["ownership_notice"] and shared.state.owner == "OwnedByCurrentUser"


def test_post_commit_workset_adjustment_is_detected_not_reported_as_success(shared, packet, placement, monkeypatch):
    original = shared.runtime.read_core_sets

    def changed(*args):
        result = original(*args)
        if shared.state.read_count == 2:
            next(iter(shared.state.rebars.values())).WorksetId = WorksetId(8)
        return result

    monkeypatch.setattr(shared.runtime, "read_core_sets", changed)
    result = invoke(shared, packet, placement, worksharing_consent=True)
    assert result["status"] == "failed_rolled_back"
    assert any("workset changed" in item["message"] for item in result["issues"])
    assert shared.state.ids == {999, 888}


def test_equal_cached_ownership_still_never_certifies_full_central_restoration(shared, packet, placement):
    shared.state.owner = "OwnedByCurrentUser"
    result = invoke(shared, packet, placement, worksharing_consent=True)
    assert result["status"] == "passed_rolled_back"
    assert result["worksharing"]["observed_ownership_unchanged"] is True
    assert result["worksharing"]["central_ownership_restored"] is False


def test_real_full_and_physical_buttons_wire_separate_consent_callback():
    # The callback is invoked by tested runtime after geometry, never by a setup prompt.
    root = full_fixtures.ROOT / "integrations/pyrevit/QMonitoring.extension/QMonitoring.tab/Diagnostics.panel"
    for button in ("FullPlateTrial", "PhysicalPlanTrial"):
        source = (root / (button + ".pushbutton/script.py")).read_text(encoding="utf-8")
        assert "worksharing_consent=lambda state: confirm_live_local(state, forms)" in source
        assert "forms.alert(report[\"ownership_notice\"])" in source
