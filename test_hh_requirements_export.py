import os
import unittest
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
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
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

    def test_validate_user_agent_requires_contact_email(self):
        self.assertIsNone(exporter.validate_user_agent_value("MyApp/1.0 (me@example.com)"))
        self.assertIn("контактную почту", exporter.validate_user_agent_value("MyApp/1.0") or "")

    def test_validate_user_agent_rejects_missing_or_placeholder(self):
        self.assertIn("необходимо указать", exporter.validate_user_agent_value(None) or "")
        self.assertIn(
            "Встроенный User-Agent",
            exporter.validate_user_agent_value(exporter.DEFAULT_HH_USER_AGENT) or "",
        )

    def test_request_json_raises_non_retryable_403_with_api_details(self):
        response = DummyResponse(
            status_code=403,
            payload={"errors": [{"type": "forbidden", "value": "captcha_required"}], "request_id": "rid-1"},
        )
        session = DummySession(response)

        with self.assertRaises(requests.exceptions.HTTPError) as ctx:
            exporter.request_json(session, "https://api.hh.ru/vacancies")

        self.assertEqual(session.calls, 1)
        self.assertIn("forbidden: captcha_required", str(ctx.exception))
        self.assertIn("request_id=rid-1", str(ctx.exception))

    def test_request_json_retries_server_errors(self):
        response = DummyResponse(status_code=503, payload={"errors": [{"type": "service_unavailable"}]})
        session = DummySession(response)

        with patch.object(exporter.time, "sleep"):
            with self.assertRaises(RuntimeError):
                exporter.request_json(session, "https://api.hh.ru/vacancies")

        self.assertEqual(session.calls, exporter.MAX_RETRIES)


if __name__ == "__main__":
    unittest.main()
