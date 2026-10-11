"""The window stays responsive while a 200-device run streams events (plan 4.4).

The limits catch UI-thread work that grows with the device count, which shows as stalls
of seconds or as a slow event loop throughout. They allow for scheduling noise: mock
devices are CPU-bound, so on a 2-vCPU CI runner fifty worker threads compete with the UI
thread for the GIL and single stalls of about 0.7 s occur. Locally the longest is ~0.06 s.
Real devices spend their time waiting on the network, which releases the GIL.
"""

from __future__ import annotations

import time

import pytest

from tsunami.core.models.jobs import DeviceStatus, JobStatus
from tsunami.core.models.play import RunMode
from tsunami.core.runner import RunRequest
from tsunami.core.transports.mock import MockTransport

DEVICES = [f"sa-as{n:03d}" for n in range(1, 201)]
MAX_GAP_SECONDS = 2.0  # a freeze: the event loop never stalls this long
P95_GAP_SECONDS = 0.25  # sluggishness: nineteen ticks in twenty arrive within this


@pytest.fixture
def workspace_root(workspace_root):
    lines = []
    for n, name in enumerate(DEVICES, start=1):
        lines += [
            f"- name: {name}",
            f"  host: 203.0.113.{n % 250 + 1}",
            "  site: site-a",
            "  role: access",
            "  platform: ios-xe",
        ]
    devices = workspace_root / "inventory/customers/acme/devices.yml"
    devices.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return workspace_root


@pytest.fixture
def mock_transport() -> MockTransport:
    mock = MockTransport(latency=(0.002, 0.01), seed=7)
    for name in DEVICES:
        state = mock.add_device(name, running=[f"hostname {name}"])
        state.exec_responses["re:^show vlan id"] = "30   Cameras   active"
    return mock


def test_two_hundred_device_run_keeps_the_ui_responsive(main_window, gui_ctx, qtbot, monkeypatch):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    ticks: list[float] = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
    timer.start(10)

    request = RunRequest(
        "plays/push/add-vlan.yml", RunMode.APPLY, prompts={"vlan_id": "30", "vlan_name": "Cameras"}
    )
    thread = main_window.start_run(request, "Add VLAN")
    finished: list[str] = []
    thread.job_finished.connect(finished.append)
    qtbot.waitUntil(lambda: bool(finished), timeout=180_000)
    timer.stop()

    job = gui_ctx.history.get_job(finished[0])
    assert job.status == JobStatus.COMPLETED
    devices = gui_ctx.history.devices(job.id)
    assert len(devices) == 200 and {d.status for d in devices} == {DeviceStatus.SUCCESS}

    gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
    assert len(ticks) > 20
    p95 = sorted(gaps)[int(len(gaps) * 0.95)]
    stats = f"max {max(gaps):.2f}s, p95 {p95:.3f}s over {len(gaps)} ticks"
    assert max(gaps) < MAX_GAP_SECONDS, f"event loop froze: {stats}"
    assert p95 < P95_GAP_SECONDS, f"event loop sluggish: {stats}"
