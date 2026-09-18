"""Полный DXF-комплект -> все добавки -> общеплитная номенклатура -> проверяемая выдача.

Не сужает задачу до одного направления/внутренней области. Геометрически неразрешимый
host блокирует полный результат. Без host выдаётся расчётный вариант, не Revit apply.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import math
from pathlib import Path

from rebar.models import Direction
from rebar.optimization.adapters.mosaic import build_demand_map
from rebar.optimization.algorithms.composite_pool import solve_composite_pool
from rebar.optimization.algorithms.stock_length_balance import balance_stock_lengths
from rebar.optimization.contracts.composite_coverage import STO_279_COVERAGE_POLICY
from rebar.optimization.contracts.composite_search import CompositeSearchProblem
from rebar.optimization.contracts.front import ComplexityAxis
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS, _canonical_direction_items
from rebar.optimization.contracts.problem import LayoutConstraints
from rebar.optimization.services.axis_patterns import a101_sto_279_slab_recipe_placement
from rebar.optimization.services.bar_schedule import build_bar_schedule, composite_schedule_groups, straight_bar_key
from rebar.optimization.services.composite_coverage import MAX_CELLS, REMAINING_CHECKS, evaluate_composite_coverage
from rebar.optimization.services.composite_detailing import build_composite_zone
from rebar.optimization.services.composite_host import evaluate_composite_host
from rebar.optimization.services.composite_host_fit import fit_composite_zone_to_host
from rebar.optimization.services.composite_mesh_domain import composite_mesh_domain, clipped_zone_footprint
from rebar.optimization.services.cutting import PLATE_11700_BATCH_PROFILE
from rebar.optimization.services.position_combinations import combine_keyed_candidates
from rebar.optimization.services.stock_cutting import check_stock_cutting
from rebar.reporting.serialization import to_jsonable
from rebar.reporting.composite_svg import render_composite_svg

from .analyze_direction import _cutting_lengths, load_direction_mosaic
from .analyze_plate import PlateDirectionSource
from .composite_host_review import HOST_COORDINATE_POLICY, rectangular_host_from_reference
from .composite_revit_export import build_composite_zone_revit_export
from .working_host import HOST_TRANSLATION_POLICY, host_from_working_input


@dataclass(frozen=True)
class CompositeDirectionSettings:
    direction: Direction
    background_origin_mm: float
    first_300_offset_mm: float
    second_offset_mm: float
    steel_class: str
    source: str
    contact_side: str = "left"

    def __post_init__(self):
        if (not isinstance(self.direction, Direction)
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > 1e9
                       for v in (self.background_origin_mm, self.first_300_offset_mm, self.second_offset_mm))
                or not isinstance(self.steel_class, str) or len(self.steel_class) > 100
                or not isinstance(self.source, str) or not 1 <= len(self.source.strip()) <= 1000
                or self.contact_side not in ("left", "right")):
            raise ValueError("нужны направление, конечные явные фазы, класс стали и источник параметров")


def _placements(demand, settings):
    result = []
    for level in demand.levels:
        if level.recipe is None:
            raise ValueError("нужна явная шкала с полным составом каждого уровня")
        if not level.requires_extra:
            continue
        if len(level.recipe.additions) > 2:
            raise ValueError("профиль поддерживает не более двух добавок; остальные не отброшены")
        placement = a101_sto_279_slab_recipe_placement(level.recipe, background_origin_mm=settings.background_origin_mm,
                                                       contact_side=settings.contact_side)
        additions = tuple(replace(part, origin_mm=settings.background_origin_mm + (
            (0 if spec.step in (100, 150) else settings.first_300_offset_mm) if i == 0 else settings.second_offset_mm
        )) for i, (spec, part) in enumerate(zip(level.recipe.additions, placement.additions)))
        result.append((level.index, replace(placement, additions=additions, source=placement.source + "; " + settings.source)))
    return tuple(result)


def _same_mesh(demands):
    # Compare geometry, not FE ids (DXF has no stable LIRA ids), colors, or bbox alone.
    from shapely.geometry import Polygon
    from shapely.strtree import STRtree

    first = tuple(Polygon(cell.poly) for cell in demands[0].cells)
    tree = STRtree(first)
    for demand in demands[1:]:
        if len(demand.cells) != len(first):
            raise ValueError("четыре направления должны иметь одну и ту же сетку КЭ")
        used = set()
        for cell in demand.cells:
            polygon = Polygon(cell.poly)
            matches = [int(i) for i in tree.query(polygon.buffer(0.001)) if int(i) not in used
                       and polygon.hausdorff_distance(first[int(i)]) <= 0.001
                       and polygon.symmetric_difference(first[int(i)]).area <= max(0.01, polygon.area * 1e-8)]
            if len(matches) != 1:
                raise ValueError("геометрия направлений различается или неоднозначна; комплект не объединён")
            used.add(matches[0])


def _direction_candidate(problem, zones, config, index, *, mesh_domain=None):
    if mesh_domain is None:
        mesh_domain = composite_mesh_domain(problem.demand)
    check = evaluate_composite_coverage(problem.demand, zones, policy_id=problem.policy_id, constraints=problem.constraints)
    if check.status != "pass":
        raise ValueError("кандидат не прошёл независимую повторную проверку полного исходного спроса")
    if check.physical_bar_count > 100000:
        raise ValueError("кандидат превышает лимит 100000 стержней; выдача не обрезана")
    host = (evaluate_composite_host(problem.demand, zones, problem.host_envelope, constraints=problem.constraints)
            if problem.host_envelope is not None else None)
    if host is not None and "fail" in host["checks"].values():
        raise ValueError("изменённая раскладка нарушает границы/проёмы/коллизии host")
    schedule = build_bar_schedule(composite_schedule_groups(zones, steel_class=config.steel_class))
    return {"candidate_index": index, "direction": to_jsonable(config.direction),
        "metrics": {"zone_count": len(zones), "position_count": len(schedule),
                    "physical_bar_count": check.physical_bar_count, "additional_mass_kg": check.additional_mass_kg},
        "coverage": to_jsonable(check), "bar_schedule": to_jsonable(schedule),
        "installation_notes": [{"zone_id": zone.id, "component_index": c.component_index,
            "note": "Стержень дополнительного армирования, находящийся рядом с фоновым армированием, "
                    "должен быть уложен вплотную, без зазора. СТО 5.5 рев3, табл. 2.7.9."}
            for zone in zones for c in zone.components if c.rebar.step == 100],
        "zone_drafts": [build_composite_zone_revit_export(problem.demand, zone, constraints=problem.constraints) for zone in zones],
        "zone_footprints": [{"source_zone_id": zone.id, **clipped_zone_footprint(mesh_domain, zone.demand_bbox)} for zone in zones],
        "svg": render_composite_svg(problem.demand, zones, host_envelope=problem.host_envelope,
                                    mesh_domain=mesh_domain), "host_preflight": host}


def _select(front):
    if not front:
        return None
    masses, counts = [p["additional_mass_kg"] for p in front], [p["position_count"] for p in front]
    return min(range(len(front)), key=lambda i: (
        ((masses[i] - min(masses)) / max(max(masses) - min(masses), 1e-6)) ** 2
        + ((counts[i] - min(counts)) / max(max(counts) - min(counts), 1)) ** 2, masses[i]))


def analyze_composite_plate(
    sources: tuple[PlateDirectionSource, ...], settings: tuple[CompositeDirectionSettings, ...], *,
    maximum_zones_per_direction: int = 64, maximum_positions: int | None = None,
    maximum_candidates: int = 128, solver_time_limit_s: float = 10,
    min_width_cells: int = 2, cutting_profile: str = "plate-11700", case_id: str = "",
    host_reference: dict | None = None, coordinate_policy: str | None = None,
    maximum_cutting_overhead_pct: float = 5,
) -> dict:
    """Один сценарий для API/CLI; класс и фазы задаёт вызывающая сторона, не алгоритм."""
    if len(sources) != 4:
        raise ValueError("нужны ровно четыре DXF, а не частичная выдача")
    ordered_settings = _canonical_direction_items(settings, lambda s: s.direction, item_name="Профиль укладки")
    if min_width_cells not in (2, 3) or isinstance(min_width_cells, bool):
        raise ValueError("минимальная ширина — 2 или 3 КЭ")
    if maximum_positions is not None and (isinstance(maximum_positions, bool)
            or not isinstance(maximum_positions, int) or not 1 <= maximum_positions <= 512):
        raise ValueError("лимит позиций всей плиты должен быть 1..512")
    if (host_reference is None) != (coordinate_policy is None):
        raise ValueError("host и явная политика координат передаются вместе")
    if host_reference is not None and coordinate_policy not in (HOST_COORDINATE_POLICY, HOST_TRANSLATION_POLICY):
        raise ValueError("неподдержанная политика координат host")
    if (isinstance(maximum_cutting_overhead_pct, bool) or not isinstance(maximum_cutting_overhead_pct, (int, float))
            or not math.isfinite(maximum_cutting_overhead_pct)
            or not 0 <= maximum_cutting_overhead_pct <= 100):
        raise ValueError("лимит прироста массы от раскроя должен быть 0..100 процентов")
    constraints = LayoutConstraints(min_width_cells=min_width_cells,
        allowed_cut_lengths_mm=_cutting_lengths("plate-11700" if cutting_profile == PLATE_11700_BATCH_PROFILE else cutting_profile),
        cutting_profile=cutting_profile)
    parsed = []
    for source in sources:
        paths = {"dxf": Path(source.dxf_path)}
        if source.shk_path is not None:
            paths["shk"] = Path(source.shk_path)
        if source.png_path is not None:
            paths["png"] = Path(source.png_path)
        hashes = {role: hashlib.sha256(path.read_bytes()).hexdigest() for role, path in paths.items()}
        mosaic = load_direction_mosaic(source.dxf_path, shk_path=source.shk_path,
                                       png_path=source.png_path, mapping_id=source.mapping_id)
        if any(hashlib.sha256(path.read_bytes()).hexdigest() != hashes[role] for role, path in paths.items()):
            raise ValueError("вход изменился во время чтения")
        parsed.append((mosaic, {"filenames": {role: path.name for role, path in paths.items()},
                                "sha256": hashes, "mapping_id": source.mapping_id}))
    parsed = _canonical_direction_items(tuple(parsed), lambda pair: pair[0].direction, item_name="DXF-комплект")
    demands = tuple(build_demand_map(mosaic) for mosaic, _ in parsed)
    if any(len(demand.cells) > MAX_CELLS for demand in demands):
        raise ValueError("превышен лимит 10000 КЭ на направление")
    _same_mesh(demands)
    try:
        host = ((host_from_working_input(host_reference) if coordinate_policy == HOST_TRANSLATION_POLICY
                 else rectangular_host_from_reference(host_reference)) if host_reference is not None else None)
    except (KeyError, TypeError, IndexError, AttributeError) as error:
        raise ValueError("неполный или неверно структурированный снимок host") from error
    problems = tuple(CompositeSearchProblem(demand, _placements(demand, config), constraints,
                     STO_279_COVERAGE_POLICY, host) for demand, config in zip(demands, ordered_settings))
    mesh_domains = tuple(composite_mesh_domain(demand) for demand in demands)
    searches = tuple(solve_composite_pool(problem, maximum_zones=maximum_zones_per_direction,
        maximum_candidates=maximum_candidates, solver_time_limit_s=solver_time_limit_s,
        maximum_bar_length_mm=11700, complexity_axis=ComplexityAxis.POSITION_COUNT,
        maximum_positions=maximum_positions, steel_class=config.steel_class, retain_position_alternatives=True)
        for problem, config in zip(problems, ordered_settings))
    by_direction, choice_groups = [], []
    for problem, search, config, (_, source), mesh_domain in zip(problems, searches, ordered_settings, parsed, mesh_domains):
        options = []
        for point in search.points:
            options.append(_direction_candidate(problem, point.zones, config, len(options), mesh_domain=mesh_domain))
        by_direction.append({"direction": to_jsonable(config.direction), "source": source,
            "source_cell_count": len(problem.demand.cells), "settings": to_jsonable(config),
            "input_svg": render_composite_svg(problem.demand, (), host_envelope=host, outline_cell_ids=(
                c["cell_id"] for c in search.telemetry.get("host_demand_feasibility", {}).get("cells", ()))),
            "candidates": options, "telemetry": search.telemetry})
        choice_groups.append(tuple((i, j) for j in range(len(options)) for i in (len(by_direction) - 1,)))

    def candidate(choice):
        return by_direction[choice[0]]["candidates"][choice[1]]

    def keys(choice):
        return frozenset(straight_bar_key(p["diameter_mm"], p["length_mm"], p["steel_class"])
                         for p in candidate(choice)["bar_schedule"])

    combinations = combine_keyed_candidates(tuple(choice_groups), keys_of=keys,
                                            mass_of=lambda choice: candidate(choice)["metrics"]["additional_mass_kg"])
    combined = []
    for choices in combinations:
        schedule = build_bar_schedule(group for i, j in choices for group in composite_schedule_groups(
            searches[i].points[j].zones, steel_class=ordered_settings[i].steel_class))
        if maximum_positions is not None and len(schedule) > maximum_positions:
            continue
        combined.append({"direction_candidate_indexes": [j for _, j in choices], "position_count": len(schedule),
                         "zone_count": sum(candidate(choice)["metrics"]["zone_count"] for choice in choices),
                         "physical_bar_count": sum(row.physical_bar_count for row in schedule),
                         "additional_mass_kg": math.fsum(row.total_mass_kg for row in schedule),
                         "bar_schedule": schedule})
    combined.sort(key=lambda p: (p["position_count"], p["additional_mass_kg"], p["direction_candidate_indexes"]))
    front, best_mass = [], math.inf
    for point in combined:
        if point["additional_mass_kg"] < best_mass - 1e-6:
            front.append(point)
            best_mass = point["additional_mass_kg"]
    for point in front:
        point["stock_cutting"] = check_stock_cutting(point["bar_schedule"])
        point["bar_schedule"] = to_jsonable(point["bar_schedule"])
    selected = _select(front)
    diagnostic_front, balance_attempts = [], []
    if cutting_profile == PLATE_11700_BATCH_PROFILE and front:
        balanced = [point for point in front if point["stock_cutting"]["status"] == "pass"]
        for original_index in dict.fromkeys((selected, min(range(len(front)), key=lambda i: front[i]["additional_mass_kg"]))):
            point = front[original_index]
            if point["stock_cutting"]["status"] == "pass":
                continue
            zone_groups = tuple(searches[i].points[j].zones for i, j in enumerate(point["direction_candidate_indexes"]))
            groups = tuple(group for zones, config in zip(zone_groups, ordered_settings)
                           for group in composite_schedule_groups(zones, steel_class=config.steel_class))
            balance = balance_stock_lengths(groups, maximum_mass_increase_pct=maximum_cutting_overhead_pct,
                                             maximum_positions=maximum_positions, time_limit_s=min(solver_time_limit_s, 30))
            attempt = {"original_candidate_index": original_index, **to_jsonable(balance)}
            balance_attempts.append(attempt)
            if balance.status != "balanced":
                continue
            lengths = dict(balance.installed_lengths_mm)
            rebuilt = []
            try:
                for i, (zones, problem, config) in enumerate(zip(zone_groups, problems, ordered_settings)):
                    changed = tuple(build_composite_zone(problem.demand, zone.demand_bbox, zone.level_index, zone.id,
                        zone.placement, constraints=constraints, installed_lengths_mm=tuple(
                            lengths[f"{zone.direction}:{zone.id}:{component.component_index}"] for component in zone.components)) for zone in zones)
                    if problem.host_envelope is not None:
                        changed = tuple(fit_composite_zone_to_host(problem.demand, zone, problem.host_envelope,
                            constraints=constraints) for zone in changed)
                    rebuilt.append(_direction_candidate(problem, changed, config, len(by_direction[i]["candidates"]),
                                                        mesh_domain=mesh_domains[i]))
            except ValueError as error:
                attempt.update(status="rejected_after_geometry_check", geometry_error=str(error))
                continue
            schedule = build_bar_schedule(group for i, zones in enumerate(zone_groups) for group in (
                replace(g, installed_length_mm=lengths[g.source_id])
                for g in composite_schedule_groups(zones, steel_class=ordered_settings[i].steel_class)))
            actual_mass = math.fsum(option["metrics"]["additional_mass_kg"] for option in rebuilt)
            actual_count = sum(option["metrics"]["physical_bar_count"] for option in rebuilt)
            if (actual_count != point["physical_bar_count"]
                    or actual_mass > point["additional_mass_kg"] * (1 + maximum_cutting_overhead_pct / 100) + 1e-6
                    or abs(actual_mass - math.fsum(row.total_mass_kg for row in schedule)) > 1e-6
                    or maximum_positions is not None and len(schedule) > maximum_positions):
                attempt.update(status="rejected_after_geometry_check", geometry_error="масса/количество/позиции после детализации нарушают лимит")
                continue
            attempt["telemetry"].update(geometry_checked=True,
                geometry_scope="coverage-and-snapshot-host" if host is not None else "coverage-only-no-host")
            indexes = [len(d["candidates"]) for d in by_direction]
            for direction, option in zip(by_direction, rebuilt):
                direction["candidates"].append(option)
            balanced.append({"direction_candidate_indexes": indexes, "position_count": len(schedule),
                "zone_count": point["zone_count"], "physical_bar_count": point["physical_bar_count"],
                "additional_mass_kg": actual_mass, "bar_schedule": to_jsonable(schedule),
                "stock_cutting": balance.telemetry["stock_cutting"],
                "mass_before_balancing_kg": balance.original_mass_kg,
                "mass_increase_pct": (actual_mass / balance.original_mass_kg - 1) * 100})
        # Different cutting-feasibility classes are never silently mixed on one Pareto curve.
        if balanced:
            diagnostic_front = front
            front, best_mass = [], math.inf
            for point in sorted(balanced, key=lambda p: (p["position_count"], p["additional_mass_kg"])):
                if point["additional_mass_kg"] < best_mass - 1e-6:
                    front.append(point)
                    best_mass = point["additional_mass_kg"]
            selected = _select(front)
    blocks = [*REMAINING_CHECKS, "stock-cutting-manufacturing-assumptions"]
    if any(spec.step == 100 for demand in demands for level in demand.levels for spec in level.recipe.additions):
        blocks.append("coplanar-background-contact-and-depths")
    if host is not None:
        blocks.extend(("live-host-geometry", "top-bottom-cover"))
    if not front:
        blocks.append("full-plate-solution-not-found")
    elif front[selected]["stock_cutting"]["status"] != "pass":
        blocks.append("stock-cutting-zero-waste")
    return {"schema_version": "composite-plate-analysis/v1", "units": "mm", "case_id": case_id,
        "status": "full_coverage_candidates_found" if front else "no_full_plate_solution_found",
        "placement_eligible": False, "source_demand_preserved": True, "averaging": "not_applied",
        "zone_boundary_policy": "zone_footprints_clipped_to_union_of_source_kleenka_cells",
        "constraints": to_jsonable(constraints), "direction_count": len(PLATE_DIRECTIONS),
        "directions": by_direction, "front": front, "selected_index": selected,
        "diagnostic_front_before_cutting": diagnostic_front, "length_balance_attempts": balance_attempts,
        "maximum_cutting_overhead_pct": maximum_cutting_overhead_pct,
        "host_envelope": to_jsonable(host), "host_coordinate_policy": coordinate_policy,
        "front_scope": "zero-waste-candidates" if diagnostic_front else "coverage-candidates-with-cutting-status",
        "selection": "equal_weight_normalized_mass_and_specification_positions",
        "blocking_check_ids": blocks,
        "warning": "Расчёт всех четырёх направлений без удаления краёв. Конечный набор кандидатов, "
                   "не глобальный оптимум. Фазы заданы пользователем, высоты не назначены. "
                   "Ведомость и пакет зон — черновик, не команда размещения в Revit. "
                   "Профиль batch согласует длины выбранных наборов с раскроем; количество стержней неизменно. "
                   "Прочие профили только проверяют раскрой. Удлинённые стержни проходят повторную геометрическую проверку."}
