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
from email.utils import parsedate_to_datetime
import json
import logging
import os
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
HH_USER_AGENT_ENV = "HH_USER_AGENT"
HH_ACCESS_TOKEN_ENV = "HH_ACCESS_TOKEN"
DEFAULT_HH_USER_AGENT = "hh-requirements-export/1.0 (set HH_USER_AGENT or --user-agent)"
USER_AGENT_FORMAT_RE = re.compile(
    r"^[A-Z0-9._-]+/[A-Z0-9._+-]+ \([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\)$",
    re.IGNORECASE,
)

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


def validate_user_agent_value(user_agent: str | None) -> str | None:
    """Return a validation error for HH API User-Agent or None when it looks usable."""
    resolved_user_agent = (user_agent or "").strip()
    if not resolved_user_agent:
        return (
            "Для запросов к API hh.ru необходимо указать контактный User-Agent: "
            "передайте --user-agent 'MyApp/1.0 (you@example.com)' "
            f"или задайте переменную окружения {HH_USER_AGENT_ENV}."
        )
    if resolved_user_agent == DEFAULT_HH_USER_AGENT or "set HH_USER_AGENT" in resolved_user_agent:
        return (
            "Встроенный User-Agent является только подсказкой и может приводить к 403 Forbidden от hh.ru. "
            "Укажите собственный контактный User-Agent в формате 'MyApp/1.0 (you@example.com)'."
        )
    if not USER_AGENT_FORMAT_RE.fullmatch(resolved_user_agent):
        return (
            "User-Agent для hh.ru должен быть в формате "
            "'ApplicationName/Version (contact_email)', например 'MyApp/1.0 (you@example.com)'."
        )
    return None


def resolve_access_token(access_token: str | None = None) -> str:
    """Resolve an optional OAuth token without treating blanks as credentials."""
    token_source = os.getenv(HH_ACCESS_TOKEN_ENV) if access_token is None else access_token
    return (token_source or "").strip()


def create_session(user_agent: str | None = None, access_token: str | None = None) -> Session:
    """Create a requests session for public or optionally authorized HH API calls."""
    resolved_user_agent = (user_agent or os.getenv(HH_USER_AGENT_ENV) or DEFAULT_HH_USER_AGENT).strip()
    resolved_access_token = resolve_access_token(access_token)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": resolved_user_agent,
            "HH-User-Agent": resolved_user_agent,
            "Accept": "application/json",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }
    )
    if resolved_access_token:
        session.headers["Authorization"] = f"Bearer {resolved_access_token}"
    return session


def format_api_error(response: Response) -> str:
    """Build a compact, human-readable error detail from an HH API response."""
    details: list[str] = []
    request_id = response.headers.get("HH-Request-Id") or response.headers.get("X-Request-Id")

    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list):
            for error in errors:
                if not isinstance(error, dict):
                    continue
                parts = [str(error.get("type") or "unknown")]
                if error.get("value"):
                    parts.append(str(error["value"]))
                if error.get("description"):
                    parts.append(str(error["description"]))
                details.append(": ".join(parts))
        if payload.get("description"):
            details.append(str(payload["description"]))
        if payload.get("request_id"):
            request_id = str(payload["request_id"])
    else:
        body_preview = response.text.strip()[:300]
        if body_preview:
            details.append(body_preview)

    if request_id:
        details.append(f"request_id={request_id}")

    return "; ".join(details)


def get_response_body_for_log(response: Response) -> str:
    """Return the API response body for diagnostics, preserving JSON payloads in tests."""
    body = response.text.strip()
    if body:
        return body

    try:
        payload = response.json()
    except ValueError:
        return ""
    return json.dumps(payload, ensure_ascii=False)


def extract_request_id(response: Response) -> str:
    """Extract HH request_id from headers or a JSON response body when available."""
    request_id = response.headers.get("HH-Request-Id") or response.headers.get("X-Request-Id")
    if request_id:
        return str(request_id)

    try:
        payload = response.json()
    except ValueError:
        return ""
    if isinstance(payload, dict) and payload.get("request_id"):
        return str(payload["request_id"])
    return ""


def redact_headers(headers: dict[str, Any] | requests.structures.CaseInsensitiveDict[str]) -> dict[str, str]:
    """Return request headers safe for logging without revealing OAuth tokens."""
    redacted: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() == "authorization":
            redacted[name] = "Bearer ***" if str(value).strip() else ""
        else:
            redacted[name] = str(value)
    return redacted


def sent_headers_for_log(response: Response, session: Session) -> dict[str, str]:
    """Return the headers that were sent, falling back to session defaults in mocked tests."""
    request_headers = getattr(getattr(response, "request", None), "headers", None)
    if request_headers:
        return redact_headers(request_headers)
    return redact_headers(session.headers)


def log_api_error_response(response: Response, session: Session) -> None:
    """Log required HH API diagnostics for client/rate-limit errors."""
    if response.status_code not in {400, 403, 429}:
        return

    request_id = extract_request_id(response) or "<missing>"
    logging.error(
        "Ошибка API hh.ru: HTTP %s; URL: %s; request_id: %s; тело ответа: %s; отправленные заголовки: %s",
        response.status_code,
        response.url,
        request_id,
        get_response_body_for_log(response),
        sent_headers_for_log(response, session),
    )


