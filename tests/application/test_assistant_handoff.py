from rebar.reporting.assistant_handoff import render_assistant_handoff


def review():
    return {"expected": {"additional_mass_kg": 1000, "physical_bar_count": 40, "source_zone_count": 4,
        "execution_group_count": 4, "run_count": 4, "position_count": 1},
        "source_original_coverage": [{"demanded_cell_count": 100, "uncovered_cell_count": 0}],
        "manual_joint_tasks": [{"id": "unresolved"}]}


def test_source_only_handoff_does_not_claim_host_placement_or_all_gates():
    text = render_assistant_handoff({"case_id": "test", "status": "prepared_with_manual_tasks"}, review())
    assert "Рабочая плита пока не проверена" in text
    assert "проверено 100" in text
    assert "Нерешённые пересечения пар в одной плоскости: 1" in text
    assert "Рабочие наборы удалять не нужно" in text
    assert "может остаться" in text


def test_host_blocker_remains_prominent_even_when_mass_and_source_coverage_pass():
    host = {"accepted_moved_bar_count": 18, "check_criterion": "outside_full_height_common_footprint",
        "blocked_before": 424, "blocked_after": 406, "after": {"bar_check": {"directions": {}}}}
    summary = {"case_id": "test", "status": "blocked_working_host", "engineer_comparison": {
        "mass_kg": 1000, "physical_bar_count": 50, "mass_delta_pct": 0, "bar_delta_pct": -20}}
    text = render_assistant_handoff(summary, review(), host)
    assert "424 → 406" in text
    assert "Всю эту партию пока создавать нельзя" in text
    assert "не означает прохождение всех гейтов" in text
    assert "рабочей плиты пока не проверена" not in text.lower()
    assert "working-host-fit.json" in text


def test_missing_complete_plan_is_not_rendered_as_zero_mass_success():
    text = render_assistant_handoff({"case_id": "a\n<b>`", "status": "blocked_patterned_stock"}, {})
    assert "Полная физическая партия не подготовлена" in text
    assert "&lt;b&gt;" in text and "<b>" not in text
    assert "0.00" not in text
