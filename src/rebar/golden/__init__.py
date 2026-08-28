"""Проверенные инженерные эталоны и их извлечение из локального датасета."""

from .catalog import GOLDEN_CASES, PLATE_ZERO_K09, get_golden_case
from .dataset_catalog import (
    ENGINEER_REFERENCE_CASES,
    ENGINEER_REFERENCE_CASES_BY_ID,
    get_engineer_reference_case,
)
from .dataset_inventory import (
    build_engineer_dataset_inventory,
    extract_engineer_reference_metrics,
)
from .dataset_models import (
    EngineerInputSetDefinition,
    EngineerMetricStatus,
    EngineerReferenceCase,
    EngineerReferenceFiles,
    EngineerReferenceMetrics,
    EngineerReferenceSheet,
)
from .models import (
    GoldenBarPosition,
    GoldenCaseDefinition,
    GoldenCaseFiles,
    GoldenSheetExpectation,
    GoldenSheetMetrics,
)
from .metric_reference import (
    PlateMetricReference,
    plate_metric_reference_from_engineer,
    plate_metric_reference_from_golden,
)
from .pdf_specs import (
    GoldenPdfToolError,
    GoldenReferenceMismatchError,
    extract_sheet_metrics,
    parse_specification_text,
    render_pdf_page_png,
    validate_sheet_metrics,
)
from .sources import (
    GoldenSourceNotFoundError,
    resolve_engineer_reference_files,
    resolve_golden_case_files,
)

__all__ = [
    "ENGINEER_REFERENCE_CASES",
    "ENGINEER_REFERENCE_CASES_BY_ID",
    "GOLDEN_CASES",
    "PLATE_ZERO_K09",
    "EngineerInputSetDefinition",
    "EngineerMetricStatus",
    "EngineerReferenceCase",
    "EngineerReferenceFiles",
    "EngineerReferenceMetrics",
    "EngineerReferenceSheet",
    "GoldenBarPosition",
    "GoldenCaseDefinition",
    "GoldenCaseFiles",
    "GoldenPdfToolError",
    "GoldenReferenceMismatchError",
    "GoldenSheetExpectation",
    "GoldenSheetMetrics",
    "GoldenSourceNotFoundError",
    "PlateMetricReference",
    "build_engineer_dataset_inventory",
    "extract_engineer_reference_metrics",
    "extract_sheet_metrics",
    "get_engineer_reference_case",
    "get_golden_case",
    "parse_specification_text",
    "plate_metric_reference_from_engineer",
    "plate_metric_reference_from_golden",
    "render_pdf_page_png",
    "resolve_engineer_reference_files",
    "resolve_golden_case_files",
    "validate_sheet_metrics",
]
