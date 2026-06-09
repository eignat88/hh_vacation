import csv
import os
import tempfile
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

import requests

import hh_requirements_export as exporter


class DummyResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text
        self.reason = "Forbidden" if status_code == 403 else "OK"
        self.url = "https://api.hh.ru/vacancies"
        self.request = requests.Request("GET", self.url).prepare()

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"{self.status_code} Client Error: {self.reason} for url: {self.url}",
                response=self,
                request=self.request,
            )


class DummySession:
    def __init__(self, response, headers=None):
        self.response = response
        self.headers = headers or {}
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        if isinstance(self.response, list):
            return self.response[min(self.calls - 1, len(self.response) - 1)]
        return self.response


class HeadHunterRequestTests(unittest.TestCase):
    def test_create_session_sets_hh_user_agent_and_optional_token(self):
        session = exporter.create_session(
            user_agent="MyApp/1.0 (me@example.com)",
            access_token="token-123",
        )

        self.assertEqual(session.headers["User-Agent"], "MyApp/1.0 (me@example.com)")
        self.assertEqual(session.headers["HH-User-Agent"], "MyApp/1.0 (me@example.com)")
        self.assertEqual(session.headers["Authorization"], "Bearer token-123")

    def test_create_session_reads_environment_defaults(self):
        with patch.dict(os.environ, {"HH_USER_AGENT": "EnvApp/2.0 (env@example.com)"}, clear=False):
            session = exporter.create_session()

        self.assertEqual(session.headers["User-Agent"], "EnvApp/2.0 (env@example.com)")
        self.assertEqual(session.headers["HH-User-Agent"], "EnvApp/2.0 (env@example.com)")

    def test_create_session_adds_authorization_from_environment_token(self):
        with patch.dict(os.environ, {"HH_ACCESS_TOKEN": "env-token-123"}, clear=False):
            session = exporter.create_session(
                user_agent="MyApp/1.0 (me@example.com)",
                access_token=None,
            )

        self.assertEqual(session.headers["Authorization"], "Bearer env-token-123")

    def test_create_session_omits_blank_authorization_header(self):
        with patch.dict(os.environ, {"HH_ACCESS_TOKEN": "   "}, clear=False):
            session = exporter.create_session(
                user_agent="MyApp/1.0 (me@example.com)",
                access_token=None,
            )

        self.assertNotIn("Authorization", session.headers)

    def test_validate_user_agent_requires_application_version_and_email(self):
        self.assertIsNone(exporter.validate_user_agent_value("MyApp/1.0 (me@example.com)"))
        self.assertIn("ApplicationName/Version", exporter.validate_user_agent_value("MyApp/1.0") or "")
        self.assertIn("ApplicationName/Version", exporter.validate_user_agent_value("MyApp (me@example.com)") or "")

    def test_validate_user_agent_rejects_missing_or_placeholder(self):
        self.assertIn("необходимо указать", exporter.validate_user_agent_value(None) or "")
        self.assertIn(
            "Встроенный User-Agent",
            exporter.validate_user_agent_value(exporter.DEFAULT_HH_USER_AGENT) or "",
        )

    def test_parse_retry_after_numeric_seconds(self):
        self.assertEqual(exporter.parse_retry_after("12", fallback=5), 12)

    def test_parse_retry_after_http_date(self):
        fixed_now = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
        retry_at = datetime(2026, 6, 9, 12, 0, 30, tzinfo=timezone.utc)

        class FixedDateTime:
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz is None else fixed_now.astimezone(tz)

        with patch.object(exporter, "datetime", FixedDateTime):
            self.assertEqual(exporter.parse_retry_after(format_datetime(retry_at, usegmt=True), fallback=5), 30)

    def test_parse_retry_after_invalid_value_falls_back(self):
        self.assertEqual(exporter.parse_retry_after("definitely not a retry date", fallback=7), 7)

    def test_request_json_raises_non_retryable_403_with_api_details(self):
        response = DummyResponse(
            status_code=403,
            payload={"errors": [{"type": "forbidden", "value": "captcha_required"}], "request_id": "rid-1"},
        )
        session = DummySession(
            response,
            headers={
                "User-Agent": "MyApp/1.0 (me@example.com)",
                "HH-User-Agent": "MyApp/1.0 (me@example.com)",
                "Authorization": "Bearer token-123",
            },
        )

        with self.assertLogs(level="ERROR") as logs:
            with self.assertRaises(requests.exceptions.HTTPError) as ctx:
                exporter.request_json(session, "https://api.hh.ru/vacancies")

        log_output = "\n".join(logs.output)
        self.assertEqual(session.calls, 1)
        self.assertIn("forbidden: captcha_required", str(ctx.exception))
        self.assertIn("request_id=rid-1", str(ctx.exception))
        self.assertIn("HTTP 403", log_output)
        self.assertIn("https://api.hh.ru/vacancies", log_output)
        self.assertIn('"request_id": "rid-1"', log_output)
        self.assertIn("request_id: rid-1", log_output)
        self.assertIn("User-Agent", log_output)
        self.assertIn("Bearer ***", log_output)
        self.assertNotIn("token-123", log_output)

    def test_log_api_error_response_logs_required_400_and_429_details(self):
        for status_code in (400, 429):
            with self.subTest(status_code=status_code):
                response = DummyResponse(
                    status_code=status_code,
                    payload={"description": "bad request", "request_id": f"rid-{status_code}"},
                    headers={"HH-Request-Id": f"rid-{status_code}"},
                    text=f"api body {status_code}",
                )
                session = DummySession(response, headers={"User-Agent": "MyApp/1.0 (me@example.com)"})

                with self.assertLogs(level="ERROR") as logs:
                    exporter.log_api_error_response(response, session)

                log_output = "\n".join(logs.output)
                self.assertIn(f"HTTP {status_code}", log_output)
                self.assertIn("https://api.hh.ru/vacancies", log_output)
                self.assertIn(f"api body {status_code}", log_output)
                self.assertIn(f"request_id: rid-{status_code}", log_output)
                self.assertIn("User-Agent", log_output)

    def test_request_json_retries_server_errors_without_sleep_after_last_attempt(self):
        response = DummyResponse(status_code=503, payload={"errors": [{"type": "service_unavailable"}]})
        session = DummySession(response)

        with patch.object(exporter.time, "sleep") as sleep_mock:
            with self.assertRaises(RuntimeError):
                exporter.request_json(session, "https://api.hh.ru/vacancies")

        self.assertEqual(session.calls, exporter.MAX_RETRIES)
        self.assertEqual(sleep_mock.call_count, exporter.MAX_RETRIES - 1)


