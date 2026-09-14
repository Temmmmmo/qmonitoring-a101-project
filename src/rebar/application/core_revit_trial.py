"""Public synthetic core-to-Revit experiment, never an engineering placement permit."""
from __future__ import annotations

import math
from typing import Any

from rebar import Axis, Band, Cell, Direction, Layer, Mosaic
from rebar.legend import parse_recipe
from rebar.optimization import (
    a101_247_slab_recipe_placement, build_composite_zone, build_demand_map,
)
from rebar.optimization.contracts import DemandMap, LayoutConstraints
from rebar.optimization.contracts.placement import CompositeLayoutZone

from .composite_revit_export import build_composite_zone_revit_export


def build_core_revit_trial(
    demand: DemandMap, zone: CompositeLayoutZone, *, constraints: LayoutConstraints | None = None,
) -> dict[str, Any]:
    """Wrap the existing v2 export in an explicit test-only host binding; never lift its gates.

    The first supported profile is one Top X addition Ø18 conditional @150, split
    into two uniform @300 runs. No second addition, actual background or DXF alignment.
    """
    export = build_composite_zone_revit_export(demand, zone, constraints=constraints)
    if (zone.direction != Direction(Layer.TOP, Axis.X) or len(zone.components) != 1
            or zone.recipe != parse_recipe("s300d18+s150d18")):
        raise ValueError("core trial supports only one Top X addition: s300d18+s150d18")
    pattern = zone.components[0].placement.pattern
    if pattern.period_mm != 300 or pattern.offsets_mm != (100, 200):
        raise ValueError("core trial requires the explicit 100/200 axis pattern")
    background, addition = zone.placement.background, zone.placement.additions[0]
    component = zone.components[0]
    x0, y0, length, width = zone.demand_bbox
    if (x0 != 0 or y0 != 0 or not 100 <= length <= 4500 or not 300 <= width <= 4000
            or addition.origin_mm is None or not -10000 <= addition.origin_mm <= 10000
            or background.origin_mm != addition.origin_mm
            or background.pattern.period_mm != 300 or background.pattern.offsets_mm != (0,)
            or background.axis_depth_from_face_mm is not None or addition.axis_depth_from_face_mm is not None):
        raise ValueError("core trial requires a bounded local rectangle and explicit shared lab phase, no Z override")
    runs = export["components"][0]["uniform_runs"]
    if len(runs) != 2 or any(not 2 <= run["bar_count"] <= 16 for run in runs):
        raise ValueError("core trial supports two runs of 2..16 bars")
    if (not math.isclose(component.anchored_length_mm, length + 1440, rel_tol=0, abs_tol=1e-6)
            or not math.isclose(component.installed_length_mm, component.anchored_length_mm, rel_tol=0, abs_tol=1e-6)):
        raise ValueError("core trial profile uses 40d each end without cut-length rounding")
    return {
        "schema_version": "qmonitoring-core-zone-trial/v1",
        "mode": "commit-readback-rollback", "units": "mm", "placement_eligible": False,
        "binding": {
            "host_id": 407801, "bar_type_id": 165160,
            "source": "isolated-laboratory-window-not-project-grid",
            "coordinate_system": "core-xy-plus-offset-from-host-bbox-min",
            "offset_x_mm": 2000, "offset_y_mm": 1000,
            "z_policy": "host-top-cover-model-radius", "background_action": "do-not-create",
            "anchorage_action": "use-installed-length-without-extra-extension",
        },
        "core_export": export,
    }


def make_core_revit_trial_sample(*, length_mm: float = 3900, width_mm: float = 800) -> dict[str, Any]:
    """Build a zone with the real common detailing service, NOT a GA solution or real DXF.

    The explicit laboratory origin 0 is not inferred from the reference slab.
    Default 40d and cutting are applied once by build_composite_zone().
    """
    recipe = parse_recipe("s300d18+s150d18")
    band = Band(0, 2, "s300d18+s150d18", 25.5, recipe.background, recipe.additions[0], recipe)
    cells = []
    for y in (0, width_mm / 2):
        cells.append(Cell([(0, y), (length_mm, y), (length_mm, y + width_mm / 2),
                           (0, y + width_mm / 2)], (length_mm / 2, y + width_mm / 4), 2, band))
    mosaic = Mosaic(Direction(Layer.TOP, Axis.X), cells, [band], (0, 0, length_mm, width_mm),
                    source_path="synthetic-core-trial", meta={"synthetic": True})
    demand = build_demand_map(mosaic)
    placement = a101_247_slab_recipe_placement(recipe, background_origin_mm=0)
    zone = build_composite_zone(demand, demand.bbox, 0, "core-top-x-18-100-200-test", placement)
    return build_core_revit_trial(demand, zone)
