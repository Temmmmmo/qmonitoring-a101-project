"""Optional new 3D-view lifecycle doubles, not live Revit proof."""
from pathlib import Path
import importlib
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("failure", [None,"missing-type","create","commit","pending"])
def test_new_view_transaction_never_activates_and_rolls_back_own_failures(monkeypatch, failure):
    lib = Path(__file__).resolve().parents[2] / "integrations/pyrevit/QMonitoring.extension/lib"
    sys.path.insert(0,str(lib))
    helper = importlib.import_module("qm_revit_presentation_view")
    monkeypatch.setattr(helper,"failure_recorder",lambda *_: object())
    calls = []
    class Id:
        def __init__(self,n):
            self.Value = n
    Id.InvalidElementId = Id(-1)
    class List(list):
        @classmethod
        def __class_getitem__(cls,_):
            return cls
        def Add(self,item):
            self.append(item)
    monkeypatch.setitem(sys.modules,"System.Collections.Generic",SimpleNamespace(List=List))
    class Settings:
        def SetClearAfterRollback(self,_): return self
        def SetForcedModalHandling(self,_): return self
        def SetFailuresPreprocessor(self,_): return self
        def SetSurfaceTransparency(self,value):
            calls.append(("transparency",value))
            return self
        def SetProjectionLineColor(self,_): return self
        def SetProjectionLineWeight(self,_): return self
    class Scope:
        def __init__(self,*_): self.status = "Uninitialized"
        def Start(self):
            calls.append("start")
            self.status = "Started"
            return self.status
        def GetStatus(self): return self.status
        def GetFailureHandlingOptions(self): return Settings()
        def SetFailureHandlingOptions(self,_): pass
        def Commit(self):
            calls.append("commit")
            self.status = "Pending" if failure == "pending" else "RolledBack" if failure == "commit" else "Committed"
            return self.status
        def RollBack(self):
            calls.append("rollback")
            self.status = "RolledBack"
            return self.status
        def Dispose(self): calls.append("dispose")
    view = SimpleNamespace(Id=Id(200),SetSectionBox=lambda _: calls.append("section"),
        SetElementOverrides=lambda identifier,_: calls.append(("override",identifier.Value)),
        IsolateElementsTemporary=lambda ids: calls.append(("isolate",[item.Value for item in ids])))
    def create(*_):
        if failure == "create":
            raise ValueError("optional view creation failed")
        return view
    class Rebar:
        def SetUnobscuredInView(self,_,enabled): calls.append(("unobscured",enabled))
    rebar = Rebar()
    document = SimpleNamespace(GetElement=lambda _: rebar)
    def point(x,y,z):
        return SimpleNamespace(X=x,Y=y,Z=z)
    floor = SimpleNamespace(Id=Id(99),get_BoundingBox=lambda _: SimpleNamespace(Min=point(0,0,0),Max=point(10,10,1)))
    class Collector:
        def __init__(self,_): pass
        def OfClass(self,_): return [] if failure == "missing-type" else [SimpleNamespace(ViewFamily="3D",Id=Id(20))]
    DB = SimpleNamespace(FilteredElementCollector=Collector,ViewFamilyType=object,
        ViewFamily=SimpleNamespace(ThreeDimensional="3D"),Transaction=Scope,View3D=SimpleNamespace(CreateIsometric=create),
        ViewDetailLevel=SimpleNamespace(Fine="Fine"),UnitUtils=SimpleNamespace(ConvertToInternalUnits=lambda v,_:v),
        UnitTypeId=SimpleNamespace(Millimeters="mm"),BoundingBoxXYZ=SimpleNamespace,XYZ=point,
        ElementId=Id,OverrideGraphicSettings=Settings,Color=lambda *_:None,Structure=SimpleNamespace(Rebar=Rebar),
        TransactionStatus=SimpleNamespace(Started="Started",Uninitialized="Uninitialized",Committed="Committed",RolledBack="RolledBack"))
    if failure:
        error = helper.PresentationViewRollbackError if failure == "pending" else ValueError
        with pytest.raises(error):
            helper.create_presentation_view(document,DB,floor,[100],{100:"top-X"})
        if failure == "create":
            assert "rollback" in calls
        if failure == "missing-type":
            assert calls == []
            return
    else:
        result = helper.create_presentation_view(document,DB,floor,[100],{100:"top-X"})
        assert result["activation"] == "deferred_until_whole_party_kept"
        assert "результат раскладки" in result["view_name"]
        assert ("isolate",[99,100]) in calls and ("transparency",75) in calls
        assert result["view_id"] == 200 and view.DetailLevel == "Fine"
        assert view.IsSectionBoxActive is True
        assert view.ViewTemplateId.Value == -1
    assert calls[-1] == "dispose"