def raise_detailed_http_error(response: Response) -> None:
    """Raise HTTPError with HH API error payload included in the message."""
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        api_error = format_api_error(response)
        message = str(exc)
        if api_error:
            message = f"{message}; API response: {api_error}"
        raise requests.exceptions.HTTPError(message, response=response, request=exc.request) from exc


def parse_retry_after(value: str | None, fallback: int) -> int:
    """Parse Retry-After as seconds or HTTP-date, falling back on invalid values."""
    retry_after = (value or "").strip()
    if not retry_after:
        return fallback

    try:
        seconds = int(retry_after)
    except ValueError:
        seconds = None
    if seconds is not None:
        if seconds > 0:
            return seconds
        logging.debug("Некорректный числовой Retry-After %r; используется fallback %s", value, fallback)
        return fallback

    try:
        retry_at = parsedate_to_datetime(retry_after)
    except (TypeError, ValueError, IndexError, OverflowError) as exc:
        logging.debug("Не удалось разобрать Retry-After %r: %s; используется fallback %s", value, exc, fallback)
        return fallback

    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)

    delay_seconds = int((retry_at - datetime.now(timezone.utc)).total_seconds())
    return max(0, delay_seconds)


def request_json(session: Session, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET JSON with retries for temporary API/network errors."""
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logging.debug("GET %s params=%s attempt=%s", url, params, attempt)
            response: Response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            log_api_error_response(response, session)

            if response.status_code == 429:
                retry_after = parse_retry_after(response.headers.get("Retry-After"), RETRY_BACKOFF_SECONDS * attempt)
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

            raise_detailed_http_error(response)
            return response.json()
        except requests.exceptions.JSONDecodeError as exc:
            last_error = exc
            logging.warning("Некорректный JSON: %s", exc)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        except requests.exceptions.HTTPError as exc:
            response = exc.response
            if response is not None and 400 <= response.status_code < 500 and response.status_code != 429:
                raise
            last_error = exc
            logging.warning("Ошибка запроса: %s", exc)
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


def make_default_output_path(text: str) -> str:
    """Build a safe default Excel path for interactive runs."""
    slug = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]+", "_", text.strip(), flags=re.UNICODE)
    slug = slug.strip("_")[:80] or "hh_vacancies"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return str(Path("output") / f"{slug}_{timestamp}.xlsx")


def complete_interactive_args(args: argparse.Namespace) -> argparse.Namespace:
    """Prompt for missing required values when the script is run from a terminal."""
    missing = [name for name in ("text", "out") if not getattr(args, name)]
    if not missing:
        return args

    if not sys.stdin.isatty():
        missing_args = ", ".join(f"--{name}" for name in missing)
        raise ValueError(f"Не указаны обязательные параметры: {missing_args}")

    print("Интерактивный режим: укажите параметры выгрузки hh.ru.")
    if not args.text:
        args.text = input("Поисковый запрос (--text), например 'менеджер маркетплейсов': ").strip()
    if not args.text:
        raise ValueError("--text не может быть пустым")

    if not args.out:
        default_out = make_default_output_path(args.text)
        entered_out = input(f"Выходной файл (--out) [{default_out}]: ").strip()
        args.out = entered_out or default_out

    if validate_user_agent_value(args.user_agent):
        args.user_agent = input(
            "Контактный User-Agent для API hh.ru (--user-agent), например 'MyApp/1.0 (you@example.com)': "
        ).strip()

    return args


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Выгрузка требований из вакансий hh.ru через официальный API в Excel/CSV."
    )
    parser.add_argument("--text", help="Поисковый запрос, например: 'менеджер маркетплейсов'.")
    parser.add_argument("--out", help="Путь к выходному файлу .xlsx или .csv. В интерактивном режиме можно оставить пустым.")
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
    parser.add_argument(
        "--user-agent",
        default=os.getenv(HH_USER_AGENT_ENV),
        help=(
            "Значение для заголовков User-Agent и HH-User-Agent. "
            f"Можно задать через переменную окружения {HH_USER_AGENT_ENV}."
        ),
    )
    parser.add_argument(
        "--access-token",
        default=os.getenv(HH_ACCESS_TOKEN_ENV),
        help=(
            "OAuth access token для запросов с авторизацией. "
            f"Можно задать через переменную окружения {HH_ACCESS_TOKEN_ENV}."
        ),
    )
    parser.add_argument("--debug", action="store_true", help="Подробное логирование.")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    """Validate argument values before making HH API requests."""
    if args.pages < 1:
        raise ValueError("--pages должен быть больше 0")
    if args.per_page < 1 or args.per_page > 100:
        raise ValueError("--per-page должен быть от 1 до 100")
    if args.delay < 0:
        raise ValueError("--delay не может быть отрицательным")

    user_agent_error = validate_user_agent_value(args.user_agent)
    if user_agent_error:
        raise ValueError(user_agent_error)


def main() -> int:
    """Run the export process."""
    args = parse_args()
    configure_logging(args.debug)

    try:
        args = complete_interactive_args(args)
        validate_args(args)
    except ValueError as exc:
        logging.error(str(exc))
        return 2

    logging.info("Старт выгрузки")
    logging.info("Запрос: %s", args.text)
    logging.info("Регион: %s", args.area)
    args_for_log = vars(args).copy()
    if args_for_log.get("access_token"):
        args_for_log["access_token"] = "***"
    logging.debug("Параметры запуска: %s", args_for_log)

    session = create_session(user_agent=args.user_agent, access_token=args.access_token)
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
