"""CSV/JSON/HTML-отчёт серии генетических экспериментов."""

from __future__ import annotations

import base64
import csv
import html
import json
from dataclasses import asdict
from pathlib import Path
from statistics import median

from rebar.application.gate_assessment import assess_plate_gates
from rebar.application.genetic_benchmark import GeneticRunResult
from rebar.golden import (
    GoldenCaseDefinition,
    PlateMetricReference,
    plate_metric_reference_from_golden,
    render_pdf_page_png,
    resolve_golden_case_files,
)
from rebar.optimization.services import rebar_mass_kg
from rebar.reporting.svg import render_solution_svg

MASS_GATE_MAX_DELTA_PCT = 15.0
BenchmarkReference = GoldenCaseDefinition | PlateMetricReference


def _metric_reference(reference: BenchmarkReference) -> PlateMetricReference:
    if isinstance(reference, GoldenCaseDefinition):
        return plate_metric_reference_from_golden(reference)
    return reference


def _relative(actual: float, target: float) -> float | None:
    return None if target == 0 else (actual - target) / target * 100.0


def _data_uri(data: bytes, media_type: str = "image/png") -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def _mass_breakdown(solution) -> dict[str, float]:
    required_mass = 0.0
    anchored_mass = 0.0
    installed_mass = 0.0
    for direction_solution in solution.direction_solutions:
        for zone in direction_solution.solution.zones:
            required_mass += rebar_mass_kg(
                zone.rebar.diameter,
                zone.required_length_mm,
                zone.bar_count,
            )
            anchored_mass += rebar_mass_kg(
                zone.rebar.diameter,
                zone.anchored_length_mm,
                zone.bar_count,
            )
            installed_mass += zone.mass_kg
    return {
        "required_mass_kg": required_mass,
        "anchorage_addition_kg": anchored_mass - required_mass,
        "cutting_addition_kg": installed_mass - anchored_mass,
        "installed_mass_kg": installed_mass,
    }


def _direction_mass_breakdown(solution) -> dict[str, float]:
    required_mass = sum(
        rebar_mass_kg(
            zone.rebar.diameter,
            zone.required_length_mm,
            zone.bar_count,
        )
        for zone in solution.zones
    )
    anchored_mass = sum(
        rebar_mass_kg(
            zone.rebar.diameter,
            zone.anchored_length_mm,
            zone.bar_count,
        )
        for zone in solution.zones
    )
    installed_mass = sum(zone.mass_kg for zone in solution.zones)
    return {
        "required_mass_kg": required_mass,
        "anchorage_addition_kg": anchored_mass - required_mass,
        "cutting_addition_kg": installed_mass - anchored_mass,
        "installed_mass_kg": installed_mass,
    }


def _selected_candidate(results: tuple[GeneticRunResult, ...]):
    available = (
        (result, candidate)
        for result in results
        if result.analysis.front is not None
        for candidate in result.analysis.front.candidates
    )
    try:
        return min(
            available,
            key=lambda item: (
                item[1].solution.metrics.total_mass_kg,
                item[1].solution.metrics.zone_count,
                item[0].config.id,
                item[1].id,
            ),
        )
    except ValueError as error:
        raise ValueError("benchmark не содержит допустимых общеплитных кандидатов") from error


