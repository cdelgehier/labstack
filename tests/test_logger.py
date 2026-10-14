"""Check the actual log output used by terminal and captured commands."""

import json
import sys
from unittest.mock import Mock

import pytest
import structlog

from labstack.logger import configure_logging, get_logger


@pytest.fixture(autouse=True)
def reset_logging():
    """Keep log configuration from leaking into other test modules."""
    yield
    structlog.contextvars.clear_contextvars()
    structlog.reset_defaults()


def test_captured_logs_are_json_on_stderr(capsys, monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", Mock(return_value=False))
    configure_logging("info")

    get_logger("labstack.cluster").info("Starting machine.", node="cp")

    captured = capsys.readouterr()
    record = json.loads(captured.err)
    assert captured.out == ""
    assert record["event"] == "Starting machine."
    assert record["node"] == "cp"
    assert record["service_name"] == "labstack"
    assert record["module"] == "labstack.cluster"
    assert record["level"] == "info"
    assert record["timestamp"].endswith("Z")


def test_debug_messages_follow_the_configured_level(capsys):
    configure_logging("info")
    get_logger("test").debug("Hidden message.")
    assert capsys.readouterr().err == ""

    configure_logging("debug")
    get_logger("test").debug("Visible message.")
    assert json.loads(capsys.readouterr().err)["event"] == "Visible message."


def test_terminal_logs_are_readable_text(capsys, monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", Mock(return_value=True))
    configure_logging("info")

    get_logger("test").info("Starting machine.", node="cp")

    captured = capsys.readouterr()
    assert "Starting machine." in captured.err
    assert "cp" in captured.err
    assert not captured.err.startswith("{")
