"""Structured logging setup.

Two sinks:

  * console: concise, level-filtered (INFO+)
  * rotating file: JSON records (ts, level, logger, module, message)

Nothing sensitive is ever logged: pass in a redacted fingerprint
(see ``Settings.fingerprint``) as extra context instead of raw settings.
"""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


class JsonFormatter(logging.Formatter):
    """Minimal JSON formatter; appends the record message verbatim."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "message": record.getMessage(),
        }
        extra = getattr(record, "_structured", {})
        if extra:
            payload["context"] = extra
        return json.dumps(payload, ensure_ascii=False, default=str)


class StructuredLoggerAdapter(logging.LoggerAdapter):
    """Attach static context (e.g. a redacted fingerprint) to every record."""

    def process(self, msg, kwargs):
        extra = kwargs.get("extra") or {}
        structured = dict(self.extra)
        if isinstance(extra.get("_structured"), dict):
            structured.update(extra["_structured"])
        extra["_structured"] = structured
        kwargs["extra"] = extra
        return msg, kwargs


def setup_logging(
    level: str = "INFO",
    log_dir: str | Path = "./logs",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
    context: Optional[dict] = None,
) -> StructuredLoggerAdapter:
    """Configure the root logger and return a context-aware adapter.

    Args:
        level: minimum root log level (e.g. INFO).
        log_dir: directory for the rotating file sink (created if absent).
        max_bytes: per-file size cap for the rotating handler.
        backup_count: how many rotated files to keep.
        context: static context added to every record's ``_structured`` field
            (use ``Settings.fingerprint()`` — never the password).
    """
    root = logging.getLogger("gateway")
    root.setLevel(level.upper())
    root.handlers.clear()
    root.propagate = False

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        log_dir / "gateway.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonFormatter())
    file_handler.setLevel(level.upper())
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
    )
    console_handler.setLevel(level.upper())
    root.addHandler(console_handler)

    return StructuredLoggerAdapter(root, context or {})