def _comparison_payload(
    results: tuple[GeneticRunResult, ...],
    reference: BenchmarkReference | None,
) -> dict | None:
    if reference is None:
        return None
    metric_reference = _metric_reference(reference)
    result, candidate = _selected_candidate(results)
    metrics = candidate.solution.metrics
    mass_delta_pct = _relative(metrics.total_mass_kg, metric_reference.expected_mass_kg)
    if mass_delta_pct is None:
        raise ValueError("масса golden-case должна быть положительной")
    gates = assess_plate_gates(
        result.analysis.problem,
        candidate.solution,
        reference=metric_reference,
    )
    directions = []
    if isinstance(reference, GoldenCaseDefinition):
        for expectation in reference.sheets:
            solution = candidate.solution.solution(expectation.direction)
            mass_breakdown = _direction_mass_breakdown(solution)
            directions.append(
                {
                    "direction": str(expectation.direction),
                    "title": expectation.title,
                    "engineer_axis_label": expectation.engineer_axis_label,
                    "pdf_page": expectation.pdf_page,
                    "engineer": {
                        "position_count": expectation.expected_position_count,
                        "physical_bar_count": expectation.expected_bar_count,
                        "total_mass_kg": expectation.expected_mass_kg,
                    },
                    "ours": {
                        "zone_count": solution.metrics.detail_count,
                        "physical_bar_count": solution.metrics.physical_bar_count,
                        "total_mass_kg": solution.metrics.total_mass_kg,
                        "under_reinforced_cell_count": (
                            solution.metrics.under_reinforced_cell_count
                        ),
                        "mass_breakdown": mass_breakdown,
                    },
                    "mass_delta_kg": (
                        solution.metrics.total_mass_kg - expectation.expected_mass_kg
                    ),
                    "mass_delta_pct": _relative(
                        solution.metrics.total_mass_kg,
                        expectation.expected_mass_kg,
                    ),
                    "bar_delta": (
                        solution.metrics.physical_bar_count
                        - expectation.expected_bar_count
                    ),
                    "bar_delta_pct": _relative(
                        float(solution.metrics.physical_bar_count),
                        float(expectation.expected_bar_count),
                    ),
                }
            )
    return {
        "reference_kind": metric_reference.source_kind,
        "selection_policy": "minimum_mass_on_generated_valid_plate_front",
        "mass_gate_target_pct": MASS_GATE_MAX_DELTA_PCT,
        "run_id": result.config.id,
        "candidate_id": candidate.id,
        "engineer": {
            "position_count": metric_reference.expected_position_count,
            "physical_bar_count": metric_reference.expected_bar_count,
            "total_mass_kg": metric_reference.expected_mass_kg,
        },
        "ours": {
            "zone_count": metrics.zone_count,
            "physical_bar_count": metrics.physical_bar_count,
            "total_mass_kg": metrics.total_mass_kg,
            "under_reinforced_cell_count": metrics.under_reinforced_cell_count,
            "mass_breakdown": _mass_breakdown(candidate.solution),
        },
        "mass_delta_kg": metrics.total_mass_kg - metric_reference.expected_mass_kg,
        "mass_delta_pct": mass_delta_pct,
        "mass_gate_gap_pct": max(
            0.0,
            mass_delta_pct - MASS_GATE_MAX_DELTA_PCT,
        ),
        "mass_gate_met": mass_delta_pct <= MASS_GATE_MAX_DELTA_PCT,
        "bar_delta": metrics.physical_bar_count - metric_reference.expected_bar_count,
        "bar_delta_pct": _relative(
            float(metrics.physical_bar_count),
            float(metric_reference.expected_bar_count),
        ),
        "gate_summary": gates.summary,
        "directions": directions,
    }


def _candidate_rows(
    results: tuple[GeneticRunResult, ...],
    reference: BenchmarkReference | None,
) -> list[dict]:
    metric_reference = None if reference is None else _metric_reference(reference)
    rows = []
    for result in results:
        front = result.analysis.front
        if front is None:
            continue
        for index, candidate in enumerate(front.candidates, 1):
            metrics = candidate.solution.metrics
            mass_breakdown = _mass_breakdown(candidate.solution)
            mass_delta_pct = (
                None
                if metric_reference is None
                else _relative(
                    metrics.total_mass_kg,
                    metric_reference.expected_mass_kg,
                )
            )
            gates = assess_plate_gates(
                result.analysis.problem,
                candidate.solution,
                reference=metric_reference,
            )
            rows.append(
                {
                    "run_id": result.config.id,
                    "candidate_index": index,
                    "candidate_id": candidate.id,
                    "complexity_axis": front.complexity_axis.value,
                    "complexity": candidate.constructability.value(
                        front.complexity_axis
                    ),
                    "zone_count": metrics.zone_count,
                    "physical_bar_count": metrics.physical_bar_count,
                    "total_mass_kg": metrics.total_mass_kg,
                    **mass_breakdown,
                    "under_reinforced_cell_count": (
                        metrics.under_reinforced_cell_count
                    ),
                    "valid": candidate.solution.valid,
                    "runtime_ms": candidate.solution.runtime_ms,
                    "mass_delta_kg": (
                        None
                        if metric_reference is None
                        else metrics.total_mass_kg
                        - metric_reference.expected_mass_kg
                    ),
                    "mass_delta_pct": (
                        mass_delta_pct
                    ),
                    "mass_gate_gap_pct": (
                        None
                        if mass_delta_pct is None
                        else max(
                            0.0,
                            mass_delta_pct - MASS_GATE_MAX_DELTA_PCT,
                        )
                    ),
                    "mass_gate_met": (
                        None
                        if mass_delta_pct is None
                        else mass_delta_pct <= MASS_GATE_MAX_DELTA_PCT
                    ),
                    "bar_delta": (
                        None
                        if metric_reference is None
                        else metrics.physical_bar_count
                        - metric_reference.expected_bar_count
                    ),
                    "bar_delta_pct": (
                        None
                        if metric_reference is None
                        else _relative(
                            float(metrics.physical_bar_count),
                            float(metric_reference.expected_bar_count),
                        )
                    ),
                    **{f"gates_{key}": value for key, value in gates.summary.items()},
                }
            )
    return rows


