"""HH API client helpers for vacancy export."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests
from requests import Response, Session

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
                if attempt < MAX_RETRIES:
                    time.sleep(retry_after)
                    continue
                break

            if response.status_code in {500, 502, 503, 504}:
                logging.warning(
                    "Временная ошибка API %s. Повтор %s/%s",
                    response.status_code,
                    attempt,
                    MAX_RETRIES,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                    continue
                break

            raise_detailed_http_error(response)
            return response.json()
        except requests.exceptions.JSONDecodeError as exc:
            last_error = exc
            logging.warning("Некорректный JSON: %s", exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        except requests.exceptions.HTTPError as exc:
            response = exc.response
            if response is not None and 400 <= response.status_code < 500 and response.status_code != 429:
                raise
            last_error = exc
            logging.warning("Ошибка запроса: %s", exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        except requests.exceptions.RequestException as exc:
            last_error = exc
            logging.warning("Ошибка запроса: %s", exc)
            if attempt < MAX_RETRIES:
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
