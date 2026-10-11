"""Rotating logs (10 MB x 5). Every line passes through the redactor.

Stdout belongs to the MCP protocol, so only a file handler is ever installed.
See design.md, Local data and audit: Retention.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from tsunami.core.redact import Redactor

MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class RedactingFormatter(logging.Formatter):
    """Formats, then redacts the whole result, exception traceback included."""

    def __init__(self, redactor: Redactor | None = None) -> None:
        super().__init__(_FORMAT)
        self.redactor = redactor or Redactor()

    def format(self, record: logging.LogRecord) -> str:
        return self.redactor.text(super().format(record))


def configure_logging(
    log_file: Path, *, level: str = "INFO", redactor: Redactor | None = None
) -> logging.Handler:
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(RedactingFormatter(redactor))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(level.upper())
    return handler


def set_redactor(redactor: Redactor) -> None:
    """Swap in a redactor with new exact values (at job start) on every installed handler."""
    handlers = list(logging.getLogger().handlers)
    for logger in logging.Logger.manager.loggerDict.values():
        if isinstance(logger, logging.Logger):
            handlers.extend(logger.handlers)
    for handler in handlers:
        if isinstance(handler.formatter, RedactingFormatter):
            handler.formatter.redactor = redactor
