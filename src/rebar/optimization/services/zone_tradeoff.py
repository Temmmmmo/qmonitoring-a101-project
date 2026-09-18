"""Аддитивный фронт зон и объяснимое геометрическое колено конечной кривой."""
from __future__ import annotations

import math


def recommend_zone_knee(points):
    """Максимальное улучшение над хордой крайних точек после нормирования осей.

    Не обученная предпочтениям инженера «Точка 3». Для прямой/двух точек колено
    отсутствует; выбранный минимум массы является явно обозначенным fallback.
    """
    if not points:
        return {"index": None, "status": "empty", "method": "normalized_chord_distance"}
    if any(not isinstance(n, int) or isinstance(n, bool) or n < 0 or not math.isfinite(m) or m < 0
           for n, m in points):
        raise ValueError("нужны число зон и конечная неотрицательная масса")
    ordered = sorted(enumerate(points), key=lambda pair: pair[1])
    fallback = min(range(len(points)), key=lambda i: (points[i][1], points[i][0]))
    result = {"index": fallback, "status": "insufficient_tradeoff", "method": "normalized_chord_distance",
              "fallback": "minimum_mass", "engineering_optimality_proven": False}
    if len(points) < 3:
        return result
    n0, m0 = ordered[0][1]
    n1, m1 = ordered[-1][1]
    if n1 <= n0 or m0 <= m1 + 1e-6:
        return result
    scores = [(1 - (n - n0) / (n1 - n0) - (m - m1) / (m0 - m1), i)
              for i, (n, m) in ordered[1:-1]]
    score, index = max(scores, key=lambda item: (item[0], -points[item[1]][0]))
    if score <= 1e-6:
        result["status"] = "no_distinct_knee"
        return result
    result.update(index=index, status="candidate", fallback=None, normalized_gain=score)
    location = next(j for j, (i, _) in enumerate(ordered) if i == index)
    n, m = points[index]
    for name, neighbour in (("coarser", ordered[location - 1][1]), ("finer", ordered[location + 1][1])):
        count_delta = abs(n - neighbour[0])
        mass_delta = abs(m - neighbour[1])
        result[name] = {"zone_count": neighbour[0], "mass_kg": neighbour[1],
                        "extra_zones": count_delta, "mass_difference_kg": mass_delta,
                        "kg_per_extra_zone": mass_delta / count_delta if count_delta else None}
    return result


def combine_zone_candidates(groups, *, count_of, mass_of):
    """Точный аддитивный DP среди переданных вариантов; номенклатура не суммируется."""
    states = {0: (0.0, ())}
    for group in groups:
        next_states = {}
        for count, (mass, choices) in states.items():
            for candidate in group:
                n, m = count_of(candidate), mass_of(candidate)
                if not isinstance(n, int) or n < 0 or not math.isfinite(m) or m < 0:
                    raise ValueError("невалидные метрики кандидата")
                total = count + n
                proposal = mass + m
                if total not in next_states or proposal < next_states[total][0]:
                    next_states[total] = (proposal, (*choices, candidate))
        states, best = {}, math.inf
        for count, item in sorted(next_states.items()):
            if item[0] < best - 1e-6:
                states[count], best = item, item[0]
    return tuple(item[1] for item in states.values())
