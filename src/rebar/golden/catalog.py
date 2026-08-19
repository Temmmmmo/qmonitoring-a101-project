"""Каталог проверенных golden-case без привязки к локальному пути датасета."""

from __future__ import annotations

from rebar.models import Axis, Direction, Layer

from .models import GoldenCaseDefinition, GoldenSheetExpectation


PLATE_ZERO_K09 = GoldenCaseDefinition(
    id="plate-zero-k09",
    title="Корпус 2.9 · плита нуля",
    dataset_dir_name="Для верификации изополей 2",
    input_task_dir_name="2025.02.19_плита нуля",
    engineer_pdf_name="ППТ8-1-Д2-Р-9-КЖ02.П1.pdf",
    sheets=(
        GoldenSheetExpectation(
            direction=Direction(Layer.BOTTOM, Axis.X),
            title="Нижняя арматура · ось X",
            engineer_axis_label="вдоль Б.О.",
            pdf_page=9,
            input_png_name=(
                "Площадь_полной_арматуры_на_1пм_по_оси_X_у_нижней_грани_"
                "(балки-стенки-посередине).png"
            ),
            expected_position_count=17,
            expected_bar_count=213,
            expected_mass_kg=700.60,
        ),
        GoldenSheetExpectation(
            direction=Direction(Layer.BOTTOM, Axis.Y),
            title="Нижняя арматура · ось Y",
            engineer_axis_label="вдоль Ц.О.",
            pdf_page=8,
            input_png_name=(
                "Площадь_полной_арматуры_на_1пм_по_оси_Y_у_нижней_грани_"
                "(балки-стенки-посередине).png"
            ),
            expected_position_count=16,
            expected_bar_count=192,
            expected_mass_kg=651.58,
        ),
        GoldenSheetExpectation(
            direction=Direction(Layer.TOP, Axis.X),
            title="Верхняя арматура · ось X",
            engineer_axis_label="вдоль Б.О.",
            pdf_page=11,
            input_png_name="Площадь_полной_арматуры_на_1пм_по_оси_X_у_верхней_грани.png",
            expected_position_count=24,
            expected_bar_count=322,
            expected_mass_kg=999.02,
        ),
        GoldenSheetExpectation(
            direction=Direction(Layer.TOP, Axis.Y),
            title="Верхняя арматура · ось Y",
            engineer_axis_label="вдоль Ц.О.",
            pdf_page=10,
            input_png_name="Площадь_полной_арматуры_на_1пм_по_оси_Y_у_верхней_грани.png",
            expected_position_count=22,
            expected_bar_count=292,
            expected_mass_kg=826.44,
        ),
    ),
    notes=(
        "Ось X сопоставлена листам «вдоль Б.О.», ось Y — «вдоль Ц.О.» по фактической "
        "ориентации стержней; обозначение требуется подтвердить у конструктора.",
        "Количество стержней — сумма колонки «Кол-во, шт.»; это не число LayoutZone.",
        "PDF используется как числовой и визуальный эталон, DWG пока не разобран.",
    ),
)

GOLDEN_CASES = {PLATE_ZERO_K09.id: PLATE_ZERO_K09}


def get_golden_case(case_id: str) -> GoldenCaseDefinition:
    """Вернуть эталон по стабильному идентификатору."""

    try:
        return GOLDEN_CASES[case_id]
    except KeyError as error:
        available = ", ".join(sorted(GOLDEN_CASES))
        raise KeyError(f"Неизвестный golden-case {case_id!r}; доступны: {available}") from error

