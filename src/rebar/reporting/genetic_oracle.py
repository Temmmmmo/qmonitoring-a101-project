"""Переносимый отчёт exact-vs-GA: вход, конечный пул, свидетели фронта и gap."""

from __future__ import annotations

import csv
import hashlib
import html
import json
from pathlib import Path

from rebar.application.genetic_oracle import GeneticOracleRun
from rebar.optimization.algorithms.genetic.exact_oracle import EXACT_SCOPE, MASS_TOLERANCE_KG

from .serialization import to_jsonable


def _candidate_set_id(run: GeneticOracleRun) -> str:
    snapshot = to_jsonable((run.problem, run.candidate_zones, run.baseline_seed_genomes))
    return hashlib.sha256(json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _summary(run: GeneticOracleRun) -> dict[str, object]:
    reached = [point.gap_pct for point in run.budget_gaps if point.gap_pct is not None]
    return {
        "case_id": run.case_id,
        "operator_policy": run.config.operator_policy,
        "complexity_axis": run.config.complexity_axis.value,
        "random_seed": run.config.random_seed,
        "population_size": run.config.population_size,
        "generations": run.config.generations,
        "candidate_set_id": _candidate_set_id(run),
        "candidate_count": run.oracle.candidate_count,
        "maximum_zones": run.oracle.maximum_zones,
        "complete": run.oracle.complete,
        "subset_count": run.oracle.subset_count,
        "evaluated_count": run.oracle.evaluated_count,
        "coverage_pruned_count": run.oracle.coverage_pruned_count,
        "objective_pruned_count": run.oracle.objective_pruned_count,
        "rejected_count": run.oracle.rejected_count,
        "exact_front_size": len(run.oracle.solutions),
        "genetic_front_size": len(run.genetic_solutions),
        "rejected_genetic_count": run.rejected_genetic_count,
        "recovered_exact_points": run.recovered_exact_points,
        "exact_front_recall": (
            run.recovered_exact_points / len(run.oracle.solutions)
            if run.oracle.solutions else None
        ),
        "minimum_mass_gap_pct": run.minimum_mass_gap_pct,
        "unreached_budget_count": len(run.budget_gaps) - len(reached),
        "maximum_reached_budget_gap_pct": max(reached) if reached else None,
    }


def generate_genetic_oracle_report(runs: tuple[GeneticOracleRun, ...], out_dir: Path) -> Path:
    """Записать JSON с полными входами/свидетелями, сводный CSV и автономный HTML."""

    if not runs:
        raise ValueError("для отчёта нужен хотя бы один запуск")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [_summary(run) for run in runs]
    payload = {
        "schema_version": 1,
        "exact_scope": EXACT_SCOPE,
        "mass_tolerance_kg": MASS_TOLERANCE_KG,
        "budget_gap_rule": "minimum_ga_mass_at_complexity_le_exact_point_budget",
        "missing_budget_rule": "null_is_unreached_not_zero_gap",
        "summaries": rows,
        "runs": to_jsonable(runs),
    }
    (out_dir / "oracle.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8",
    )
    with (out_dir / "runs.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    def display(value: object) -> str:
        if value is None:
            return "не достигнут / нет оценки"
        if isinstance(value, float):
            return f"{value:.6f}"
        return html.escape(str(value))

    columns = {
        "case_id": "Маска / ось стержней",
        "operator_policy": "Политика",
        "complexity_axis": "Ось сложности",
        "random_seed": "Seed",
        "candidate_count": "Прямоугольников в пуле",
        "exact_front_size": "Точек exact",
        "genetic_front_size": "Точек GA",
        "recovered_exact_points": "Восстановлено точек exact",
        "minimum_mass_gap_pct": "Gap минимума массы, %",
        "unreached_budget_count": "Недостигнутых бюджетов сложности",
        "maximum_reached_budget_gap_pct": "Макс. gap достигнутых бюджетов, %",
        "rejected_genetic_count": "Отклонено GA",
    }
    heading = "".join(f"<th>{label}</th>" for label in columns.values())
    body = "".join(
        "<tr>" + "".join(f"<td>{display(row[key])}</td>" for key in columns) + "</tr>"
        for row in rows
    )
    details = []
    for run in runs:
        label = html.escape(f"{run.case_id} · {run.config.id}")
        gaps = "".join(
            f"<tr><td>{point.complexity}</td><td>{display(point.exact_mass_kg)}</td>"
            f"<td>{display(point.genetic_mass_kg)}</td><td>{display(point.gap_pct)}</td></tr>"
            for point in run.budget_gaps
        )
        details.append(
            f"<details><summary>{label}</summary><table><thead><tr>"
            "<th>Бюджет сложности</th><th>Exact, кг</th><th>GA, кг</th><th>Gap, %</th>"
            f"</tr></thead><tbody>{gaps}</tbody></table></details>"
        )
    document = """<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Exact-oracle · проверка GA</title><style>
body{font:15px system-ui,sans-serif;margin:24px;color:#182c40;background:#f6f8fa}
table{border-collapse:collapse;background:white;font-size:13px}th,td{padding:9px;
border:1px solid #ccd5dd;text-align:left}th{background:#e7edf3}details{margin:12px 0}
.scroll{overflow-x:auto}p{max-width:1000px;line-height:1.5}
</style><h1>Точный эталон внутри CandidateSet</h1>
<p>Перебор всех допустимых сочетаний данного конечного набора прямоугольников.
Детализация и hard-валидатор общие с GA. Это не глобальный оптимум всех геометрий,
не полный поиск фаз стержней и не разрешение на выпуск в Revit. Предупреждения
research-профиля не превращаются в пройденные инженерные проверки.</p>
<p>Для каждого бюджета зон или физических стержней сравнивается минимальная масса
GA при сложности не выше бюджета точного фронта. Недостигнутый бюджет отмечается
отдельно: это не нулевой gap. Масса сравнивается с допуском 10⁻⁶ кг.</p>
<p><a href="oracle.json">Полный JSON: входы, пулы и свидетели</a> ·
<a href="runs.csv">Сводный CSV</a></p>"""
    document += f"<div class=scroll><table><thead><tr>{heading}</tr></thead>"
    document += f"<tbody>{body}</tbody></table></div><h2>Gap по бюджетам сложности</h2>"
    document += "".join(details) + "</html>"
    report = out_dir / "index.html"
    report.write_text(document, encoding="utf-8")
    return report
