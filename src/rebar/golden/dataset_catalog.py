"""Переносимый manifest всех найденных инженерских выдач и входных комплектов."""

from __future__ import annotations

from rebar.models import Axis, Direction, Layer

from .dataset_models import (
    EngineerInputSetDefinition,
    EngineerMetricStatus,
    EngineerReferenceCase,
    EngineerReferenceSheet,
)

BOTTOM_X = Direction(Layer.BOTTOM, Axis.X)
BOTTOM_Y = Direction(Layer.BOTTOM, Axis.Y)
TOP_X = Direction(Layer.TOP, Axis.X)
TOP_Y = Direction(Layer.TOP, Axis.Y)

_LEGACY_DATASET = "Для верификации изополей"
_K09_DATASET = "Для верификации изополей 2"
_K09_ROOT = "ГОТОВЫЕ КОМПЛЕКТЫ_ВТ2_К09"


def _input_set(
    input_id: str,
    title: str,
    directory: tuple[str, ...],
    *,
    bottom_x: str,
    bottom_y: str,
    top_x: str,
    top_y: str,
) -> EngineerInputSetDefinition:
    return EngineerInputSetDefinition(
        id=input_id,
        title=title,
        relative_directory=directory,
        dxf_by_direction=(
            (BOTTOM_X, bottom_x),
            (BOTTOM_Y, bottom_y),
            (TOP_X, top_x),
            (TOP_Y, top_y),
        ),
    )


def _sheet(
    page: int,
    directions: Direction | tuple[Direction, ...],
    positions: int,
    bars: int,
    mass_kg: float,
) -> EngineerReferenceSheet:
    if isinstance(directions, Direction):
        directions = (directions,)
    return EngineerReferenceSheet(page, directions, positions, bars, mass_kg)


_UNREADABLE_NOTE = (
    "Встроенный шрифт PDF возвращает искажённый Unicode; числовые итоги нельзя "
    "использовать как метку без OCR или ручной контрольной транскрипции."
)
_AXIS_NOTE = (
    "Направления входных DXF определены по именам файлов; связь буквенных/цифровых "
    "осей с X/Y нужно подтвердить у конструктора."
)


