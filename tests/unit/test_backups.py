"""Browsing and comparing config backups: listing, path guard, redaction before display."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tsunami.core.backups import (
    BackupError,
    backup_folder,
    diff_redacted,
    list_backups,
    parse_name,
    read_redacted,
)
from tsunami.core.redact import Redactor

LOGIN_PASSWORD = "Login-Pass-1234"
ENABLE_SECRET = "Enable-Secret-4321"
TACACS_KEY = "Real-Tacacs-Key-77"
SNMP_COMMUNITY = "Snmp-Comm-9988"
NEW_ENABLE_SECRET = "Rotated-Enable-5555"
CLEAR = (LOGIN_PASSWORD, ENABLE_SECRET, TACACS_KEY, SNMP_COMMUNITY, NEW_ENABLE_SECRET)


def _config(ntp: str, enable: str) -> str:
    return (
        "hostname sa-as01\n"
        f"enable secret {enable}\n"
        f"username netadmin secret {LOGIN_PASSWORD}\n"
        f"snmp-server community {SNMP_COMMUNITY} RO\n"
        "tacacs server T1\n"
        " address ipv4 192.0.2.10\n"
        f" key {TACACS_KEY}\n"
        f"! note: the shared key is {TACACS_KEY}\n"
        f"ntp server {ntp}\n"
        "end\n"
    )


def _write(paths, stamp: str, job: str, text: str, customer="acme", device="sa-as01") -> Path:
    folder = paths.backups_dir / customer / device
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stamp}_{job}.cfg"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


@pytest.fixture
def redactor() -> Redactor:
    # Only the stored customer secret and login are known exactly; the enable secret and
    # SNMP community must be caught by the line rules alone.
    return Redactor([TACACS_KEY, LOGIN_PASSWORD])


def test_list_is_newest_first_and_ignores_other_files(app_paths):
    old = _write(app_paths, "20260101T010101Z", "job-aaaa1111", "a")
    new = _write(app_paths, "20260301T020202Z", "job-bbbb2222", "bbbb")
    mid = _write(app_paths, "20260201T030303Z", "job-cccc3333", "cc")
    folder = old.parent
    (folder / "notes.txt").write_text("x")
    (folder / "20260101T010101Z.cfg").write_text("no job id")
    (folder / "2026-01-01_job.cfg").write_text("bad stamp")
    (folder / "20261399T010101Z_job.cfg").write_text("impossible date")
    (folder / "20260101T010101Z_job.cfg.bak").write_text("wrong suffix")
    (folder / "20260401T010101Z_dir.cfg").mkdir()  # a folder, not a file

    entries = list_backups(app_paths, "acme", "sa-as01")

    assert [e.path for e in entries] == [new, mid, old]
    assert [e.job_id for e in entries] == ["job-bbbb2222", "job-cccc3333", "job-aaaa1111"]
    assert entries[0].taken_at == datetime(2026, 3, 1, 2, 2, 2, tzinfo=UTC)
    assert [e.size for e in entries] == [4, 2, 1]


def test_list_is_empty_for_a_device_with_no_backups(app_paths):
    assert list_backups(app_paths, "acme", "sa-as01") == []


def test_parse_name():
    assert parse_name("20260101T010203Z_abc-123.cfg") == (
        datetime(2026, 1, 1, 1, 2, 3, tzinfo=UTC),
        "abc-123",
    )
    assert parse_name("20260101T010203Z_.cfg") is None
    assert parse_name("../20260101T010203Z_abc.cfg") is None
    assert parse_name("20260101T010203Z_a/b.cfg") is None


@pytest.mark.parametrize(
    ("customer", "device"),
    [
        ("..", "sa-as01"),
        ("acme", ".."),
        ("../acme", "sa-as01"),
        ("acme", "../../x"),
        ("acme", "sa-as01/.."),
        ("acme\\x", "sa-as01"),
        ("Acme", "sa-as01"),
        ("acme", "SA_AS01"),
        ("", "sa-as01"),
        ("acme", ""),
        ("acme", "c:"),
    ],
)
def test_bad_customer_or_device_is_refused(app_paths, customer, device):
    with pytest.raises(BackupError):
        backup_folder(app_paths, customer, device)
    with pytest.raises(BackupError):
        list_backups(app_paths, customer, device)


def test_folder_stays_under_backups_dir(app_paths):
    folder = backup_folder(app_paths, "acme", "sa-as01")
    assert folder.is_relative_to(app_paths.backups_dir.resolve())


def test_read_refuses_paths_outside_the_backups_folder(app_paths, tmp_path, redactor):
    inside = _write(app_paths, "20260101T010101Z", "job1", _config("192.0.2.99", ENABLE_SECRET))
    outside = tmp_path / "20260101T010101Z_job1.cfg"
    outside.write_text(_config("192.0.2.99", ENABLE_SECRET), encoding="utf-8")
    root = app_paths.backups_dir
    assert read_redacted(inside, redactor, root=root)
    with pytest.raises(BackupError):
        read_redacted(outside, redactor, root=root)
    with pytest.raises(BackupError):
        read_redacted(root / ".." / outside.name, redactor, root=root)
    with pytest.raises(BackupError):  # not a backup name, even inside the folder
        other = inside.parent / "other.txt"
        other.write_text("secret")
        read_redacted(other, redactor, root=root)
    with pytest.raises(BackupError):
        read_redacted(inside, None, root=root)  # type: ignore[arg-type]


def test_read_never_returns_clear_secrets(app_paths, redactor):
    path = _write(app_paths, "20260101T010101Z", "job1", _config("192.0.2.99", ENABLE_SECRET))
    assert ENABLE_SECRET in path.read_text(encoding="utf-8")  # the file really is unredacted

    text = read_redacted(path, redactor, root=app_paths.backups_dir)

    for clear in CLEAR:
        assert clear not in text
    assert "<redacted>" in text
    assert "hostname sa-as01" in text
    assert "ntp server 192.0.2.99" in text
    assert "tacacs server T1" in text  # structure is kept, only the values go


def test_diff_shows_changes_without_secrets(app_paths, redactor):
    old = _write(app_paths, "20260101T010101Z", "job1", _config("192.0.2.99", ENABLE_SECRET))
    new = _write(app_paths, "20260201T010101Z", "job2", _config("192.0.2.100", NEW_ENABLE_SECRET))

    diff = diff_redacted(old, new, redactor, root=app_paths.backups_dir)

    assert "- ntp server 192.0.2.99" in diff.split("\n")
    assert "+ ntp server 192.0.2.100" in diff.split("\n")
    assert "  end" in diff.split("\n")  # unchanged context keeps a two-space marker
    assert diff.startswith(f"--- {old.name}\n+++ {new.name}\n@@")
    for clear in CLEAR:
        assert clear not in diff
    # A rotated secret is masked on both sides, so it is not a visible difference.
    assert "enable" not in diff


def test_identical_backups_give_an_empty_diff(app_paths, redactor):
    a = _write(app_paths, "20260101T010101Z", "job1", _config("192.0.2.99", ENABLE_SECRET))
    b = _write(app_paths, "20260201T010101Z", "job2", _config("192.0.2.99", NEW_ENABLE_SECRET))
    assert diff_redacted(a, b, redactor) == ""


def test_secret_removed_or_added_by_exact_value_is_masked_in_diff(app_paths):
    # A value only the exact layer knows (a comment, no rule matches): masked on both sides.
    old = _write(app_paths, "20260101T010101Z", "job1", "! token Tok-Exact-0001\nntp server a\n")
    new = _write(app_paths, "20260201T010101Z", "job2", "! token Tok-Exact-0002\nntp server b\n")
    diff = diff_redacted(old, new, Redactor(["Tok-Exact-0001", "Tok-Exact-0002"]))
    assert "Tok-Exact" not in diff
    assert "+ ntp server b" in diff
