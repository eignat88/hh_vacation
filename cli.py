"""Command-line interface for hh requirements export."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import requests

from api import (
    DEFAULT_AREA,
    DEFAULT_DELAY,
    DEFAULT_PAGES,
    DEFAULT_PER_PAGE,
    HH_ACCESS_TOKEN_ENV,
    HH_USER_AGENT_ENV,
    create_session,
    get_vacancy_details,
    search_vacancies,
    validate_user_agent_value,
)
from exporters import save_outputs
from extractors import build_row

if find_spec("tqdm"):
    tqdm = import_module("tqdm").tqdm
else:
    def tqdm(iterable, **_kwargs):
        """Minimal fallback if tqdm is not installed yet."""
        return iterable

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
        if not vacancy_id:
            skipped += 1
            logging.warning("Вакансия без id пропущена до запроса деталей: %s", vacancy_name or item)
            continue

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
