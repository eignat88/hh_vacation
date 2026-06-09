"""Output writers for vacancy export rows."""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any

import pandas as pd

OUTPUT_COLUMNS = [
    "vacancy_id",
    "name",
    "employer_name",
    "area_name",
    "salary_from",
    "salary_to",
    "salary_currency",
    "salary_gross",
    "experience",
    "employment",
    "schedule",
    "published_at",
    "alternate_url",
    "key_skills",
    "snippet_requirement",
    "snippet_responsibility",
    "requirements_from_description",
    "responsibilities_from_description",
    "conditions_from_description",
    "full_description_text",
    "source_query",
    "loaded_at",
]


def save_to_excel(rows: list[dict[str, Any]], path: Path) -> None:
    """Save rows to an Excel file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    dataframe = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    dataframe.to_excel(path, index=False, engine="openpyxl")


def save_to_csv(rows: list[dict[str, Any]], path: Path) -> None:
    """Save rows to a UTF-8-SIG CSV file friendly to Excel on Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    dataframe = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    dataframe.to_csv(path, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)


def save_outputs(rows: list[dict[str, Any]], out: Path, output_format: str) -> list[Path]:
    """Save requested output formats and return created paths.

    Empty exports are still written with headers so the file shape is explicit,
    but a warning is logged to make the potentially surprising result visible.
    """
    if not rows:
        logging.warning("Нет строк для выгрузки; будут сохранены только заголовки колонок.")

    created_paths: list[Path] = []

    if output_format in {"xlsx", "both"}:
        xlsx_path = out if out.suffix.lower() == ".xlsx" else out.with_suffix(".xlsx")
        save_to_excel(rows, xlsx_path)
        created_paths.append(xlsx_path)

    if output_format in {"csv", "both"}:
        csv_path = out if out.suffix.lower() == ".csv" else out.with_suffix(".csv")
        save_to_csv(rows, csv_path)
        created_paths.append(csv_path)

    return created_paths
