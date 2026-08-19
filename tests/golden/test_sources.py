"""Поиск материалов при разных Unicode-нормализациях имён файлов."""

import unicodedata

from rebar.golden import PLATE_ZERO_K09, resolve_golden_case_files


def _nfd(value: str) -> str:
    return unicodedata.normalize("NFD", value)


def test_resolve_golden_sources_accepts_decomposed_unicode_names(tmp_path):
    case = PLATE_ZERO_K09
    dataset = tmp_path / _nfd(case.dataset_dir_name)
    task = dataset / _nfd(case.input_task_dir_name) / "Изополя"
    package = dataset / "package"
    task.mkdir(parents=True)
    package.mkdir()
    pdf = package / _nfd(case.engineer_pdf_name)
    pdf.write_bytes(b"pdf")
    for sheet in case.sheets:
        (task / _nfd(sheet.input_png_name)).write_bytes(b"png")

    files = resolve_golden_case_files(case, tmp_path)

    assert files.engineer_pdf == pdf
    assert set(files.input_png_by_direction) == {sheet.direction for sheet in case.sheets}

