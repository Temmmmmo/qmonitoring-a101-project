"""Составная схема: каждая добавка и её реальные оси, не старый равномерный LayoutZone."""
import html

from shapely.geometry import box as geometry_box

from rebar.optimization.services.composite_mesh_domain import composite_mesh_domain

from rebar.models import Axis
from rebar.optimization.services.axis_patterns import pattern_coordinates

from .svg import ZONE_COLORS, _aci_hex
from .source_graphics import _box, render_source_zone_layers


def render_composite_svg(demand, zones, *, host_envelope=None, outline_cell_ids=(), physical_bars=None,
                         host_rings_mm=(), source_zone_drafts=(), host_opening_rings_mm=(), mesh_domain=None) -> str:
    if physical_bars is not None and zones:
        raise ValueError("Render physical bars or source zones, not both inventories at once")
    boxes = [demand.bbox]
    if source_zone_drafts and (zones or physical_bars is None):
        raise ValueError("Source overlay requires a separate physical inventory")
    for source in source_zone_drafts:
        if source["direction"] != {"layer": demand.direction.layer.value, "axis": demand.direction.axis.value}:
            raise ValueError("Source overlay direction differs from physical bars")
        boxes.append(_box(source["demand_bbox_mm"]))
        boxes.extend(_box(c["bar_axis_bbox_mm"]) for c in source["components"])
    if host_envelope is not None:
        boxes.append(host_envelope.outer_mm)
    for ring in (*host_rings_mm, *host_opening_rings_mm):
        boxes.append((min(p[0] for p in ring), min(p[1] for p in ring),
                      max(p[0] for p in ring), max(p[1] for p in ring)))
    outlined = frozenset(outline_cell_ids)
    lines = []
    for zone in zones:
        for component in zone.components:
            start, end = component.longitudinal_interval_mm
            for coord in pattern_coordinates(component.placement, component.axis_window_mm):
                box = (start, coord, end, coord) if demand.direction.axis is Axis.X else (coord, start, coord, end)
                boxes.append(box)
                title = (f"{zone.id}; добавка {component.component_index + 1}; Ø{component.rebar.diameter}; "
                         f"L={component.installed_length_mm:.3f} мм; условный шаг {component.rebar.step}")
                lines.append((box, title, ZONE_COLORS[component.component_index % len(ZONE_COLORS)]))
    for bar in physical_bars or ():
        start, end = bar["longitudinal_mm"]
        coord = bar["coordinate_mm"]
        bounds = (start, coord, end, coord) if demand.direction.axis is Axis.X else (coord, start, coord, end)
        boxes.append(bounds)
        lines.append((bounds, f"{bar['id']}; Ø{bar['diameter_mm']}; L={end-start:.3f} мм; "
                      "физический стержень после обработки", ZONE_COLORS[0]))
    xmin, ymin = min(b[0] for b in boxes), min(b[1] for b in boxes)
    xmax, ymax = max(b[2] for b in boxes), max(b[3] for b in boxes)
    width, height = max(1, xmax - xmin), max(1, ymax - ymin)
    cells = []
    for cell in demand.cells:
        points = " ".join(f"{x-xmin:.3f},{ymax-y:.3f}" for x, y in cell.poly)
        color = _aci_hex(cell.aci) if cell.aci else "#d9e4ec"
        outline = (' stroke="#dc2626" stroke-width="2" vector-effect="non-scaling-stroke"'
                   if cell.id in outlined else "")
        note = "; противоречие продольной анкеровке у края" if cell.id in outlined else ""
        cells.append(f'<polygon data-cell-id="{html.escape(str(cell.id), quote=True)}" points="{points}" '
                     f'fill="{color}" fill-opacity="0.35"{outline}><title>'
                     f'КЭ {html.escape(str(cell.id))}; уровень {cell.level_index}{note}</title></polygon>')
    bars = []
    for (x1, y1, x2, y2), title, color in lines:
        bars.append(f'<line x1="{x1-xmin:.3f}" x2="{x2-xmin:.3f}" y1="{ymax-y1:.3f}" y2="{ymax-y2:.3f}" '
                    f'stroke="{color}" stroke-width="1" vector-effect="non-scaling-stroke"><title>'
                    f'{html.escape(title)}</title></line>')
    contours = []
    for ring in host_rings_mm:
        points = " ".join(f"{x-xmin:.3f},{ymax-y:.3f}" for x, y in ring)
        contours.append(f'<polygon points="{points}" fill="none" stroke="#111827" stroke-width="2" '
                        'vector-effect="non-scaling-stroke"><title>Внешний контур сечения рабочего host; '
                        'защитный слой не добавлен к обрезке</title></polygon>')
    openings = []
    for ring in host_opening_rings_mm:
        points = " ".join(f"{x-xmin:.3f},{ymax-y:.3f}" for x, y in ring)
        openings.append(f'<polygon points="{points}" fill="none" stroke="#dc2626" stroke-width="2" '
                        'vector-effect="non-scaling-stroke"><title>Отверстие рабочего host; '
                        'реальные отрезки разрезаны по границе, без добавленного защитного слоя</title></polygon>')
    source_zones = []
    source_envelopes = []
    if zones and mesh_domain is None:
        mesh_domain = composite_mesh_domain(demand)
    if source_zone_drafts:
        rectangles, envelopes = render_source_zone_layers(source_zone_drafts, xmin=xmin, ymax=ymax,
                                                          span=max(width, height))
        source_zones.append(rectangles)
        source_envelopes.append(envelopes)
    for index, zone in enumerate(zones, 1):
        for component in zone.components:
            start, end = component.longitudinal_interval_mm
            axes = pattern_coordinates(component.placement, component.axis_window_mm)
            bx1, by1, bx2, by2 = ((start, axes[0], end, axes[-1]) if demand.direction.axis is Axis.X
                                  else (axes[0], start, axes[-1], end))
            source_envelopes.append(f'<rect data-component-index="{component.component_index}" '
                f'x="{bx1-xmin:.3f}" y="{ymax-by2:.3f}" width="{bx2-bx1:.3f}" height="{by2-by1:.3f}" '
                f'fill="none" stroke="#7b426f" stroke-dasharray="5 4" stroke-width="1" '
                f'vector-effect="non-scaling-stroke"><title>Z{index} / {component.component_index+1}; '
                f'исходная огибающая осей после40d/раскроя, не AreaBoundary и не нормализованная партия; '
                f'Ø{component.rebar.diameter}; L={component.installed_length_mm:.3f} мм; '
                f'условный шаг {component.rebar.step}; {component.bar_count} шт.</title></rect>')
        clipped = geometry_box(*zone.demand_bbox).intersection(mesh_domain)
        polygons = (clipped,) if clipped.geom_type == "Polygon" else tuple(
            item for item in getattr(clipped, "geoms", ()) if item.geom_type == "Polygon")
        paths = []
        for polygon in polygons:
            for ring in (polygon.exterior, *polygon.interiors):
                points = list(ring.coords)
                paths.append("M " + " L ".join(f"{x-xmin:.3f} {ymax-y:.3f}" for x, y in points) + " Z")
        if not paths:
            raise ValueError("зона не пересекает исходную сетку КЭ")
        label_point = clipped.representative_point()
        x1, y1, x2, y2 = zone.demand_bbox
        source_zones.append(f'<g data-zone-id="{html.escape(zone.id, quote=True)}">'
            f'<path d="{" ".join(paths)}" fill="#173f61" fill-opacity="0.07" fill-rule="evenodd" '
            f'stroke="#173f61" stroke-width="1.8" vector-effect="non-scaling-stroke">'
            f'<title>Z{index} · {html.escape(zone.id)}; действующая зона по КЭ, '
            f'обрезана от расчётного окна {x2-x1:.3f} × {y2-y1:.3f} мм; '
            f'не физический контур стали</title></path>'
            f'<text x="{label_point.x-xmin:.3f}" y="{ymax-label_point.y:.3f}" font-size="{max(width, height)*.009:.3f}" '
            f'fill="#173f61" paint-order="stroke" stroke="white" '
            f'stroke-width="{max(width, height)*.0015:.3f}">Z{index}</text></g>')
    if host_envelope is not None:
        for i, (x1, y1, x2, y2) in enumerate((host_envelope.outer_mm, *host_envelope.openings_mm)):
            contours.append(f'<rect x="{x1-xmin:.3f}" y="{ymax-y2:.3f}" width="{x2-x1:.3f}" height="{y2-y1:.3f}" '
                            f'fill="none" stroke="{"#dc2626" if i else "#111827"}" stroke-width="2" '
                            f'vector-effect="non-scaling-stroke"><title>{"Проём" if i else "Контур host"}</title></rect>')
    label = "Фактические оси" if lines else "Исходный спрос; раскладка отсутствует"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.3f} {height:.3f}" role="img" '
            f'aria-label="{label} {html.escape(str(demand.direction))}">'
            f'<g class="cells">{"".join(cells)}</g><g class="bar-axes">{"".join(bars)}</g>'
            f'<g class="source-component-envelopes">{"".join(source_envelopes)}</g>'
            f'<g class="source-zones">{"".join(source_zones)}</g>'
            f'<g class="host-contours">{"".join(contours)}</g>'
            f'<g class="host-openings">{"".join(openings)}</g></svg>')
