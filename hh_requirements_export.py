#!/usr/bin/env python3
"""Backward-compatible entry point for hh.ru vacancy requirements export."""

from __future__ import annotations

import api as _api
from api import *  # noqa: F403
from api import datetime as datetime
from cli import complete_interactive_args as complete_interactive_args  # noqa: F401
from cli import configure_logging as configure_logging  # noqa: F401
from cli import main as main
from cli import make_default_output_path as make_default_output_path  # noqa: F401
from cli import parse_args as parse_args  # noqa: F401
from cli import validate_args as validate_args  # noqa: F401
from exporters import *  # noqa: F403
from extractors import *  # noqa: F403


def parse_retry_after(value: str | None, fallback: int) -> int:
    """Compatibility wrapper that keeps old tests patching this module's datetime working."""
    original_datetime = _api.datetime
    _api.datetime = datetime
    try:
        return _api.parse_retry_after(value, fallback)
    finally:
        _api.datetime = original_datetime


if __name__ == "__main__":
    raise SystemExit(main())