ENGINEER_REFERENCE_CASES = (
    EngineerReferenceCase(
        id="legacy-kj00-s1-2",
        title="КЖ00 · секции 1-2",
        dataset_dir_name=_LEGACY_DATASET,
        engineer_pdf_parts=("1-КЖ00.С1-2", "л10-13.pdf"),
        input_sets=(
            _input_set(
                "legacy-s1-t800",
                "Секция 1 · t=800",
                ("1-КЖ00.С1-2",),
                bottom_x="С1_t_800_Нижняя по оси Х.dxf",
                bottom_y="С1_t_800_Нижняя по оси У.dxf",
                top_x="С1_t_800_Верхняя по оси Х.dxf",
                top_y="С1_t_800_Верхняя по оси У.dxf",
            ),
            _input_set(
                "legacy-s2-t700",
                "Секция 2 · t=700",
                ("1-КЖ00.С1-2",),
                bottom_x="С2_t_700_Нижняя по оси Х.dxf",
                bottom_y="С2_t_700_Нижняя по оси У.dxf",
                top_x="С2_t_700_Верхняя по оси Х.dxf",
                top_y="С2_t_700_Верхняя по оси У.dxf",
            ),
        ),
        metric_status=EngineerMetricStatus.PDF_TEXT_UNREADABLE,
        notes=(_UNREADABLE_NOTE, _AXIS_NOTE),
    ),
    EngineerReferenceCase(
        id="legacy-kj00-s3-4",
        title="КЖ00 · секции 3-4",
        dataset_dir_name=_LEGACY_DATASET,
        engineer_pdf_parts=("1-КЖ00.С3-4", "л12-13, 15-16.pdf"),
        input_sets=(
            _input_set(
                "legacy-s3",
                "Секция 3",
                ("1-КЖ00.С3-4",),
                bottom_x="01_C3_Низ вдоль буквенных осей.dxf",
                bottom_y="02_С3_НИЗ вдоль цифровых осей.dxf",
                top_x="03_С3_ВЕРХ вдоль буквенных осей.dxf",
                top_y="04_С3_ВЕРХ вдоль цифровых осей.dxf",
            ),
            _input_set(
                "legacy-s4",
                "Секция 4",
                ("1-КЖ00.С3-4",),
                bottom_x="05_C4_Низ вдоль буквенных осей.dxf",
                bottom_y="06_С4_НИЗ вдоль цифровых осей.dxf",
                top_x="07_С4_ВЕРХ вдоль буквенных осей.dxf",
                top_y="08_С4_ВЕРХ вдоль цифровых осей.dxf",
            ),
        ),
        metric_status=EngineerMetricStatus.PDF_TEXT_UNREADABLE,
        notes=(_UNREADABLE_NOTE, _AXIS_NOTE),
    ),
    EngineerReferenceCase(
        id="legacy-kj00-s5-6",
        title="КЖ00 · секции 5-6",
        dataset_dir_name=_LEGACY_DATASET,
        engineer_pdf_parts=("1-КЖ00.С5-6", "л11, 13, 15-16.pdf"),
        input_sets=(
            _input_set(
                "legacy-s5",
                "Секция 5",
                ("1-КЖ00.С5-6",),
                bottom_x="С5_Х низ.dxf",
                bottom_y="С5_У низ.dxf",
                top_x="С5_Х верх.dxf",
                top_y="С5_У верх.dxf",
            ),
            _input_set(
                "legacy-s6",
                "Секция 6",
                ("1-КЖ00.С5-6",),
                bottom_x="С6_Х низ.dxf",
                bottom_y="С6_У низ.dxf",
                top_x="С6_Х верх.dxf",
                top_y="С6_У верх.dxf",
            ),
        ),
        metric_status=EngineerMetricStatus.PDF_TEXT_UNREADABLE,
        notes=(_UNREADABLE_NOTE, _AXIS_NOTE),
    ),
    EngineerReferenceCase(
        id="legacy-kj00-s7-8",
        title="КЖ00 · секции 7-8",
        dataset_dir_name=_LEGACY_DATASET,
        engineer_pdf_parts=("1-КЖ00.С7-8", "л9-11.pdf"),
        input_sets=(
            _input_set(
                "legacy-s7-8",
                "Секции 7-8 · объединённый вход",
                ("1-КЖ00.С7-8",),
                bottom_x="Х_низ.dxf",
                bottom_y="У_низ.dxf",
                top_x="Х_верх.dxf",
                top_y="У_верх.dxf",
            ),
        ),
        metric_status=EngineerMetricStatus.PDF_TEXT_UNREADABLE,
        notes=(_UNREADABLE_NOTE, _AXIS_NOTE),
    ),
    EngineerReferenceCase(
        id="legacy-kj00-section-2",
        title="КЖ00 · корпус 2",
        dataset_dir_name=_LEGACY_DATASET,
        engineer_pdf_parts=("2-КЖ00", "л8-9.pdf"),
        input_sets=(
            _input_set(
                "legacy-section-2",
                "Корпус 2",
                ("2-КЖ00",),
                bottom_x="Нижняя ар-ра_по_оси_X.dxf",
                bottom_y="Нижняя ар-ра_по_оси_Y.dxf",
                top_x="Верхняя ар-ра_по_оси_X.dxf",
                top_y="Верхняя ар-ра_по_оси_Y.dxf",
            ),
        ),
        metric_status=EngineerMetricStatus.PDF_TEXT_UNREADABLE,
        notes=(_UNREADABLE_NOTE, _AXIS_NOTE),
    ),
    EngineerReferenceCase(
        id="legacy-kj00-section-4",
        title="КЖ00 · корпус 4",
        dataset_dir_name=_LEGACY_DATASET,
        engineer_pdf_parts=("4-КЖ00", "л10-13.pdf"),
        input_sets=(
            _input_set(
                "legacy-section-4",
                "Корпус 4",
                ("4-КЖ00",),
                bottom_x="Нижняя по Х.dxf",
                bottom_y="Нижняя по У.dxf",
                top_x="Верхняя по Х.dxf",
                top_y="Верхняя по У.dxf",
            ),
        ),
        metric_status=EngineerMetricStatus.PDF_TEXT_UNREADABLE,
        notes=(_UNREADABLE_NOTE, _AXIS_NOTE),
    ),
    EngineerReferenceCase(
        id="k09-foundation",
        title="Корпус 2.9 · фундаментная плита",
        dataset_dir_name=_K09_DATASET,
        engineer_pdf_parts=(
            _K09_ROOT,
            "2025-04-24_ППТ8-1-Д2-Р-9-КЖ00_фундаментная плита",
            "ППТ8-1-Д2-Р-9-КЖ00.pdf",
        ),
        input_sets=(
            _input_set(
                "k09-foundation-input",
                "Фундаментная плита",
                (_K09_ROOT, "_Задания", "2024.12.17_фундаментная плита", "Изополя"),
                bottom_x="Нижняя по Х.dxf",
                bottom_y="Нижняя по У.dxf",
                top_x="Верхняя по Х.dxf",
                top_y="Верхняя по У.dxf",
            ),
        ),
        metric_status=EngineerMetricStatus.VERIFIED_PDF_SPEC,
        sheets=(
            _sheet(8, BOTTOM_Y, 45, 251, 2958.48),
            _sheet(9, BOTTOM_X, 56, 313, 4928.12),
            _sheet(10, TOP_Y, 20, 329, 4144.18),
            _sheet(11, TOP_X, 25, 464, 8982.09),
        ),
        notes=(
            "Четыре листа дополнительной арматуры повторно проверены по тексту и рендеру PDF.",
            "Специальные гнутые позиции фундаментной плиты выходят за прямоугольный MVP.",
            _AXIS_NOTE,
        ),
    ),
    EngineerReferenceCase(
        id="k09-minus-2",
        title="Корпус 2.9 · плита над минус 2 этажом",
        dataset_dir_name=_K09_DATASET,
        engineer_pdf_parts=(
            _K09_ROOT,
            "2025-04-30_ППТ8-1-Д2-Р-9-КЖ02.П2_плита над минус 2 этажом",
            "ППТ8-1-Д2-Р-9-КЖ02.П2.pdf",
        ),
        input_sets=(
            _input_set(
                "k09-minus-2-input",
                "Плита над минус 2 этажом",
                (_K09_ROOT, "_Задания", "2025.02.04_плита над минус 2 этажом", "Изополя"),
                bottom_x="Нижняя по Х.dxf",
                bottom_y="Нижняя по У.dxf",
                top_x="Верхняя по Х.dxf",
                top_y="Верхняя по У.dxf",
            ),
        ),
        metric_status=EngineerMetricStatus.VERIFIED_PDF_SPEC,
        sheets=(
            _sheet(7, BOTTOM_Y, 9, 176, 699.76),
            _sheet(8, BOTTOM_X, 13, 172, 605.18),
            _sheet(9, TOP_Y, 16, 239, 984.09),
            _sheet(10, TOP_X, 13, 269, 971.59),
        ),
        notes=(
            "Четыре листа дополнительной арматуры повторно проверены по тексту и рендеру PDF.",
            _AXIS_NOTE,
        ),
    ),
    EngineerReferenceCase(
        id="plate-zero-k09",
        title="Корпус 2.9 · плита нуля",
        dataset_dir_name=_K09_DATASET,
        engineer_pdf_parts=(
            _K09_ROOT,
            "2025-05-20_ППТ8-1-Д2-Р-9-КЖ02.П1_плита нуля",
            "ППТ8-1-Д2-Р-9-КЖ02.П1.pdf",
        ),
        input_sets=(
            _input_set(
                "plate-zero-k09-input",
                "Плита нуля",
                (_K09_ROOT, "_Задания", "2025.02.19_плита нуля", "Изополя"),
                bottom_x="Нижняя по Х.dxf",
                bottom_y="Нижняя по У.dxf",
                top_x="Верхняя по Х.dxf",
                top_y="Верхняя по У.dxf",
            ),
        ),
        metric_status=EngineerMetricStatus.VERIFIED_PDF_SPEC,
        sheets=(
            _sheet(8, BOTTOM_Y, 16, 192, 651.58),
            _sheet(9, BOTTOM_X, 17, 213, 700.60),
            _sheet(10, TOP_Y, 22, 292, 826.44),
            _sheet(11, TOP_X, 24, 322, 999.02),
        ),
        notes=(
            "Единственный случай с подключённым end-to-end golden-тестом и явным mapping.",
            _AXIS_NOTE,
        ),
    ),
    EngineerReferenceCase(
        id="k09-above-1",
        title="Корпус 2.9 · плита над 1 этажом",
        dataset_dir_name=_K09_DATASET,
        engineer_pdf_parts=(
            _K09_ROOT,
            "2025-08-14_ППТ8-1-Д2-Р-9-КЖ2.1_плита над 1 этажом",
            "ППТ8-1-Д2-Р-9-КЖ2.1.pdf",
        ),
        input_sets=(
            _input_set(
                "k09-above-1-input",
                "Плита над 1 этажом",
                (_K09_ROOT, "_Задания", "2025.05.05_плита над 1 этажом", "Допка плит"),
                bottom_x="Х Низ.dxf",
                bottom_y="У Низ.dxf",
                top_x="Х Верх.dxf",
                top_y="У Верх.dxf",
            ),
        ),
        metric_status=EngineerMetricStatus.VERIFIED_PDF_SPEC,
        sheets=(
            _sheet(11, BOTTOM_X, 14, 128, 300.99),
            _sheet(12, BOTTOM_Y, 19, 177, 301.89),
            _sheet(13, TOP_X, 20, 332, 622.50),
            _sheet(14, TOP_Y, 25, 243, 479.74),
        ),
        notes=(
            "Четыре листа дополнительной арматуры повторно проверены по тексту и рендеру PDF.",
            _AXIS_NOTE,
        ),
    ),
    EngineerReferenceCase(
        id="k09-typical-3-14",
        title="Корпус 2.9 · типовые плиты над 3-14 этажами",
        dataset_dir_name=_K09_DATASET,
        engineer_pdf_parts=(
            _K09_ROOT,
            "2025-08-18_ППТ8-1-Д2-Р-9-КЖ2.3-14_плиты перекрытия над 3-14 этажами",
            "ППТ8-1-Д2-Р-9-КЖ2.3-14.pdf",
        ),
        input_sets=(
            _input_set(
                "k09-above-3-input",
                "Плита над 3 этажом",
                (
                    _K09_ROOT,
                    "_Задания",
                    "2025.08.13_плиты перекрытия над 3-14 этажами",
                    "Плита над 3 этажом",
                    "Изополя",
                ),
                bottom_x="Нижняя по Х.dxf",
                bottom_y="Нижняя по У.dxf",
                top_x="Верхняя по Х.dxf",
                top_y="Верхняя по У.dxf",
            ),
            _input_set(
                "k09-above-9-input",
                "Плита над 9 этажом",
                (
                    _K09_ROOT,
                    "_Задания",
                    "2025.08.13_плиты перекрытия над 3-14 этажами",
                    "Плита над 9 этажом",
                    "Изополя",
                ),
                bottom_x="Нижняя по Х.dxf",
                bottom_y="Нижняя по У.dxf",
                top_x="Верхняя по Х.dxf",
                top_y="Верхняя по У.dxf",
            ),
        ),
        metric_status=EngineerMetricStatus.VERIFIED_PDF_SPEC,
        sheets=(
            _sheet(9, (BOTTOM_X, BOTTOM_Y), 30, 440, 1050.39),
            _sheet(10, (TOP_X, TOP_Y), 44, 787, 1828.37),
        ),
        notes=(
            "Один типовой инженерный результат связан с двумя входами: плитами над 3 и 9 этажами.",
            "На каждом релевантном листе объединены обе оси одного слоя; доступны plate-level итоги.",
            _AXIS_NOTE,
        ),
    ),
)

ENGINEER_REFERENCE_CASES_BY_ID = {case.id: case for case in ENGINEER_REFERENCE_CASES}


def get_engineer_reference_case(case_id: str) -> EngineerReferenceCase:
    """Вернуть инженерскую выдачу по стабильному идентификатору."""

    try:
        return ENGINEER_REFERENCE_CASES_BY_ID[case_id]
    except KeyError as error:
        available = ", ".join(sorted(ENGINEER_REFERENCE_CASES_BY_ID))
        raise KeyError(f"Неизвестная инженерская выдача {case_id!r}; доступны: {available}") from error
