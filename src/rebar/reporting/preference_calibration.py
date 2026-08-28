"""JSON/HTML-отчёт первого leave-one-project-out эксперимента."""

from __future__ import annotations

import html
import json
from dataclasses import asdict
from pathlib import Path

from rebar.learning import (
    FrontObservation,
    PreferenceCalibration,
    rank_candidates,
    reference_distance,
    weak_target_candidate,
)


def _mean(values) -> float:
    materialized = tuple(values)
    return sum(materialized) / len(materialized)


def _scatter_svg(
    observation: FrontObservation,
    calibration: PreferenceCalibration,
) -> str:
    width, height, padding = 520, 310, 42
    masses = tuple(candidate.mass_kg for candidate in observation.candidates)
    bars = tuple(candidate.physical_bar_count for candidate in observation.candidates)
    mass_min, mass_max = min(masses), max(masses)
    bars_min, bars_max = min(bars), max(bars)
    mass_span = mass_max - mass_min or 1.0
    bars_span = bars_max - bars_min or 1
    recommended = rank_candidates(
        observation,
        calibration.model.mass_weight,
    )[0]
    target = weak_target_candidate(
        observation,
        target_mass_weight=calibration.target_mass_weight,
    )

    points = []
    for candidate in observation.candidates:
        x = padding + (candidate.physical_bar_count - bars_min) / bars_span * (
            width - 2 * padding
        )
        y = height - padding - (candidate.mass_kg - mass_min) / mass_span * (
            height - 2 * padding
        )
        if candidate.id == recommended.id == target.id:
            css_class, radius = "both", 7
        elif candidate.id == target.id:
            css_class, radius = "target", 6
        elif candidate.id == recommended.id:
            css_class, radius = "recommended", 6
        else:
            css_class, radius = "candidate", 3
        points.append(
            f'<circle class="{css_class}" cx="{x:.1f}" cy="{y:.1f}" r="{radius}" />'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{html.escape(observation.title)}">'
        f'<line x1="{padding}" y1="{height-padding}" x2="{width-padding}" '
        f'y2="{height-padding}" class="axis" />'
        f'<line x1="{padding}" y1="{padding}" x2="{padding}" '
        f'y2="{height-padding}" class="axis" />'
        f'<text x="{width/2:.0f}" y="{height-8}" class="axis-label">'
        "физические стержни →</text>"
        f'<text x="14" y="{height/2:.0f}" class="axis-label vertical">масса →</text>'
        + "".join(points)
        + "</svg>"
    )


