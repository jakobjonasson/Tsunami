"""Rotating, redacting log file (design.md, Local data and audit: Retention)."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

import pytest

from tsunami.core.logsetup import RedactingFormatter, configure_logging, set_redactor
from tsunami.core.redact import Redactor


@pytest.fixture
def log_file(tmp_path: Path):
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    yield tmp_path / "logs" / "app.log"
    for h in list(root.handlers):
        if h not in before:
            root.removeHandler(h)
            h.close()
    root.setLevel(level)


def test_pattern_and_exact_values_redacted(log_file: Path):
    handler = configure_logging(log_file, redactor=Redactor(exact_values=["hunter2-value"]))
    log = logging.getLogger("tsunami.test")
    log.info("sent: enable secret 9 $9$abc")
    log.info("password is hunter2-value ok")
    handler.flush()
    text = log_file.read_text(encoding="utf-8")
    assert "$9$abc" not in text
    assert "hunter2-value" not in text
    assert "<redacted>" in text


def test_exception_traceback_redacted(log_file: Path):
    handler = configure_logging(log_file, redactor=Redactor(exact_values=["s3cr3t-token"]))
    log = logging.getLogger("tsunami.test")
    try:
        raise RuntimeError("failed sending s3cr3t-token to device")
    except RuntimeError:
        log.exception("job failed")
    handler.flush()
    text = log_file.read_text(encoding="utf-8")
    assert "Traceback" in text and "RuntimeError" in text
    assert "s3cr3t-token" not in text


def test_set_redactor_updates_handlers(log_file: Path):
    handler = configure_logging(log_file)
    log = logging.getLogger("tsunami.test")
    log.info("before newvalue-123")
    set_redactor(Redactor(exact_values=["newvalue-123"]))
    log.info("after newvalue-123")
    handler.flush()
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert "newvalue-123" in lines[0]
    assert "newvalue-123" not in lines[1]


def test_rotation_settings_and_level(log_file: Path):
    handler = configure_logging(log_file, level="DEBUG")
    assert isinstance(handler, logging.handlers.RotatingFileHandler)
    assert handler.maxBytes == 10 * 1024 * 1024
    assert handler.backupCount == 5
    assert handler.encoding == "utf-8"
    assert isinstance(handler.formatter, RedactingFormatter)
    assert handler in logging.getLogger().handlers
    assert logging.getLogger().level == logging.DEBUG


def test_no_stdout_handler(log_file: Path):
    configure_logging(log_file)
    for h in logging.getLogger().handlers:
        stream = getattr(h, "stream", None)
        assert stream is not sys.stdout
        assert stream is not sys.__stdout__
