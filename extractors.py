"""HTML cleanup and vacancy field extraction helpers."""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from typing import Any

from bs4 import BeautifulSoup

REQUIREMENT_HEADINGS = [
    "Требования",
    "Наши требования",
    "Мы ожидаем",
    "Мы ожидаем от кандидата",
    "Что мы ожидаем",
    "Ожидания",
    "Что нужно знать",
    "Что важно",
    "Кого мы ищем",
    "Необходимые навыки",
    "Пожелания к кандидату",
    "Вы нам подходите, если",
    "Будет плюсом",
    "Плюсом будет",
    "Будет преимуществом",
    "Что нужно уметь",
]

RESPONSIBILITY_HEADINGS = [
    "Обязанности",
    "Ваши обязанности",
    "Что предстоит делать",
    "Задачи",
    "Чем предстоит заниматься",
    "Что нужно делать",
    "Ваши задачи",
    "Функционал",
]

CONDITION_HEADINGS = [
    "Условия",
    "Условия работы",
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


def clean_html(raw_html: str | None) -> str:
    """Convert vacancy HTML to readable plain text while preserving list structure."""
    if not raw_html:
        return ""

    soup = BeautifulSoup(raw_html, "html.parser")

    for br in soup.find_all("br"):
        br.replace_with("\n")

    for heading_tag in soup.find_all(["strong", "b"]):
        heading_text = heading_tag.get_text(" ", strip=True)
        if heading_text and is_known_heading_text(heading_text):
            heading_tag.insert_before("\n")
            heading_tag.insert_after("\n")

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


def strip_heading_prefix(line: str) -> str:
    """Remove bullet/list markers before matching a possible section heading."""
    return re.sub(r"^[\s\-–—•\d.)]+", "", line.strip())


def is_known_heading_text(text: str) -> bool:
    """Return True when a short HTML emphasis tag contains a known section heading."""
    all_headings = REQUIREMENT_HEADINGS + RESPONSIBILITY_HEADINGS + CONDITION_HEADINGS + COMMON_STOP_HEADINGS
    matched, _ = heading_matches(text, all_headings, allow_inline_remainder=False)
    return matched


def heading_matches(
    line: str,
    headings: Iterable[str],
    *,
    allow_inline_remainder: bool = True,
) -> tuple[bool, str]:
    """Return whether a line is a heading or starts with a heading plus a clear separator."""
    stripped_line = strip_heading_prefix(line)
    line_normalized = normalize_heading(stripped_line)

    for heading in sorted(headings, key=lambda value: len(normalize_heading(value)), reverse=True):
        heading_normalized = normalize_heading(heading)
        if line_normalized == heading_normalized:
            return True, ""
        if not line_normalized.startswith(heading_normalized):
            continue

        normalized_tail = line_normalized[len(heading_normalized) :]
        if normalized_tail and not normalized_tail[0].isspace() and normalized_tail[0] not in ":;.-–—":
            continue

        if not allow_inline_remainder:
            return True, ""

        separator_match = re.search(r"[:;.–—-]", stripped_line)
        if not separator_match:
            return True, ""

        remainder = stripped_line[separator_match.end() :].strip()
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


def extract_unheaded_leading_list(text: str) -> str:
    """Use the first bullet list as requirements when the description has no explicit headings."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    collected: list[str] = []
    started = False

    for line in lines:
        if is_stop_heading(line, RESPONSIBILITY_STOP_HEADINGS + CONDITION_STOP_HEADINGS):
            break
        if line.startswith("- "):
            started = True
            collected.append(line)
            continue
        if started:
            break

    return "\n".join(collected).strip()


def extract_requirements(text: str) -> str:
    """Extract candidate requirements from a cleaned vacancy description."""
    explicit_section = extract_section(text, REQUIREMENT_HEADINGS, REQUIREMENT_STOP_HEADINGS)
    if explicit_section:
        return explicit_section
    return extract_unheaded_leading_list(text)


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