def _hypervolume_bounds(
    results: tuple[GeneticRunResult, ...],
) -> tuple[float, float, float, float] | None:
    points = [
        (
            float(candidate.constructability.value(front.complexity_axis)),
            candidate.solution.metrics.total_mass_kg,
        )
        for result in results
        if result.analysis.front is not None
        for front in (result.analysis.front,)
        for candidate in front.candidates
    ]
    if not points:
        return None
    minimum_complexity = min(point[0] for point in points)
    maximum_complexity = max(point[0] for point in points)
    minimum_mass = min(point[1] for point in points)
    maximum_mass = max(point[1] for point in points)
    complexity_margin = max(1.0, (maximum_complexity - minimum_complexity) * 0.1)
    mass_margin = max(1.0, (maximum_mass - minimum_mass) * 0.1)
    return (
        minimum_complexity,
        minimum_mass,
        maximum_complexity + complexity_margin,
        maximum_mass + mass_margin,
    )


def _normalized_hypervolume(
    result: GeneticRunResult,
    bounds: tuple[float, float, float, float] | None,
) -> float:
    """Площадь 2D Pareto-front в общих для серии нормализованных границах."""

    front = result.analysis.front
    if front is None or not front.candidates or bounds is None:
        return 0.0
    ideal_complexity, ideal_mass, reference_complexity, reference_mass = bounds
    points = sorted(
        {
            (
                float(candidate.constructability.value(front.complexity_axis)),
                candidate.solution.metrics.total_mass_kg,
            )
            for candidate in front.candidates
        }
    )
    area = 0.0
    previous_mass = reference_mass
    for complexity, mass in points:
        if mass >= previous_mass:
            continue
        area += max(0.0, reference_complexity - complexity) * (
            previous_mass - mass
        )
        previous_mass = mass
    full_area = (reference_complexity - ideal_complexity) * (
        reference_mass - ideal_mass
    )
    return 0.0 if full_area <= 0.0 else area / full_area


def _operator_learning_summary(result: GeneticRunResult) -> dict[str, object]:
    accumulated: dict[str, dict[str, float | int]] = {}
    policy_names: set[str] = set()
    exploration: set[float] = set()
    for analysis in result.analysis.direction_analyses:
        solutions = analysis.candidate_solutions or analysis.solutions
        if not solutions:
            continue
        telemetry = solutions[0].meta.get("operator_learning", {})
        if not isinstance(telemetry, dict):
            continue
        if telemetry.get("name"):
            policy_names.add(str(telemetry["name"]))
        if telemetry.get("exploration") is not None:
            exploration.add(float(telemetry["exploration"]))
        for item in telemetry.get("operators", ()):
            if not isinstance(item, dict) or not item.get("name"):
                continue
            target = accumulated.setdefault(
                str(item["name"]),
                {"selections": 0, "reward_sum": 0.0, "positive_rewards": 0},
            )
            target["selections"] += int(item.get("selections", 0))
            target["reward_sum"] += float(item.get("reward_sum", 0.0))
            target["positive_rewards"] += int(item.get("positive_rewards", 0))
    for item in accumulated.values():
        selections = int(item["selections"])
        item["mean_reward"] = (
            0.0 if selections == 0 else float(item["reward_sum"]) / selections
        )
    return {
        "policy_names": sorted(policy_names),
        "exploration": sorted(exploration),
        "total_selections": sum(
            int(item["selections"]) for item in accumulated.values()
        ),
        "operators": accumulated,
    }


