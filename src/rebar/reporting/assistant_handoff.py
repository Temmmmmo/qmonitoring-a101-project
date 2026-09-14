"""Human-readable handoff of calculation facts, never an engineering permission."""
from __future__ import annotations


def _label(value):
    return " ".join(str(value).split()).replace("`", "'").replace("|", "/").replace("<", "&lt;").replace(">", "&gt;")


def render_assistant_handoff(summary: dict, review: dict, host_review: dict | None = None) -> str:
    """Separate complete DXF metrics from verified host placement and open work."""
    metrics = review.get("expected", {})
    lines = ["# QMonitoring — результат расчёта и что делать дальше", "",
        f"Комплект: `{_label(summary['case_id'])}`. Статус: `{_label(summary['status'])}`.", "",
        "Это расчёт помощника конструктора, не разрешение на изготовление или постоянное размещение.", ""]
    if not metrics:
        lines.extend(["Полная физическая партия не подготовлена. Причины — в `engineer-review.json` и "
                      "`physical-normalization.json`; частичный результат не выдаётся за полный.", ""])
        return "\n".join(lines)
    lines.extend(["## Полная расчётная партия", "",
        "| Показатель | Значение |", "| --- | ---: |",
        f"| Дополнительная арматура, кг | {metrics['additional_mass_kg']:.2f} |",
        f"| Физические стержни, шт. | {metrics['physical_bar_count']} |",
        f"| Исходные параметрические зоны | {metrics['source_zone_count']} |",
        f"| Группы одинаковой геометрии | {metrics['execution_group_count']} |",
        f"| Rebar-наборы | {metrics['run_count']} |",
        f"| Типоразмеры прямых стержней | {metrics['position_count']} |", "",
        "Зоны, стержни, наборы и позиции PDF-спецификации — разные единицы счёта.", ""])
    engineer = summary.get("engineer_comparison")
    if engineer:
        lines.extend([f"Сопоставленный инженерный комплект: {engineer['mass_kg']:.2f} кг / "
            f"{engineer['physical_bar_count']} стержней. Изменение полной расчётной партии: "
            f"масса **{engineer['mass_delta_pct']:+.2f}%**, стержни **{engineer['bar_delta_pct']:+.2f}%**.", "",
            "Это сравнение расчётных партий по DXF. Оно не доказывает размещаемость в рабочей RVT "
            "и не означает прохождение всех гейтов.", ""])
    coverage = review.get("source_original_coverage", [])
    count = sum(row["demanded_cell_count"] for row in coverage)
    missing = sum(row["uncovered_cell_count"] for row in coverage)
    lines.extend(["## Что проверено", "",
        f"- Покрытие исходной потребности: проверено {count} требующих КЭ по направлениям; "
        f"непокрытых в исходной модели покрытия — {missing}. Это не проверка физического контура RVT.",
        f"- Исходные оси и полный выпуск 40d: `{_label(review.get('source_axis_and_new40d_status', 'not_checked'))}`.",
        f"- Раскрой всей партии: `{_label(review.get('stock_cutting', {}).get('status', 'not_checked'))}`. "
        "Используется явно заданная модель прутка 11700 мм и нулевой потери на рез.",
        f"- Нерешённые пересечения пар в одной плоскости: {len(review.get('manual_joint_tasks', []))}. "
        "Отсутствие таких пар не заменяет проверку фона и взаимного положения X/Y по высоте.", ""])
    if host_review is None:
        lines.extend(["## Рабочая плита пока не проверена", "",
            "В этом запуске нет снимка реального host. Массу и покрытие нельзя считать "
            "подтверждёнными для размещения. Следующий шаг — read-only снимок выбранной плиты и CAD.", ""])
    else:
        lines.extend(["## Проверка реальной плиты", "",
            f"Принято продольных сдвигов: {host_review['accepted_moved_bar_count']}. "
            "Длины, диаметры, масса и количество стержней не изменены; неподходящие стержни сохранены.", "",
            f"По выбранной проверке `{host_review['check_criterion']}` нарушений: "
            f"**{host_review['blocked_before']} → {host_review['blocked_after']}**.", "",
            "| Направление | Всего стержней | Нарушают необходимое XY-условие | Нарушают общий контур всей толщины |",
            "| --- | ---: | ---: | ---: |"])
        for name, row in host_review["after"]["bar_check"]["directions"].items():
            lines.append(f"| {_label(name)} | {row['physical_bar_count']} | "
                f"{row['outside_even_in_xy_projection']} | {row['outside_full_height_common_footprint']} |")
        lines.extend(["", "XY-проверка не назначает глубины осей и не подтверждает коллизии с существующей "
            "арматурой. Даже проверка тела в Solid не является расчётом прочности.", ""])
        if host_review["blocked_after"]:
            lines.extend(["**Всю эту партию пока создавать нельзя: проверка Revit должна её остановить.** "
                "Повторное чтение той же плиты это не исправит. Нужна перераскладка проблемных участков "
                "или явно согласованный режим инженерных исключений; автоматической подрезки нет.", ""])
        else:
            lines.extend(["Контур пройден только при записанном профиле. Далее нужно проверить глубины, фон, "
                "нерешённые стыки и фактическое создание/считывание в Revit. Это ещё не допуск к размещению.", ""])
    lines.extend(["## Передача в Revit", "",
        "Рабочие наборы удалять не нужно. Новые версии Full Plate Trial 0.6.1 / Physical Plan Trial 0.1.1 "
        "сохраняют их; условия запуска и отдельное согласие на заимствование — в `WORKSHARING_README.md` внутри ZIP.", "",
        "Пробное создание откатывает локальные изменения. Заимствование в центральной модели может остаться; "
        "автоматических сохранения, синхронизации или возврата владения нет. "
        "Новый адаптер требует проверки в настоящем Revit 2024 — локальные тесты этого не заменяют.", "",
        "Подробности и все исключения: `engineer-review.json`, `physical-normalization.json`" +
        (", `working-host-fit.json`." if host_review else "."), ""])
    return "\n".join(lines)
