"""2024 constraint API doubles: not a live native placement approval."""
from pathlib import Path
import importlib
import sys
from types import SimpleNamespace

import pytest

LIB = Path(__file__).resolve().parents[2] / "integrations/pyrevit/QMonitoring.extension/lib"


@pytest.mark.parametrize("axis,offset_sign,reverse", [("X", 1, False), ("X", -1, True), ("Y", 1, False), ("Y", -1, True)])
def test_fixed_face_offsets_restore_expected_axes_not_cover_or_other_bars(monkeypatch, axis, offset_sign, reverse):
    sys.path.insert(0, str(LIB))
    runtime = importlib.import_module("qm_revit_rebar_review")
    values = ([100, 111, 50], [498, 111, 50]) if axis == "X" else ([111, 100, 50], [111, 498, 50])
    wanted = ([95, 100, 50], [500, 100, 50]) if axis == "X" else ([100, 95, 50], [100, 500, 50])
    if reverse:
        values = values[::-1]
    normal = [0, 1, 0] if axis == "X" else [1, 0, 0]
    run = {"bar_id": "native-moved", "direction": "bottom-" + axis,
           "normal": normal, "axes": [{"start_mm": wanted[0], "end_mm": wanted[1]}]}
    midpoint = [(a+b)/2 for a, b in zip(*values)]
    movement = [1, 0, 0] if axis == "X" else [0, 1, 0]
    def vector(p):
        return SimpleNamespace(X=p[0], Y=p[1], Z=p[2])
    def identity(n):
        return SimpleNamespace(Value=n)

    class Face:
        def __init__(self, n):
            self.FaceNormal, self.Origin = vector(n), vector([0, 0, 0])

    faces, handles, candidates, assigned = {}, [], {}, {}
    for kind, point, n in (("StartOfBar", values[0], movement),
                           ("EndOfBar", values[1], movement),
                           ("Edge", midpoint, [0, 0, 1]),
                           ("RebarPlane", midpoint, normal)):
        handle = SimpleNamespace(GetHandleType=lambda k=kind: k, GetEdgeNumber=lambda: 0, IsValid=lambda: True)
        reference = SimpleNamespace(ElementId=identity(99), key=kind, ConvertToStableRepresentation=lambda _, k=kind: k)
        faces[kind] = Face(n)
        offset = sum(a*b for a, b in zip(point, n))*offset_sign
        candidate = SimpleNamespace(GetConstraintType=lambda: "FixedDistanceToHostFace",
            IsFixedDistanceToHostFace=lambda: True, NumberOfTargets=1, IsValid=lambda: True,
            GetTargetHostFaceReference=lambda r=reference: r, offset=offset, original_offset=offset)
        candidate.GetDistanceToTargetHostFace = lambda c=candidate: c.offset
        candidate.SetDistanceToTargetHostFace = lambda value, c=candidate: setattr(c, "offset", value)
        handles.append(handle)
        candidates[kind] = candidate
    manager = SimpleNamespace(GetAllHandles=lambda: handles,
        GetCurrentConstraintOnHandle=lambda _: SimpleNamespace(GetConstraintType=lambda: "ToOtherRebar",
            NumberOfTargets=1, GetTargetElement=lambda _: SimpleNamespace(Id=identity(888))),
        GetConstraintCandidatesForHandle=lambda h, _: [candidates[h.GetHandleType()]],
        SetPreferredConstraintForHandle=lambda h, c: assigned.setdefault(h.GetHandleType(), c),
        GetPreferredConstraintOnHandle=lambda h: assigned[h.GetHandleType()], Dispose=lambda: None)
    db = SimpleNamespace(PlanarFace=Face, UnitTypeId=SimpleNamespace(Millimeters="mm"),
        UnitUtils=SimpleNamespace(ConvertFromInternalUnits=lambda v, _: v,
                                  ConvertToInternalUnits=lambda v, _: v))
    rebar = SimpleNamespace(Id=identity(1000), GetRebarConstraintsManager=lambda: manager)
    floor = SimpleNamespace(Id=identity(99), GetGeometryObjectFromReference=lambda r: faces[r.key])
    monkeypatch.setattr(runtime, "read_trial_rebar", lambda *_: {"bars": [{"curves": [{
        "kind": "Line", "start_mm": values[0], "end_mm": values[1]}]}]})
    diagnostic = {}
    probe = SimpleNamespace(DB=db, doc=object())
    runtime.constrain_review_axis(probe, floor, rebar, run, diagnostic)
    assert assigned["StartOfBar"].offset == (500 if reverse else 95)*offset_sign
    assert assigned["EndOfBar"].offset == (95 if reverse else 500)*offset_sign
    assert assigned["RebarPlane"].offset == 100*offset_sign
    assert assigned["Edge"].offset == 50*offset_sign
    assert len(diagnostic["handles"]) == 4
    assert diagnostic["handles"][0]["previous_constraint_type"] == "ToOtherRebar"
    assert diagnostic["handles"][0]["previous_target_element_ids"] == [888]
    # Failed candidate capability/calibration must never turn into silent PASS.
    for candidate in candidates.values():
        candidate.offset = candidate.original_offset
    candidates["Edge"].offset += 0.1
    with pytest.raises(ValueError, match="No calibrated"):
        runtime.constrain_review_axis(probe, floor, rebar, run, {})
    candidates["Edge"].offset -= 0.1
    handles[0].GetHandleType = lambda: "CustomHandle"
    with pytest.raises(ValueError, match="Unsupported"):
        runtime.constrain_review_axis(probe, floor, rebar, run, {})
    handles[0].GetHandleType = lambda: "StartOfBar"
    manager.SetPreferredConstraintForHandle = lambda *_: (_ for _ in ()).throw(ValueError("assignment failed"))
    with pytest.raises(ValueError, match="assignment failed"):
        runtime.constrain_review_axis(probe, floor, rebar, run, {})


def test_matching_native_axis_skips_constraint_api_even_when_reversed(monkeypatch):
    sys.path.insert(0, str(LIB))
    runtime = importlib.import_module("qm_revit_rebar_review")
    monkeypatch.setattr(runtime, "read_trial_rebar", lambda *_: {"bars": [{"curves": [{
        "kind": "Line", "start_mm": [500, 100, 50], "end_mm": [95, 100, 50]}]}]})
    run = {"bar_id": "unchanged", "direction": "bottom-X", "normal": [0, 1, 0],
           "axes": [{"start_mm": [95, 100, 50], "end_mm": [500, 100, 50]}]}
    diagnostic = {}
    # No constraint manager exists on this double: the untouched path needs none.
    runtime.constrain_review_axis(SimpleNamespace(DB=None), None,
        SimpleNamespace(Id=SimpleNamespace(Value=1)), run, diagnostic)
    assert diagnostic["status"] == "native_axis_already_matches_no_constraints_changed"
    assert diagnostic["handles"] == []
