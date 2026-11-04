"""The device CSV report: inventory, job and contact status, and backups."""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

import pytest

from tsunami.core.device_report import COLUMNS, ReportScope, report_rows, to_csv, write_report
from tsunami.core.history import History
from tsunami.core.issues import TsunamiError
from tsunami.core.models.jobs import Actor, DeviceStatus
from tsunami.core.workspace import load_workspace

FIXTURE = Path(__file__).parents[1] / "fixtures" / "workspace"


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    shutil.copytree(FIXTURE, root)
    return load_workspace(root)


@pytest.fixture
def hist(tmp_path):
    h = History(tmp_path / "history.db")
    yield h
    h.close()


def _backup(paths, device: str, name: str) -> None:
    folder = paths.backups_dir / "acme" / device
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text("hostname x\n", encoding="utf-8")


def test_rows_join_inventory_history_and_backups(ws, hist, app_paths):
    job = hist.create_job(
        actor=Actor.GUI, play_path="Drift check", play_hash="", customer="acme", mode="drift"
    )
    hist.add_devices(job, ["sa-as01"])
    hist.update_device(job, "sa-as01", status=DeviceStatus.SUCCESS, finished_at=hist._clock())
    hist.record_device_facts("acme", "sa-as01", model="C9300-24P", version="17.9.4")
    _backup(app_paths, "sa-as01", "20261001T100000Z_discovery.cfg")
    _backup(app_paths, "sa-as01", "20261003T120000Z_drift.cfg")

    rows = {r["device"]: r for r in report_rows(ws, hist, app_paths, ReportScope("acme"))}
    assert list(rows) == ["sa-as01", "sa-as02"]
    one = rows["sa-as01"]
    assert one["site"] == "site-a" and one["host"] == "203.0.113.11"
    assert one["login"] == "acme-netadmin" and one["jump_host"] == ""
    assert (one["model"], one["version"]) == ("C9300-24P", "17.9.4")
    assert (one["last_job"], one["last_job_status"]) == ("Drift check", "success")
    assert one["last_job_at"] and one["last_contact"] == one["last_job_at"]
    assert (one["backups"], one["newest_backup_at"]) == (2, "2026-10-03 12:00:00Z")
    assert one["newest_backup_by"] == "Drift check"
    two = rows["sa-as02"]
    assert two["backups"] == 0 and two["newest_backup_at"] == two["last_contact"] == ""


def test_a_site_and_an_unknown_scope(ws, hist, app_paths):
    rows = report_rows(ws, hist, app_paths, ReportScope("acme", "site-a"))
    assert {r["site"] for r in rows} == {"site-a"}
    with pytest.raises(TsunamiError, match="no site"):
        report_rows(ws, hist, app_paths, ReportScope("acme", "nowhere"))
    with pytest.raises(TsunamiError, match="unknown customer"):
        report_rows(ws, hist, app_paths, ReportScope("nobody"))


def test_csv_has_every_column_and_no_formulas(ws, hist, app_paths, tmp_path):
    hist.record_device_facts("acme", "sa-as01", version='=HYPERLINK("http://x")')
    path = tmp_path / "out.csv"
    assert write_report(path, ws, hist, app_paths, ReportScope("acme")) == 2
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # Excel opens it as UTF-8
    rows = list(csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines()))
    assert tuple(rows[0]) == COLUMNS
    assert rows[0]["version"] == '\'=HYPERLINK("http://x")'


@pytest.mark.parametrize("value", ["=1+1", "+1", "-1", "@SUM(A1)", "\tx"])
def test_formula_starts_are_escaped(value):
    text = to_csv([{"device": value}])
    assert list(csv.DictReader(text.splitlines()))[0]["device"] == "'" + value
