# -*- coding: utf-8 -*-
"""New review-only 3D view; never activate it before whole-party Keep."""
from __future__ import division, unicode_literals

import datetime

from qm_revit_probe import element_id, text_type
from qm_revit_trial import failure_recorder, rollback_scope


class PresentationViewRollbackError(ValueError):
    rollback_unconfirmed = True


def create_presentation_view(document, DB, floor, created_ids, direction_by_id=None):
    """Caller must be inside the review TransactionGroup, outside Transaction.

    Returns metadata, not an activated view. Failure rolls back this view's
    transaction only; an unconfirmed rollback must reject the whole group.
    """
    from System.Collections.Generic import List
    types = [item for item in DB.FilteredElementCollector(document).OfClass(DB.ViewFamilyType)
        if item.ViewFamily == DB.ViewFamily.ThreeDimensional]
    if not types:
        raise ValueError("No existing ThreeDimensional ViewFamilyType; presentation view skipped")
    transaction = DB.Transaction(document,"QMonitoring presentation view - NOT FOR CONSTRUCTION")
    failures, committed = [], False
    try:
        if transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError("Presentation view transaction did not start")
        options = transaction.GetFailureHandlingOptions().SetClearAfterRollback(True)
        transaction.SetFailureHandlingOptions(options.SetForcedModalHandling(True).SetFailuresPreprocessor(
            failure_recorder(DB,failures)))
        view = DB.View3D.CreateIsometric(document,types[0].Id)
        view.ViewTemplateId = DB.ElementId.InvalidElementId
        view.Name = "QMonitoring — результат раскладки — "+datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        view.DetailLevel = DB.ViewDetailLevel.Fine
        box = floor.get_BoundingBox(None)
        if box is None:
            raise ValueError("Selected Floor has no native bounding box for view framing")
        padding = DB.UnitUtils.ConvertToInternalUnits(1000,DB.UnitTypeId.Millimeters)
        section = DB.BoundingBoxXYZ()
        section.Min = DB.XYZ(box.Min.X-padding,box.Min.Y-padding,box.Min.Z-padding)
        section.Max = DB.XYZ(box.Max.X+padding,box.Max.Y+padding,box.Max.Z+padding)
        view.IsSectionBoxActive = True
        view.SetSectionBox(section)
        ids = List[DB.ElementId]()
        ids.Add(floor.Id)
        view.SetElementOverrides(floor.Id,DB.OverrideGraphicSettings().SetSurfaceTransparency(75))
        colors = {"bottom-X":(37,99,235),"bottom-Y":(8,145,178),
            "top-X":(234,88,12),"top-Y":(147,51,234)}
        for value in created_ids:
            identifier = DB.ElementId(value)
            rebar = document.GetElement(identifier)
            if not isinstance(rebar,DB.Structure.Rebar):
                raise ValueError("Created Rebar missing during presentation framing")
            ids.Add(identifier)
            rebar.SetUnobscuredInView(view,True)
            color = colors.get((direction_by_id or {}).get(value),(71,85,105))
            view.SetElementOverrides(identifier,DB.OverrideGraphicSettings().SetProjectionLineColor(
                DB.Color(*color)).SetProjectionLineWeight(3))
        view.IsolateElementsTemporary(ids)
        returned = transaction.Commit()
        if returned != DB.TransactionStatus.Committed or transaction.GetStatus() != returned or failures:
            raise ValueError("Presentation view Commit failed or posted warning/error: "+text_type(failures))
        committed = True
        return {"status":"created_pending_whole_party_keep","view_id":element_id(view.Id),
            "view_name":text_type(view.Name),"temporary_isolation":True,"engineering_approval":False,
            "activation":"deferred_until_whole_party_kept"}
    finally:
        if committed:
            transaction.Dispose()
        elif transaction.GetStatus() == DB.TransactionStatus.Uninitialized:
            transaction.Dispose()
        else:
            confirmed,detail = rollback_scope(transaction,DB)
            if not confirmed:
                raise PresentationViewRollbackError("Presentation view rollback unconfirmed: "+text_type(detail))
