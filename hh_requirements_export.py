#!/usr/bin/env python3
"""Export hh.ru vacancy requirements to Excel/CSV.

The script uses the official HeadHunter API only. It searches vacancies by a
user query, loads each detailed vacancy card, extracts useful structured fields
and applies simple heuristics to isolate requirements, responsibilities and
conditions from the full description text.
"""

from __future__ import annotations

import argparse
import csv
import html
import logging
import re
import sys
import time
from datetime import datetime, timezone
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests import Response, Session

if find_spec("tqdm"):
    tqdm = import_module("tqdm").tqdm
else:
    def tqdm(iterable, **_kwargs):
        """Minimal fallback if tqdm is not installed yet."""
        return iterable

HH_API_BASE_URL = "https://api.hh.ru"
DEFAULT_AREA = "113"
DEFAULT_PAGES = 5
DEFAULT_PER_PAGE = 50
DEFAULT_DELAY = 0.3
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2

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

REQUIREMENT_HEADINGS = [
    "Требования",
    "Наши требования",
    "Мы ожидаем",
    "Что мы ожидаем",
    "Что нужно знать",
    "Что важно",
    "Кого мы ищем",
    "Необходимые навыки",
    "Пожелания к кандидату",
    "Вы нам подходите, если",
    "Будет плюсом",
    "Что нужно уметь",
]

RESPONSIBILITY_HEADINGS = [
    "Обязанности",
    "Что предстоит делать",
    "Задачи",
    "Чем предстоит заниматься",
    "Что нужно делать",
    "Ваши задачи",
    "Функционал",
]

CONDITION_HEADINGS = [
    "Условия",
    "Мы предлагаем",
    "Что мы предлагаем",
    "Преимущества",
    "Что предлагаем",
    "У нас вы получите",
    "Для вас",
]

COMMON_STOP_HEADINGS = [
    "О компании",
    "Почему мы",
    "Этапы отбора",
    "Контакты",
    "Ждем вас",
]

REQUIREMENT_STOP_HEADINGS = RESPONSIBILITY_HEADINGS + CONDITION_HEADINGS + COMMON_STOP_HEADINGS
RESPONSIBILITY_STOP_HEADINGS = REQUIREMENT_HEADINGS + CONDITION_HEADINGS + COMMON_STOP_HEADINGS
CONDITION_STOP_HEADINGS = REQUIREMENT_HEADINGS + RESPONSIBILITY_HEADINGS + COMMON_STOP_HEADINGS


