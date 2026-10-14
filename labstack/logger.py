"""Provide readable terminal logs and JSON logs for captured task output."""

import logging
import sys
from typing import Any

import structlog


def configure_logging(log_level: str = "info") -> None:
    """Send application progress to stderr and keep stdout for command results.

    Interactive terminals use a console renderer. Redirected commands use JSON,
    so build logs retain fields such as the node, image target and release.
    Guest output is saved separately and is never added as a log context field.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", level=level)
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(service_name="labstack")
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        # UTC timestamps keep saved logs comparable across machines and timezones.
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if sys.stderr.isatty():
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    else:
        processors.append(structlog.processors.JSONRenderer())
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


def get_logger(name: str) -> Any:
    """Attach a module name to events without configuring logging during imports."""
    return structlog.get_logger(module=name)