class HeadHunterExtractionTests(unittest.TestCase):
    def test_clean_html_preserves_strong_headings_and_lists(self):
        html = """
        <p><strong>Требования:</strong></p>
        <ul><li>Опыт с Python</li><li>SQL</li></ul>
        <p><strong>Условия:</strong></p><p>Удаленка</p>
        """

        text = exporter.clean_html(html)

        self.assertIn("Требования:", text)
        self.assertIn("- Опыт с Python", text)
        self.assertIn("Условия:", text)

    def test_extract_section_handles_inline_heading_after_normalization(self):
        text = "Требования: Python и SQL\nОбязанности:\n- Разработка"

        self.assertEqual(exporter.extract_requirements(text), "Python и SQL")

    def test_extract_requirements_from_realistic_hh_html_variants(self):
        html = """
        <div>Мы ожидаем от кандидата:</div>
        <ul><li>Опыт аналитики от 2 лет</li><li>Знание SQL</li></ul>
        <p><strong>Будет плюсом:</strong> Python</p>
        <p><strong>Мы предлагаем:</strong></p><ul><li>ДМС</li></ul>
        """

        text = exporter.clean_html(html)
        requirements = exporter.extract_requirements(text)

        self.assertIn("- Опыт аналитики от 2 лет", requirements)
        self.assertIn("- Знание SQL", requirements)
        self.assertIn("Python", requirements)
        self.assertNotIn("ДМС", requirements)

    def test_extract_requirements_falls_back_to_unheaded_list(self):
        text = exporter.clean_html("<ul><li>Опыт продаж</li><li>Грамотная речь</li></ul><p>Условия:</p><p>Офис</p>")

        self.assertEqual(exporter.extract_requirements(text), "- Опыт продаж\n- Грамотная речь")

    def test_build_row_extracts_sections_and_normalizes_fields(self):
        search_item = {
            "id": "1",
            "name": "Аналитик",
            "snippet": {"requirement": "<highlighttext>SQL</highlighttext>", "responsibility": "Отчеты"},
            "employer": {"name": "ACME"},
        }
        details = {
            "id": "1",
            "description": "<p>Требования:</p><ul><li>SQL</li></ul><p>Обязанности:</p><ul><li>Отчеты</li></ul>",
            "salary": {"from": 100, "to": 200, "currency": "RUR", "gross": True},
            "key_skills": [{"name": "SQL"}, {"name": "Python"}],
            "area": {"name": "Москва"},
        }

        row = exporter.build_row(search_item, details, "аналитик", "2026-06-09T12:00:00+00:00")

        self.assertEqual(row["vacancy_id"], "1")
        self.assertEqual(row["salary_from"], 100)
        self.assertEqual(row["key_skills"], "SQL, Python")
        self.assertEqual(row["requirements_from_description"], "- SQL")
        self.assertEqual(row["responsibilities_from_description"], "- Отчеты")
        self.assertEqual(row["source_query"], "аналитик")


class HeadHunterExporterTests(unittest.TestCase):
    def test_save_outputs_writes_csv_headers_for_empty_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "vacancies.xlsx"

            with self.assertLogs(level="WARNING") as logs:
                created = exporter.save_outputs([], out, "csv")

            self.assertEqual(created, [out.with_suffix(".csv")])
            self.assertIn("только заголовки", "\n".join(logs.output))
            with created[0].open(encoding="utf-8-sig", newline="") as file_obj:
                reader = csv.reader(file_obj)
                self.assertEqual(next(reader), exporter.OUTPUT_COLUMNS)

    def test_save_outputs_writes_both_formats(self):
        row = {column: "" for column in exporter.OUTPUT_COLUMNS}
        row["vacancy_id"] = "1"
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "vacancies"

            created = exporter.save_outputs([row], out, "both")

            self.assertEqual(set(created), {out.with_suffix(".xlsx"), out.with_suffix(".csv")})
            for path in created:
                self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