def configure_logging(debug: bool) -> None:
    """Configure console and file logging."""
    logs_dir = Path("logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_level = logging.DEBUG if debug else logging.INFO
    log_format = "[%(levelname)s] %(message)s"

    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(logging.Formatter(log_format))

    file_handler = logging.FileHandler(logs_dir / "hh_parser.log", encoding="utf-8")
    file_handler.setLevel(log_level)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    root.addHandler(console_handler)
    root.addHandler(file_handler)


def create_session() -> Session:
    """Create a requests session with headers recommended for public API clients."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "hh-requirements-export/1.0 (+https://api.hh.ru)",
            "Accept": "application/json",
        }
    )
    return session


def request_json(session: Session, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET JSON with retries for temporary API/network errors."""
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logging.debug("GET %s params=%s attempt=%s", url, params, attempt)
            response: Response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", RETRY_BACKOFF_SECONDS * attempt))
                logging.warning("Превышен лимит запросов. Пауза %s сек.", retry_after)
                time.sleep(retry_after)
                continue

            if response.status_code in {500, 502, 503, 504}:
                logging.warning(
                    "Временная ошибка API %s. Повтор %s/%s",
                    response.status_code,
                    attempt,
                    MAX_RETRIES,
                )
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue

            if response.status_code in {403, 404, 410}:
                response.raise_for_status()

            response.raise_for_status()
            return response.json()
        except requests.exceptions.JSONDecodeError as exc:
            last_error = exc
            logging.warning("Некорректный JSON: %s", exc)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        except requests.exceptions.RequestException as exc:
            last_error = exc
            logging.warning("Ошибка запроса: %s", exc)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    if last_error:
        raise last_error
    raise RuntimeError(f"Не удалось получить данные: {url}")


def search_vacancies(
    session: Session,
    text: str,
    area: str,
    pages: int,
    per_page: int,
    search_field: str | None = None,
    only_with_salary: bool = False,
    currency: str | None = None,
    schedule: str | None = None,
    experience: str | None = None,
    delay: float = DEFAULT_DELAY,
) -> tuple[list[dict[str, Any]], int]:
    """Search vacancies and return search-result items plus the API total count."""
    vacancies: list[dict[str, Any]] = []
    total_found = 0

    for page in range(pages):
        logging.info("Страница %s из %s", page + 1, pages)
        params: dict[str, Any] = {
            "text": text,
            "area": area,
            "page": page,
            "per_page": per_page,
        }
        if search_field:
            params["search_field"] = search_field
        if only_with_salary:
            params["only_with_salary"] = "true"
        if currency:
            params["currency"] = currency
        if schedule:
            params["schedule"] = schedule
        if experience:
            params["experience"] = experience

        data = request_json(session, f"{HH_API_BASE_URL}/vacancies", params=params)
        items = data.get("items", [])
        total_found = int(data.get("found") or 0)
        logging.info("Найдено вакансий на странице: %s", len(items))
        vacancies.extend(items)

        if page + 1 >= int(data.get("pages") or 0):
            break
        time.sleep(delay)

    return vacancies, total_found


def get_vacancy_details(session: Session, vacancy_id: str) -> dict[str, Any]:
    """Load a detailed vacancy card by ID."""
    return request_json(session, f"{HH_API_BASE_URL}/vacancies/{vacancy_id}")


def clean_html(raw_html: str | None) -> str:
    """Convert vacancy HTML to readable plain text while preserving list structure."""
    if not raw_html:
        return ""

    soup = BeautifulSoup(raw_html, "html.parser")

    for br in soup.find_all("br"):
        br.replace_with("\n")

    for li in soup.find_all("li"):
        text = li.get_text(" ", strip=True)
        li.replace_with(f"\n- {text}\n")

    for block in soup.find_all(["p", "div", "section", "tr", "h1", "h2", "h3", "h4", "h5", "h6"]):
        block.insert_before("\n")
        block.insert_after("\n")

    text = soup.get_text(" ")
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_heading(text: str) -> str:
    """Normalize section headings for case-insensitive matching."""
    normalized = re.sub(r"^[\s\-–—•\d.)]+", "", text.strip().lower())
    normalized = normalized.replace("ё", "е")
    normalized = re.sub(r"[:;.\s]+$", "", normalized)
    return normalized


def heading_matches(line: str, headings: Iterable[str]) -> tuple[bool, str]:
    """Return whether a line starts with one of the expected headings."""
    line_normalized = normalize_heading(line)
    for heading in headings:
        heading_normalized = normalize_heading(heading)
        if line_normalized == heading_normalized:
            return True, ""
        if line_normalized.startswith(f"{heading_normalized}:"):
            remainder = line.split(":", 1)[1].strip() if ":" in line else ""
            return True, remainder
    return False, ""


def is_stop_heading(line: str, stop_headings: Iterable[str]) -> bool:
    """Detect if a line starts a new semantic block."""
    matched, _ = heading_matches(line, stop_headings)
    return matched


def extract_section(text: str, start_headings: Iterable[str], stop_headings: Iterable[str]) -> str:
    """Extract text after a semantic heading until the next semantic heading."""
    if not text:
        return ""

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    collected: list[str] = []
    is_collecting = False

    for line in lines:
        if not is_collecting:
            matched, inline_remainder = heading_matches(line, start_headings)
            if matched:
                is_collecting = True
                if inline_remainder:
                    collected.append(inline_remainder)
            continue

        if is_stop_heading(line, stop_headings):
            break
        collected.append(line)

    return "\n".join(collected).strip()


def extract_requirements(text: str) -> str:
    """Extract candidate requirements from a cleaned vacancy description."""
    return extract_section(text, REQUIREMENT_HEADINGS, REQUIREMENT_STOP_HEADINGS)


def normalize_salary(salary: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize a possibly missing or partially filled HH salary object."""
    salary = salary or {}
    return {
        "salary_from": salary.get("from"),
        "salary_to": salary.get("to"),
        "salary_currency": salary.get("currency"),
        "salary_gross": salary.get("gross"),
    }


def safe_nested_value(data: dict[str, Any], key: str, nested_key: str = "name") -> str:
    """Read nested dictionary values safely and return an empty string if absent."""
    value = data.get(key) or {}
    return value.get(nested_key) or "" if isinstance(value, dict) else ""


def build_row(search_item: dict[str, Any], details: dict[str, Any], source_query: str, loaded_at: str) -> dict[str, Any]:
    """Build one output row from search and detailed vacancy API data."""
    description_text = clean_html(details.get("description"))
    salary_fields = normalize_salary(details.get("salary") or search_item.get("salary"))
    key_skills = ", ".join(skill.get("name", "") for skill in details.get("key_skills", []) if skill.get("name"))
    snippet = search_item.get("snippet") or {}

    return {
        "vacancy_id": details.get("id") or search_item.get("id") or "",
        "name": details.get("name") or search_item.get("name") or "",
        "employer_name": safe_nested_value(details, "employer") or safe_nested_value(search_item, "employer"),
        "area_name": safe_nested_value(details, "area") or safe_nested_value(search_item, "area"),
        **salary_fields,
        "experience": safe_nested_value(details, "experience") or safe_nested_value(search_item, "experience"),
        "employment": safe_nested_value(details, "employment") or safe_nested_value(search_item, "employment"),
        "schedule": safe_nested_value(details, "schedule") or safe_nested_value(search_item, "schedule"),
        "published_at": details.get("published_at") or search_item.get("published_at") or "",
        "alternate_url": details.get("alternate_url") or search_item.get("alternate_url") or "",
        "key_skills": key_skills,
        "snippet_requirement": clean_html(snippet.get("requirement")),
        "snippet_responsibility": clean_html(snippet.get("responsibility")),
        "requirements_from_description": extract_requirements(description_text),
        "responsibilities_from_description": extract_section(
            description_text,
            RESPONSIBILITY_HEADINGS,
            RESPONSIBILITY_STOP_HEADINGS,
        ),
        "conditions_from_description": extract_section(
            description_text,
            CONDITION_HEADINGS,
            CONDITION_STOP_HEADINGS,
        ),
        "full_description_text": description_text,
        "source_query": source_query,
        "loaded_at": loaded_at,
    }


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
    """Save requested output formats and return created paths."""
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


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Выгрузка требований из вакансий hh.ru через официальный API в Excel/CSV."
    )
    parser.add_argument("--text", required=True, help="Поисковый запрос, например: 'менеджер маркетплейсов'.")
    parser.add_argument("--out", required=True, help="Путь к выходному файлу .xlsx или .csv.")
    parser.add_argument("--area", default=DEFAULT_AREA, help="Регион поиска hh.ru. По умолчанию 113 (Россия).")
    parser.add_argument("--pages", type=int, default=DEFAULT_PAGES, help="Количество страниц поиска.")
    parser.add_argument("--per-page", type=int, default=DEFAULT_PER_PAGE, help="Количество вакансий на странице.")
    parser.add_argument("--search-field", choices=["name", "company_name", "description"], help="Область поиска hh.ru.")
    parser.add_argument("--only-with-salary", action="store_true", help="Искать только вакансии с указанной зарплатой.")
    parser.add_argument("--currency", help="Валюта зарплаты для фильтра API, например RUR.")
    parser.add_argument("--schedule", help="График работы в формате справочника hh.ru, например remote.")
    parser.add_argument("--experience", help="Требуемый опыт в формате справочника hh.ru, например between1And3.")
    parser.add_argument("--output-format", choices=["xlsx", "csv", "both"], default="xlsx", help="Формат выгрузки.")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="Пауза между запросами к API в секундах.")
    parser.add_argument("--debug", action="store_true", help="Подробное логирование.")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    """Validate argument values that should be positive."""
    if args.pages < 1:
        raise ValueError("--pages должен быть больше 0")
    if args.per_page < 1 or args.per_page > 100:
        raise ValueError("--per-page должен быть от 1 до 100")
    if args.delay < 0:
        raise ValueError("--delay не может быть отрицательным")


def main() -> int:
    """Run the export process."""
    args = parse_args()
    configure_logging(args.debug)

    try:
        validate_args(args)
    except ValueError as exc:
        logging.error(str(exc))
        return 2

    logging.info("Старт выгрузки")
    logging.info("Запрос: %s", args.text)
    logging.info("Регион: %s", args.area)
    logging.debug("Параметры запуска: %s", vars(args))

    session = create_session()
    loaded_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    rows: list[dict[str, Any]] = []
    skipped = 0

    try:
        search_items, total_found = search_vacancies(
            session=session,
            text=args.text,
            area=args.area,
            pages=args.pages,
            per_page=args.per_page,
            search_field=args.search_field,
            only_with_salary=args.only_with_salary,
            currency=args.currency,
            schedule=args.schedule,
            experience=args.experience,
            delay=args.delay,
        )
    except requests.exceptions.ConnectionError:
        logging.error("Нет подключения к интернету или api.hh.ru недоступен.")
        return 1
    except requests.exceptions.RequestException as exc:
        logging.error("Не удалось выполнить поиск вакансий: %s", exc)
        return 1

    logging.info("Всего найдено по запросу: %s", total_found)
    logging.info("Получено из поисковой выдачи для обработки: %s", len(search_items))

    for item in tqdm(search_items, desc="Обработка вакансий", unit="vacancy"):
        vacancy_id = str(item.get("id") or "")
        vacancy_name = item.get("name") or ""
        logging.info("Обработка вакансии %s: %s", vacancy_id, vacancy_name)

        try:
            details = get_vacancy_details(session, vacancy_id)
            row = build_row(item, details, args.text, loaded_at)
            if not row["requirements_from_description"]:
                logging.warning("Не удалось извлечь блок требований для вакансии %s", vacancy_id)
            rows.append(row)
        except requests.exceptions.HTTPError as exc:
            skipped += 1
            logging.warning("Вакансия %s недоступна и будет пропущена: %s", vacancy_id, exc)
        except requests.exceptions.ConnectionError:
            skipped += 1
            logging.warning("Ошибка соединения при обработке вакансии %s. Вакансия пропущена.", vacancy_id)
        except Exception as exc:  # noqa: BLE001 - batch export must continue on one bad vacancy.
            skipped += 1
            logging.exception("Ошибка обработки вакансии %s: %s", vacancy_id, exc)
        finally:
            time.sleep(args.delay)

    out_path = Path(args.out)
    created_paths = save_outputs(rows, out_path, args.output_format)

    logging.info("Выгрузка завершена. Обработано: %s", len(rows))
    logging.info("Пропущено из-за ошибок: %s", skipped)
    for path in created_paths:
        logging.info("Файл сохранён: %s", path)

    print("\nИтог выполнения:")
    print(f"- найдено вакансий по запросу: {total_found}")
    print(f"- успешно обработано: {len(rows)}")
    print(f"- пропущено из-за ошибок: {skipped}")
    print("- итоговые файлы:")
    for path in created_paths:
        print(f"  - {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