def _run_rows(
    results: tuple[GeneticRunResult, ...],
    reference: BenchmarkReference | None,
) -> list[dict]:
    metric_reference = None if reference is None else _metric_reference(reference)
    rows = []
    hypervolume_bounds = _hypervolume_bounds(results)
    for result in results:
        front = result.analysis.front
        candidates = () if front is None else front.candidates
        minimum_mass_candidate = (
            None
            if not candidates
            else min(
                candidates,
                key=lambda item: (
                    item.solution.metrics.total_mass_kg,
                    item.solution.metrics.physical_bar_count,
                    item.solution.metrics.zone_count,
                ),
            )
        )
        minimum_mass_delta_pct = (
            None
            if metric_reference is None or minimum_mass_candidate is None
            else _relative(
                minimum_mass_candidate.solution.metrics.total_mass_kg,
                metric_reference.expected_mass_kg,
            )
        )
        minimum_mass_bar_delta_pct = (
            None
            if metric_reference is None or minimum_mass_candidate is None
            else _relative(
                float(minimum_mass_candidate.solution.metrics.physical_bar_count),
                float(metric_reference.expected_bar_count),
            )
        )
        rows.append(
            {
                "run_id": result.config.id,
                **{
                    key: (
                        value.value
                        if hasattr(value, "value")
                        else value
                    )
                    for key, value in asdict(result.config).items()
                },
                "plate_front_size": len(candidates),
                "plate_combination_count": (
                    0 if front is None else front.combination_count
                ),
                "direction_source_candidate_count": (
                    0
                    if front is None
                    else sum(
                        item.source_candidate_count
                        for item in front.direction_fronts
                    )
                ),
                "direction_rejected_candidate_count": (
                    0
                    if front is None
                    else sum(len(item.rejections) for item in front.direction_fronts)
                ),
                "single_cell_downgrade_count": sum(
                    analysis.problem.meta.get(
                        "single_cell_preprocessing",
                        {},
                    ).get("changed_count", 0)
                    for analysis in result.analysis.direction_analyses
                ),
                "input_cell_count": sum(
                    len(analysis.problem.demand.cells)
                    for analysis in result.analysis.direction_analyses
                ),
                "minimum_mass_kg": (
                    None
                    if minimum_mass_candidate is None
                    else minimum_mass_candidate.solution.metrics.total_mass_kg
                ),
                "minimum_mass_delta_pct": minimum_mass_delta_pct,
                "mass_gate_target_pct": (
                    None if metric_reference is None else MASS_GATE_MAX_DELTA_PCT
                ),
                "mass_gate_gap_pct": (
                    None
                    if minimum_mass_delta_pct is None
                    else max(
                        0.0,
                        minimum_mass_delta_pct - MASS_GATE_MAX_DELTA_PCT,
                    )
                ),
                "mass_gate_met": (
                    None
                    if minimum_mass_delta_pct is None
                    else minimum_mass_delta_pct <= MASS_GATE_MAX_DELTA_PCT
                ),
                "minimum_mass_bar_delta_pct": minimum_mass_bar_delta_pct,
                "minimum_mass_zone_count": (
                    None
                    if minimum_mass_candidate is None
                    else minimum_mass_candidate.solution.metrics.zone_count
                ),
                "minimum_complexity": (
                    None
                    if not candidates or front is None
                    else min(
                        item.constructability.value(front.complexity_axis)
                        for item in candidates
                    )
                ),
                "normalized_hypervolume": _normalized_hypervolume(
                    result,
                    hypervolume_bounds,
                ),
                "operator_learning": _operator_learning_summary(result),
                "runtime_ms": sum(
                    max(
                        solution.runtime_ms
                        for solution in (
                            analysis.candidate_solutions or analysis.solutions
                        )
                    )
                    for analysis in result.analysis.direction_analyses
                ),
            }
        )
    return rows


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=tuple(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _configuration_summary_rows(run_rows: list[dict]) -> list[dict]:
    """Сгруппировать одинаковые настройки по seed для честного тюнинга."""

    parameter_names = (
        "population_size",
        "generations",
        "crossover_rate",
        "mutation_rate",
        "candidate_window",
        "candidate_trajectories",
        "layer_bridge_span",
        "maximum_merge_reduction",
        "maximum_pool_merges",
        "complexity_axis",
        "operator_policy",
        "ucb_exploration",
        "baseline_seed_algorithms",
    )
    grouped: dict[tuple, list[dict]] = {}
    for row in run_rows:
        grouped.setdefault(
            tuple(row[name] for name in parameter_names),
            [],
        ).append(row)

    summaries = []
    for key, rows in sorted(grouped.items(), key=lambda item: item[0]):
        parameters = dict(zip(parameter_names, key))
        mass_deltas = [
            row["minimum_mass_delta_pct"]
            for row in rows
            if row["minimum_mass_delta_pct"] is not None
        ]
        gate_gaps = [
            row["mass_gate_gap_pct"]
            for row in rows
            if row["mass_gate_gap_pct"] is not None
        ]
        bar_deltas = [
            row["minimum_mass_bar_delta_pct"]
            for row in rows
            if row["minimum_mass_bar_delta_pct"] is not None
        ]
        hypervolumes = [row["normalized_hypervolume"] for row in rows]
        summaries.append(
            {
                "configuration_id": (
                    f"p{parameters['population_size']}-g{parameters['generations']}-"
                    f"w{parameters['candidate_window']}-"
                    f"t{parameters['candidate_trajectories']}-"
                    f"b{parameters['layer_bridge_span']}-"
                    f"r{parameters['maximum_merge_reduction']}-"
                    f"{parameters['complexity_axis']}-"
                    f"{parameters['operator_policy']}"
                ),
                **parameters,
                "seed_count": len(rows),
                "mass_gate_success_rate": (
                    None
                    if not mass_deltas
                    else sum(
                        value <= MASS_GATE_MAX_DELTA_PCT
                        for value in mass_deltas
                    )
                    / len(mass_deltas)
                ),
                "best_mass_delta_pct": min(mass_deltas) if mass_deltas else None,
                "median_mass_delta_pct": median(mass_deltas) if mass_deltas else None,
                "worst_mass_delta_pct": max(mass_deltas) if mass_deltas else None,
                "mass_delta_spread_pct": (
                    max(mass_deltas) - min(mass_deltas) if mass_deltas else None
                ),
                "median_mass_gate_gap_pct": median(gate_gaps) if gate_gaps else None,
                "median_bar_delta_pct": median(bar_deltas) if bar_deltas else None,
                "best_normalized_hypervolume": max(hypervolumes),
                "median_normalized_hypervolume": median(hypervolumes),
                "worst_normalized_hypervolume": min(hypervolumes),
                "median_runtime_ms": median(row["runtime_ms"] for row in rows),
            }
        )
    return summaries


def _signed(value: float, digits: int = 2) -> str:
    prefix = "+" if value > 0 else ""
    return f"{prefix}{value:.{digits}f}"


def _signed_or_dash(value: float | None, suffix: str = "") -> str:
    return "—" if value is None else f"{_signed(value)}{suffix}"


def _decimal_or_dash(value: float | None, suffix: str = "") -> str:
    return "—" if value is None else f"{value:.2f}{suffix}"


def _comparison_summary_html(comparison: dict) -> str:
    engineer = comparison["engineer"]
    ours = comparison["ours"]
    breakdown = ours["mass_breakdown"]
    heading = (
        "Golden инженера ↔ наш кандидат минимальной массы"
        if comparison["reference_kind"] == "strict_golden"
        else "Проверенная спецификация инженера ↔ наш кандидат минимальной массы"
    )
    return f"""
    <section class="panel comparison-summary">
      <div class="section-head"><div><span class="eyebrow">Главное сравнение</span>
        <h2>{heading}</h2></div>
        <span class="status {'good' if comparison['mass_gate_met'] else 'bad'}">
          {'гейт ≤15% выполнен' if comparison['mass_gate_met'] else 'гейт ≤15% не выполнен'}
        </span></div>
      <div class="hero-metrics">
        <div class="metric engineer"><span>Инженер · масса</span>
          <strong>{engineer['total_mass_kg']:.2f} кг</strong></div>
        <div class="metric ours"><span>Наш кандидат · масса</span>
          <strong>{ours['total_mass_kg']:.2f} кг</strong></div>
        <div class="metric delta bad"><span>Разница массы</span>
          <strong>{_signed(comparison['mass_delta_kg'])} кг</strong>
          <small>{_signed(comparison['mass_delta_pct'])}%</small></div>
        <div class="metric"><span>Разрыв до гейта ≤15%</span>
          <strong>{comparison['mass_gate_gap_pct']:.2f} п.п.</strong></div>
        <div class="metric"><span>Недоармированных КЭ</span>
          <strong>{ours['under_reinforced_cell_count']}</strong></div>
      </div>
      <p class="mass-breakdown"><strong>Из чего складываются наши {ours['total_mass_kg']:.2f} кг:</strong>
        рабочая длина {breakdown['required_mass_kg']:.2f} кг ·
        анкеровка 40d +{breakdown['anchorage_addition_kg']:.2f} кг ·
        раскрой +{breakdown['cutting_addition_kg']:.2f} кг.</p>
      <div class="table-wrap"><table class="comparison-table"><thead><tr>
        <th>Метрика</th><th>Инженер golden</th><th>Наш результат</th>
        <th>Абсолютное отклонение</th><th>Относительное</th></tr></thead><tbody>
        <tr><td>Масса дополнительной стали</td><td>{engineer['total_mass_kg']:.2f} кг</td>
          <td>{ours['total_mass_kg']:.2f} кг</td>
          <td class="bad-text">{_signed(comparison['mass_delta_kg'])} кг</td>
          <td class="bad-text">{_signed(comparison['mass_delta_pct'])}%</td></tr>
        <tr><td>Физические стержни</td><td>{engineer['physical_bar_count']} шт.</td>
          <td>{ours['physical_bar_count']} шт.</td>
          <td class="bad-text">{_signed(float(comparison['bar_delta']), 0)} шт.</td>
          <td class="bad-text">{_signed(comparison['bar_delta_pct'])}%</td></tr>
        <tr><td>Позиции спецификации инженера</td><td>{engineer['position_count']}</td>
          <td>не рассчитаны</td><td>—</td><td>—</td></tr>
        <tr><td>Параметрические прямоугольные зоны</td><td>не извлечены из PDF</td>
          <td>{ours['zone_count']}</td><td>—</td><td>—</td></tr>
      </tbody></table></div>
      <p class="note"><strong>Почему две последние строки без процента:</strong>
        {engineer['position_count']} позиций в PDF — не то же самое, что
        {ours['zone_count']} объектов <code>LayoutZone</code>. Сравнивать их численно
        было бы методологической ошибкой.
        Выбран запуск <code>{html.escape(comparison['run_id'])}</code>, кандидат
        <code>{html.escape(comparison['candidate_id'])}</code>.</p>
    </section>
    """


def _direction_comparison_html(
    results: tuple[GeneticRunResult, ...],
    reference: GoldenCaseDefinition,
    comparison: dict,
    *,
    reference_data_dir: Path | None,
    render_dpi: int,
) -> str:
    result, candidate = _selected_candidate(results)
    files = (
        None
        if reference_data_dir is None
        else resolve_golden_case_files(reference, reference_data_dir)
    )
    comparison_by_direction = {
        item["direction"]: item for item in comparison["directions"]
    }
    cards = []
    for expectation in reference.sheets:
        direction = expectation.direction
        row = comparison_by_direction[str(direction)]
        engineer = row["engineer"]
        ours = row["ours"]
        solution = candidate.solution.solution(direction)
        problem = result.analysis.problem.problem(direction)
        breakdown = ours["mass_breakdown"]
        ours_svg = render_solution_svg(problem, solution)
        if files is None:
            input_visual = '<div class="visual-placeholder">Входное PNG не встроено</div>'
            engineer_visual = (
                '<div class="visual-placeholder">Лист инженера не встроен</div>'
            )
        else:
            input_uri = _data_uri(
                files.input_png_by_direction[direction].read_bytes()
            )
            engineer_uri = _data_uri(
                render_pdf_page_png(
                    files.engineer_pdf,
                    expectation.pdf_page,
                    dpi=render_dpi,
                )
            )
            input_visual = (
                f'<img src="{input_uri}" alt="{html.escape(expectation.title)}: изополе">'
            )
            engineer_visual = (
                f'<img src="{engineer_uri}" alt="{html.escape(expectation.title)}: '
                'решение инженера">'
            )
        cards.append(
            f"""
            <article class="direction-card">
              <div class="section-head"><div><span class="eyebrow">{html.escape(str(direction))}</span>
                <h3>{html.escape(expectation.title)}</h3>
                <p>Инженер: {html.escape(expectation.engineer_axis_label)} · лист PDF
                  {expectation.pdf_page}</p></div>
                <span class="direction-delta">масса {_signed(row['mass_delta_pct'])}%</span>
              </div>
              <div class="direction-table table-wrap"><table><thead><tr>
                <th>Метрика</th><th>Инженер</th><th>Наша</th><th>Δ</th><th>Δ, %</th>
              </tr></thead><tbody>
                <tr><td>Масса</td><td>{engineer['total_mass_kg']:.2f} кг</td>
                  <td>{ours['total_mass_kg']:.2f} кг</td>
                  <td>{_signed(row['mass_delta_kg'])} кг</td>
                  <td>{_signed(row['mass_delta_pct'])}%</td></tr>
                <tr><td>Физические стержни</td><td>{engineer['physical_bar_count']}</td>
                  <td>{ours['physical_bar_count']}</td>
                  <td>{_signed(float(row['bar_delta']), 0)}</td>
                  <td>{_signed(row['bar_delta_pct'])}%</td></tr>
                <tr><td>Позиции / зоны</td><td>{engineer['position_count']} позиций</td>
                  <td>{ours['zone_count']} зон</td><td>несопоставимо</td><td>—</td></tr>
                <tr><td>Недоармированные КЭ</td><td>эталон</td>
                  <td>{ours['under_reinforced_cell_count']}</td><td>—</td><td>—</td></tr>
                <tr><td>Рабочая длина / 40d / раскрой</td><td>нет разбивки</td>
                  <td>{breakdown['required_mass_kg']:.1f} / +{breakdown['anchorage_addition_kg']:.1f} /
                    +{breakdown['cutting_addition_kg']:.1f} кг</td><td>—</td><td>—</td></tr>
              </tbody></table></div>
              <div class="visual-grid">
                <figure><figcaption>Исходное изополе</figcaption>{input_visual}</figure>
                <figure><figcaption>Golden · решение инженера</figcaption>{engineer_visual}</figure>
                <figure><figcaption>Наши параметрические зоны</figcaption>
                  <div class="svg-frame">{ours_svg}</div></figure>
              </div>
            </article>
            """
        )
    return "".join(cards)


def generate_genetic_benchmark_report(
    results: tuple[GeneticRunResult, ...],
    out_dir: Path,
    *,
    reference: BenchmarkReference | None = None,
    reference_data_dir: Path | None = None,
    render_dpi: int = 110,
) -> Path:
    """Сохранить сравнимый отчёт с параметрами, seed, гейтами и golden-дельтами."""

    if not results:
        raise ValueError("невозможно построить отчёт без запусков")
    if render_dpi <= 0:
        raise ValueError("render_dpi должен быть положительным")
    if reference_data_dir is not None and not isinstance(reference, GoldenCaseDefinition):
        raise ValueError("reference_data_dir поддерживается только для строгого golden")
    out_dir.mkdir(parents=True, exist_ok=True)
    run_rows = _run_rows(results, reference)
    configuration_summaries = _configuration_summary_rows(run_rows)
    candidate_rows = _candidate_rows(results, reference)
    comparison = _comparison_payload(results, reference)
    _write_csv(out_dir / "runs.csv", run_rows)
    _write_csv(out_dir / "configurations.csv", configuration_summaries)
    _write_csv(out_dir / "candidates.csv", candidate_rows)

    payload = {
        "schema_version": 5,
        "tuning_policy": {
            "hard_filter": "valid layout and zero under-reinforced cells",
            "primary_metric": "mass_gate_gap_pct",
            "mass_gate_target_pct": MASS_GATE_MAX_DELTA_PCT,
            "secondary_metrics": (
                "minimum_mass_bar_delta_pct",
                "minimum_mass_zone_count",
                "normalized_hypervolume",
                "operator_learning",
                "runtime_ms",
            ),
        },
        "reference": (
            None
            if reference is None
            else {
                "id": _metric_reference(reference).id,
                "title": _metric_reference(reference).title,
                "source_kind": _metric_reference(reference).source_kind,
                "mass_kg": _metric_reference(reference).expected_mass_kg,
                "physical_bar_count": _metric_reference(reference).expected_bar_count,
                "position_count": _metric_reference(reference).expected_position_count,
            }
        ),
        "runs": run_rows,
        "configuration_summaries": configuration_summaries,
        "candidates": candidate_rows,
        "comparison": comparison,
    }
    (out_dir / "benchmark.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    table_rows = "".join(
        "<tr>"
        f"<td>{html.escape(row['run_id'])}</td>"
        f"<td>{row['plate_front_size']}</td>"
        f"<td>{row['minimum_mass_kg']:.2f}</td>"
        f"<td>{_signed_or_dash(row['minimum_mass_delta_pct'], '%')}</td>"
        f"<td>{_decimal_or_dash(row['mass_gate_gap_pct'], ' п.п.')}</td>"
        f"<td>{_signed_or_dash(row['minimum_mass_bar_delta_pct'], '%')}</td>"
        f"<td>{row['minimum_complexity']}</td>"
        f"<td>{row['normalized_hypervolume']:.4f}</td>"
        f"<td>{row['runtime_ms']:.0f}</td>"
        "</tr>"
        for row in run_rows
        if row["minimum_mass_kg"] is not None
    )
    configuration_rows = "".join(
        "<tr>"
        f"<td>{html.escape(row['configuration_id'])}</td>"
        f"<td>{row['seed_count']}</td>"
        f"<td>{_signed_or_dash(row['best_mass_delta_pct'], '%')}</td>"
        f"<td>{_signed_or_dash(row['median_mass_delta_pct'], '%')}</td>"
        f"<td>{_signed_or_dash(row['worst_mass_delta_pct'], '%')}</td>"
        f"<td>{_decimal_or_dash(row['mass_delta_spread_pct'], ' п.п.')}</td>"
        f"<td>{_decimal_or_dash(None if row['mass_gate_success_rate'] is None else row['mass_gate_success_rate'] * 100.0, '%')}</td>"
        f"<td>{_signed_or_dash(row['median_bar_delta_pct'], '%')}</td>"
        f"<td>{row['median_normalized_hypervolume']:.4f}</td>"
        "</tr>"
        for row in configuration_summaries
    )
    comparison_html = (
        ""
        if comparison is None
        else _comparison_summary_html(comparison)
    )
    directions_html = (
        ""
        if not isinstance(reference, GoldenCaseDefinition) or comparison is None
        else _direction_comparison_html(
            results,
            reference,
            comparison,
            reference_data_dir=reference_data_dir,
            render_dpi=render_dpi,
        )
    )
    directions_section_html = (
        ""
        if not directions_html
        else f"""
        <section class="panel"><div class="section-head"><div>
        <span class="eyebrow">4 направления</span>
        <h2>Покомпонентное сравнение и схемы</h2></div></div>
        <p>Для каждого направления показаны один и тот же вход, ручная раскладка
        golden и наш параметрический результат.</p></section>{directions_html}
        """
    )
    page = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Golden ↔ наше решение</title>
<style>
:root{{--ink:#17212b;--muted:#647380;--line:#d9e1e6;--paper:#fff;--bg:#edf1f3;
--gold:#d89a21;--ours:#176d75;--bad:#a83b32;--good:#2d7651}}
*{{box-sizing:border-box}}body{{margin:0;font:14px Inter,ui-sans-serif,system-ui,sans-serif;
color:var(--ink);background:var(--bg)}}header{{padding:30px max(24px,calc((100vw - 1580px)/2));
background:#152b3a;color:white}}header h1{{margin:4px 0 8px;font-size:clamp(26px,3vw,40px)}}
header p{{max-width:1000px;margin:0;color:#d5e1e7}}main{{max-width:1580px;margin:24px auto;
padding:0 20px 60px}}.panel,.direction-card{{background:var(--paper);border-radius:14px;
box-shadow:0 4px 20px #17304015;padding:22px;margin-bottom:24px}}.section-head{{display:flex;
gap:16px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap}}h2,h3{{margin:3px 0 7px}}
h2{{font-size:24px}}h3{{font-size:21px}}p{{color:var(--muted)}}.eyebrow{{font-size:11px;
font-weight:800;letter-spacing:.12em;text-transform:uppercase;color:var(--ours)}}.status,
.direction-delta{{padding:7px 10px;border-radius:999px;font-weight:750;background:#f5e8e5;
color:var(--bad)}}.status.good{{background:#e5f2eb;color:var(--good)}}.hero-metrics{{display:grid;
grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:18px 0}}
.metric{{padding:15px;border:1px solid var(--line);border-radius:10px;background:#f8fafb}}
.metric span,.metric strong,.metric small{{display:block}}.metric strong{{font-size:25px;margin-top:4px}}
.metric small{{margin-top:3px}}.metric.engineer{{border-top:4px solid var(--gold)}}
.metric.ours{{border-top:4px solid var(--ours)}}.metric.bad{{border-top:4px solid var(--bad)}}
.table-wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;margin-top:12px}}
th,td{{border-bottom:1px solid var(--line);padding:9px 11px;text-align:right;white-space:nowrap}}
th:first-child,td:first-child{{text-align:left}}th{{color:var(--muted);font-size:12px}}
.bad-text{{color:var(--bad);font-weight:750}}.note{{padding:12px 14px;border-left:4px solid var(--gold);
background:#fff6dc;border-radius:7px;color:#55491f}}code{{background:#e9eef1;padding:2px 5px;
border-radius:4px}}.visual-grid{{display:grid;grid-template-columns:repeat(3,minmax(280px,1fr));
gap:14px;margin-top:18px;align-items:start}}figure{{margin:0;border:1px solid var(--line);
border-radius:10px;overflow:hidden;background:#f7f9fa}}figcaption{{padding:9px 11px;background:#e9eff2;
font-weight:750}}figure img{{display:block;width:100%;height:520px;object-fit:contain;background:white}}
.svg-frame{{height:520px;background:white}}.svg-frame svg{{width:100%;height:100%;display:block}}
.svg-frame .bar-axes{{display:none}}
.visual-placeholder{{height:520px;display:grid;place-items:center;color:var(--muted)}}
.benchmark-table{{background:white;border-radius:14px;padding:20px;box-shadow:0 4px 20px #17304015}}
.files{{font-size:12px}}@media(max-width:1050px){{.visual-grid{{grid-template-columns:1fr}}
figure img,.svg-frame,.visual-placeholder{{height:auto;min-height:360px}}}}
</style></head><body>
<header><span class="eyebrow">Qmonitoring × A101 · проверяемый эксперимент</span>
<h1>Сравнение решения инженера и нашей раскладки</h1>
<p>Golden берётся из спецификаций и листов конструктора. Наш вариант автоматически
выбран только как кандидат минимальной массы среди фактически построенного допустимого
фронта; это не утверждение о глобальном оптимуме и не готовая рабочая документация.</p></header>
<main>{comparison_html}{directions_section_html}
<section class="benchmark-table"><span class="eyebrow">Контекст эксперимента</span>
<h2>Запуски GA</h2><p class="files">Полные данные: <code>benchmark.json</code>,
<code>runs.csv</code> и <code>candidates.csv</code>.</p>
<table><thead><tr><th>Запуск</th><th>Точек фронта</th><th>Мин. масса, кг</th>
<th>Δ массы</th><th>До гейта ≤15%</th><th>Δ стержней</th>
<th>Мин. сложность</th><th>Hypervolume</th><th>Runtime, мс</th></tr></thead><tbody>{table_rows}</tbody></table>
</section>
<section class="benchmark-table"><span class="eyebrow">Тюнинг без cherry-picking</span>
<h2>Устойчивость конфигураций по seed</h2>
<p>Настройки сравниваются сначала по доле прохождения гейта, затем по медиане и худшему
отклонению массы. Отклонение стержней остаётся отдельной вторичной метрикой.</p>
<table><thead><tr><th>Конфигурация</th><th>Seed</th><th>Лучшее Δ массы</th>
<th>Медиана</th><th>Худшее</th><th>Разброс</th><th>Гейт пройден</th>
<th>Медиана Δ стержней</th><th>Медиана hypervolume</th></tr></thead><tbody>{configuration_rows}</tbody></table>
</section></main></body></html>"""
    report_path = out_dir / "index.html"
    report_path.write_text(page, encoding="utf-8")
    return report_path
