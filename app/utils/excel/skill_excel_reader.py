import logging
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = [
    "canonical_name",
    "aliases",
    "category",
    "parent_skill",
    "confidence",
    "source",
    "is_active",
]


class SkillExcelReader:
    """
    Parses Skill Ontology rows out of an Excel workbook.

    Pure parsing utility: performs no database access, so it can be shared
    as-is between the seed script and the future Bulk Import feature.
    """

    @staticmethod
    def read(file_path: str | Path) -> list[dict[str, Any]]:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Skill ontology Excel file not found: {path}")

        workbook = load_workbook(filename=path, read_only=True, data_only=True)
        worksheet = workbook.active

        rows = worksheet.iter_rows(values_only=True)
        header_row = next(rows, None)
        if header_row is None:
            raise ValueError(f"Skill ontology Excel file has no header row: {path}")

        headers = [str(cell).strip() if cell is not None else "" for cell in header_row]
        missing_columns = [column for column in REQUIRED_COLUMNS if column not in headers]
        if missing_columns:
            raise ValueError(
                f"Skill ontology Excel is missing required column(s): {', '.join(missing_columns)}"
            )
        column_index = {column: headers.index(column) for column in REQUIRED_COLUMNS}

        skills: list[dict[str, Any]] = []
        for row_number, row in enumerate(rows, start=2):
            if row is None or all(cell is None or str(cell).strip() == "" for cell in row):
                continue  # skip completely empty rows

            skill = SkillExcelReader._parse_row(row, column_index)
            if not skill["canonical_name"]:
                logger.warning("Skipping row %s: canonical_name is required.", row_number)
                continue

            skills.append(skill)

        return skills

    @staticmethod
    def _parse_row(row: tuple, column_index: dict[str, int]) -> dict[str, Any]:
        def cell(column: str) -> Any:
            index = column_index[column]
            return row[index] if index < len(row) else None

        def clean_str(value: Any) -> str:
            return str(value).strip() if value is not None else ""

        aliases_raw = clean_str(cell("aliases"))
        aliases = [alias.strip() for alias in aliases_raw.split(",") if alias.strip()]

        return {
            "canonical_name": clean_str(cell("canonical_name")),
            "aliases": aliases,
            "category": clean_str(cell("category")) or None,
            "parent_skill": clean_str(cell("parent_skill")) or None,
            "confidence": clean_str(cell("confidence")).lower() or "unverified",
            "source": clean_str(cell("source")).lower() or None,
            "is_active": SkillExcelReader._parse_bool(cell("is_active")),
        }

    @staticmethod
    def _parse_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().upper() == "TRUE"
