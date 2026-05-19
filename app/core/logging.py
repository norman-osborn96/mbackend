"""JSON-structured logging with request and user context."""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any, Optional

from pythonjsonlogger import jsonlogger

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
user_id_var: ContextVar[str] = ContextVar("user_id", default="-")


class MailPulseJsonFormatter(jsonlogger.JsonFormatter):
    """Ensures every log line includes timestamp, level, service_name, request_id, user_id."""

    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        log_record.setdefault("timestamp", self.formatTime(record, self.datefmt))
        log_record.setdefault("level", record.levelname)
        log_record.setdefault(
            "service_name",
            getattr(record, "service_name", record.name),
        )
        log_record.setdefault("request_id", request_id_var.get("-"))
        log_record.setdefault("user_id", user_id_var.get("-"))
        log_record.setdefault("message", record.getMessage())


_root_logger: Optional[logging.Logger] = None


def configure_logging(level: str = "INFO") -> None:
    """Idempotent setup of the mailpulse JSON logger."""
    global _root_logger
    if _root_logger is not None:
        return

    root = logging.getLogger("mailpulse")
    root.setLevel(level.upper())
    handler = logging.StreamHandler(sys.stdout)
    formatter = MailPulseJsonFormatter(
        "%(message)s",
        json_ensure_ascii=False,
    )
    handler.setFormatter(formatter)
    root.handlers.clear()
    root.addHandler(handler)
    root.propagate = False
    _root_logger = root


def get_logger(name: str, *, service_name: Optional[str] = None) -> logging.Logger:
    """Return a child logger; optional service_name is attached via LoggerAdapter."""

    configure_logging()
    child = logging.getLogger("mailpulse").getChild(name)
    if service_name:
        return logging.LoggerAdapter(child, {"service_name": service_name})  # type: ignore[return-value]
    return child


def set_request_context(*, request_id: str, user_id: Optional[str] = None) -> None:
    request_id_var.set(request_id or "-")
    user_id_var.set(user_id or "-")
