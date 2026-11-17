"""Structured logging.

JSON lines on stdout are parsed by Cloud Logging into jsonPayload, so every key here
becomes filterable (e.g. jsonPayload.event="module_sync_failed") without a logging
client library or extra IAM roles.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("log_context", default={})


def bind_context(**fields: Any) -> None:
    """Attach fields (run_id, module) to every subsequent log line in this context."""
    current = dict(_context.get())
    for key, value in fields.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    _context.set(current)


def get_context() -> dict[str, Any]:
    return dict(_context.get())


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "logger": record.name,
        }
        ctx = _context.get()
        payload.update(ctx)
        fields = getattr(record, "fields", None)
        if fields:
            payload.update(fields)
        if "run_id" in ctx:
            # Labels are indexed by Cloud Logging, which makes "show me this run" a cheap filter.
            payload["logging.googleapis.com/labels"] = {"run_id": str(ctx["run_id"])}
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-friendly format for local runs and the live demo."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, timezone.utc).strftime("%H:%M:%S")
        fields = dict(_context.get())
        fields.update(getattr(record, "fields", None) or {})
        fields.pop("event", None)
        kv = " ".join(f"{k}={_short(v)}" for k, v in fields.items())
        line = f"{ts} {record.levelname:<7} {record.getMessage():<28} {kv}".rstrip()
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def _short(value: Any) -> str:
    text = json.dumps(value, default=str, ensure_ascii=False) if not isinstance(value, str) else value
    return text if len(text) <= 160 else text[:157] + "..."


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(TextFormatter() if fmt == "text" else JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Third-party libraries are noisy at INFO and could log request URLs.
    for noisy in ("urllib3", "google", "google.auth"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, exc_info: bool = False, **fields: Any) -> None:
    logger.log(level, event, extra={"fields": {"event": event, **fields}}, exc_info=exc_info)