def generate_preference_calibration_report(
    calibration: PreferenceCalibration,
    observations: tuple[FrontObservation, ...],
    out_dir: Path,
) -> Path:
    """Сохранить воспроизводимый отчёт обучения слабого селектора."""

    if {fold.held_out_case_id for fold in calibration.folds} != {
        observation.case_id for observation in observations
    }:
        raise ValueError("LOPO folds и переданные наблюдения описывают разные проекты")
    out_dir.mkdir(parents=True, exist_ok=True)
    fold_by_case = {fold.held_out_case_id: fold for fold in calibration.folds}
    recommendations = []
    for observation in observations:
        ranking = rank_candidates(observation, calibration.model.mass_weight)
        target = weak_target_candidate(
            observation,
            target_mass_weight=calibration.target_mass_weight,
        )
        recommendations.append(
            {
                "case_id": observation.case_id,
                "candidate_count": len(observation.candidates),
                "recommended_candidate_id": ranking[0].id,
                "top_3_candidate_ids": [candidate.id for candidate in ranking[:3]],
                "weak_target_candidate_id": target.id,
                "recommended_reference_distance": reference_distance(
                    observation,
                    ranking[0],
                    target_mass_weight=calibration.target_mass_weight,
                ),
            }
        )
    payload = {
        "schema_version": 1,
        "method": {
            "name": "single-weight-pareto-ranker",
            "score": (
                "mass_weight * minmax(mass_kg) + "
                "(1 - mass_weight) * minmax(physical_bar_count)"
            ),
            "weak_target": (
                "0.5 * relative_mass_distance + 0.5 * relative_bar_distance"
            ),
            "validation": "leave-one-project-out",
        },
        "model": asdict(calibration.model),
        "summary": {
            "project_count": len(calibration.folds),
            "mean_regret": calibration.mean_regret,
            "top_1_accuracy": calibration.top_1_accuracy,
            "top_3_accuracy": calibration.top_3_accuracy,
            "mass_gate_success_rate": calibration.mass_gate_success_rate,
            "mean_absolute_mass_delta_pct": _mean(
                abs(fold.mass_delta_pct) for fold in calibration.folds
            ),
            "mean_absolute_bar_delta_pct": _mean(
                abs(fold.bar_delta_pct) for fold in calibration.folds
            ),
            "mean_balanced_regret": _mean(
                fold.balanced_regret for fold in calibration.folds
            ),
            "mean_minimum_mass_regret": _mean(
                fold.minimum_mass_regret for fold in calibration.folds
            ),
            "mean_minimum_bars_regret": _mean(
                fold.minimum_bars_regret for fold in calibration.folds
            ),
        },
        "folds": [asdict(fold) for fold in calibration.folds],
        "global_model_recommendations": recommendations,
    }
    (out_dir / "calibration.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    fold_rows = "".join(
        "<tr>"
        f"<td>{html.escape(fold.held_out_case_id)}</td>"
        f"<td>{fold.learned_mass_weight:.2f}</td>"
        f"<td>{fold.selected_mass_kg:.2f} кг ({fold.mass_delta_pct:+.2f}%) "
        f"{'✓' if fold.mass_gate_met else '✗'}</td>"
        f"<td>{fold.selected_bar_count} ({fold.bar_delta_pct:+.2f}%)</td>"
        f"<td>{fold.selected_zone_count}</td>"
        f"<td>{fold.regret:.4f}</td>"
        f"<td>{'да' if fold.top_3_hit else 'нет'}</td>"
        "</tr>"
        for fold in calibration.folds
    )
    charts = "".join(
        f"""
        <article class="chart"><div><h3>{html.escape(observation.title)}</h3>
        <p>{len(observation.candidates)} допустимых точек · отложенный α
        {fold_by_case[observation.case_id].learned_mass_weight:.2f}</p></div>
        {_scatter_svg(observation, calibration)}</article>
        """
        for observation in observations
    )
    report_path = out_dir / "index.html"
    report_path.write_text(
        f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Калибровка выбора Парето-точки</title><style>
:root{{--ink:#162531;--muted:#667680;--line:#d8e0e4;--teal:#087b83;--gold:#d89a21}}
*{{box-sizing:border-box}}body{{margin:0;background:#edf1f3;color:var(--ink);
font:14px Inter,system-ui,sans-serif}}header{{padding:30px max(24px,calc((100vw - 1200px)/2));
background:#173242;color:white}}header h1{{margin:5px 0}}header p{{max-width:900px;color:#d7e3e7}}
main{{max-width:1200px;margin:24px auto;padding:0 20px 50px}}section,.chart{{background:white;
border-radius:13px;padding:20px;margin-bottom:20px;box-shadow:0 5px 18px #17304012}}
.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}
.metric{{border:1px solid var(--line);border-radius:9px;padding:14px}}.metric strong{{display:block;
font-size:25px;margin-top:4px}}table{{width:100%;border-collapse:collapse}}th,td{{padding:9px;
border-bottom:1px solid var(--line);text-align:right}}th:first-child,td:first-child{{text-align:left}}
.table{{overflow:auto}}.charts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));
gap:18px}}.chart{{margin:0}}.chart h3{{margin:0}}.chart p{{color:var(--muted)}}svg{{width:100%;
height:auto}}.axis{{stroke:#99a8b0;stroke-width:1}}.axis-label{{font-size:11px;fill:#667680}}
.vertical{{transform:rotate(-90deg);transform-origin:14px center}}.candidate{{fill:#9caeb6}}
.target{{fill:var(--gold);stroke:white;stroke-width:2}}.recommended{{fill:var(--teal);stroke:white;
stroke-width:2}}.both{{fill:var(--teal);stroke:var(--gold);stroke-width:3}}.legend span{{margin-right:18px}}
.legend i{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px}}
</style></head><body><header><span>Qmonitoring × A101 · learning from demonstrations</span>
<h1>Первый селектор точки Парето</h1><p>Модель учит одно число по инженерским plate-level
спецификациям. Генератор и hard-валидатор не обучаются; конструктор сохраняет выбор.</p></header>
<main><section><h2>Результат без утечки проекта</h2><div class="metrics">
<div class="metric">Глобальный вес массы<strong>{calibration.model.mass_weight:.2f}</strong></div>
<div class="metric">LOPO mean regret<strong>{calibration.mean_regret:.4f}</strong></div>
<div class="metric">Top-1 к слабой метке<strong>{calibration.top_1_accuracy*100:.0f}%</strong></div>
<div class="metric">Top-3 к слабой метке<strong>{calibration.top_3_accuracy*100:.0f}%</strong></div>
<div class="metric">Гейт массы ≤15%<strong>{calibration.mass_gate_success_rate*100:.0f}%</strong></div>
</div><p>Это исследовательский результат на {len(observations)} плитах, не доказанная модель
предпочтений заказчика. Слабая метка использует только массу и физические стержни.</p></section>
<section><h2>Leave-one-project-out</h2><div class="table"><table><thead><tr><th>Отложенная плита</th>
<th>α массы</th><th>Масса рекомендации</th><th>Стержни</th><th>Зоны</th><th>Regret</th>
<th>Цель в top-3</th></tr></thead><tbody>{fold_rows}</tbody></table></div></section>
<p class="legend"><span><i style="background:#d89a21"></i>слабая цель</span>
<span><i style="background:#087b83"></i>рекомендация глобальной модели</span></p>
<div class="charts">{charts}</div></main></body></html>""",
        encoding="utf-8",
    )
    return report_path
