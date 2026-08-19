"""Проверенные инженерные эталоны и их извлечение из локального датасета."""

from .catalog import GOLDEN_CASES, PLATE_ZERO_K09, get_golden_case
from .models import (
    GoldenBarPosition,
    GoldenCaseDefinition,
    GoldenCaseFiles,
    GoldenSheetExpectation,
    GoldenSheetMetrics,
)
from .pdf_specs import (
    GoldenPdfToolError,
    GoldenReferenceMismatchError,
    extract_sheet_metrics,
    parse_specification_text,
    render_pdf_page_png,
    validate_sheet_metrics,
)
from .sources import GoldenSourceNotFoundError, resolve_golden_case_files

__all__ = [
    "GOLDEN_CASES",
    "PLATE_ZERO_K09",
    "GoldenBarPosition",
    "GoldenCaseDefinition",
    "GoldenCaseFiles",
    "GoldenPdfToolError",
    "GoldenReferenceMismatchError",
    "GoldenSheetExpectation",
    "GoldenSheetMetrics",
    "GoldenSourceNotFoundError",
    "extract_sheet_metrics",
    "get_golden_case",
    "parse_specification_text",
    "render_pdf_page_png",
    "resolve_golden_case_files",
    "validate_sheet_metrics",
]
