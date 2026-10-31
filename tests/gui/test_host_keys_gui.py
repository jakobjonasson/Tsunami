"""The Trust host keys tab (gui/screens/host_keys.py) with a fake probe: no network."""

from __future__ import annotations

import paramiko
from PySide6.QtWidgets import QMessageBox

from tsunami.core.hostkey_trust import JUMP_HOST, KeyProbe, Outcome
from tsunami.core.issues import TsunamiError
from tsunami.gui.screens import host_keys
from tsunami.gui.screens.host_keys import HostKeysTab, open_trust_tab

KEY = paramiko.ECDSAKey.generate()


def fake_trust(outcomes: dict[str, Outcome], calls: list):
    def trust(paths, customer, devices, *, jump=None, cancel=None, on_result=None, **kw):
        calls.append({"customer": customer, "devices": [d.name for d in devices], "jump": jump})
        out = []
        for d in devices:
            outcome = outcomes.get(d.name, Outcome.KNOWN)
            result = KeyProbe(
                d.name,
                d.host,
                d.port,
                outcome,
                fingerprint="SHA256:new",
                known_fingerprint="SHA256:old" if outcome == Outcome.CHANGED else None,
                detail="host key differs" if outcome == Outcome.CHANGED else "",
                key=KEY if outcome == Outcome.CHANGED else None,
            )
            out.append(result)
            if on_result is not None:
                on_result(result)
        return out

    return trust


def _run(qtbot, tab: HostKeysTab) -> None:
    assert tab.start()
    qtbot.waitUntil(lambda: not tab.running, timeout=10000)


def test_results_fill_the_table_and_a_changed_key_needs_confirmation(
    qtbot, main_window, gui_ctx, monkeypatch
):
    calls: list = []
    monkeypatch.setattr(
        host_keys,
        "trust_host_keys",
        fake_trust({"sa-as01": Outcome.RECORDED, "sa-as02": Outcome.CHANGED}, calls),
    )
    accepted: list = []
    monkeypatch.setattr(host_keys, "accept_changed", lambda p, c, r: accepted.append(r.device))
    tab = open_trust_tab(gui_ctx, main_window, "acme")
    assert isinstance(tab, HostKeysTab)
    assert [tab.table.item(r, 0).text() for r in range(tab.table.rowCount())] == [
        "sa-as01",
        "sa-as02",
    ]
    _run(qtbot, tab)
    assert calls[0]["customer"] == "acme" and calls[0]["jump"] is None
    assert tab.table.item(0, 2).text() == "recorded"
    assert tab.table.item(1, 2).text() == "changed"
    fingerprints = tab.table.item(1, 3).text()
    assert "SHA256:old" in fingerprints and "SHA256:new" in fingerprints
    assert "1 recorded" in tab.progress.text() and "1 changed" in tab.progress.text()

    tab.table.selectRow(0)
    assert not tab.accept_button.isEnabled()  # only changed keys can be accepted
    tab.table.selectRow(1)
    assert tab.accept_button.isEnabled()

    asked: list[str] = []

    def answer(button):
        def question(parent, title, text, *a, **k):
            asked.append(text)
            return button

        return question

    monkeypatch.setattr(QMessageBox, "question", answer(QMessageBox.StandardButton.No))
    assert not tab.accept_selected()
    assert accepted == [] and "SHA256:old" in asked[0] and "SHA256:new" in asked[0]

    monkeypatch.setattr(QMessageBox, "question", answer(QMessageBox.StandardButton.Yes))
    assert tab.accept_selected()
    assert accepted == ["sa-as02"]
    assert tab.table.item(1, 2).text() == "recorded"
    assert tab.table.item(1, 4).text() == "new key accepted"


def test_a_site_tab_probes_only_that_sites_devices(qtbot, main_window, gui_ctx, monkeypatch):
    calls: list = []
    monkeypatch.setattr(host_keys, "trust_host_keys", fake_trust({}, calls))
    tab = open_trust_tab(gui_ctx, main_window, "acme", "site-a")
    _run(qtbot, tab)
    assert calls[0]["devices"] == ["sa-as01", "sa-as02"]  # both are at site-a
    empty = open_trust_tab(gui_ctx, main_window, "acme", "nowhere")
    assert empty.table.rowCount() == 0 and not empty.start_button.isEnabled()
    assert "No devices" in empty.progress.text()


def test_a_jump_host_whose_login_is_unavailable_stops_before_probing(
    qtbot, main_window, gui_ctx, monkeypatch
):
    calls: list = []
    monkeypatch.setattr(host_keys, "trust_host_keys", fake_trust({}, calls))
    tab = open_trust_tab(gui_ctx, main_window, "acme")

    def locked(customer):
        raise TsunamiError("Tsunami is locked")

    monkeypatch.setattr(gui_ctx, "jump_target", locked)
    warned: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text, *a: warned.append(text))
    assert not tab.start()
    assert calls == []
    assert warned and "jump host" in warned[0] and "locked" in warned[0]


def test_a_changed_jump_host_key_gets_its_own_row_to_accept(
    qtbot, main_window, gui_ctx, monkeypatch
):
    def trust(paths, customer, devices, *, on_result=None, **kw):
        results = [
            KeyProbe(
                JUMP_HOST,
                "198.51.100.10",
                22,
                Outcome.CHANGED,
                fingerprint="SHA256:new",
                known_fingerprint="SHA256:old",
                key=KEY,
            ),
            *(
                KeyProbe(d.name, d.host, d.port, Outcome.UNREACHABLE, detail="accept it")
                for d in devices
            ),
        ]
        for result in results:
            on_result(result)
        return results

    monkeypatch.setattr(host_keys, "trust_host_keys", trust)
    accepted: list = []
    monkeypatch.setattr(host_keys, "accept_changed", lambda p, c, r: accepted.append(r.host))
    tab = open_trust_tab(gui_ctx, main_window, "acme")
    _run(qtbot, tab)
    rows = [tab.table.item(r, 0).text() for r in range(tab.table.rowCount())]
    assert rows == [JUMP_HOST, "sa-as01", "sa-as02"]
    assert tab.table.item(0, 2).text() == "changed"
    assert tab.table.item(1, 2).text() == "unreachable"
    assert tab.progress.text().startswith("2 of 2")  # the jump host is not a device

    asked: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda parent, title, text, *a, **k: asked.append(text) or QMessageBox.StandardButton.Yes,
    )
    tab.table.selectRow(0)
    assert tab.accept_selected()
    assert accepted == ["198.51.100.10"]
    assert asked[0].startswith("The jump host (198.51.100.10)")
    assert "start again" in tab.table.item(0, 4).text()

    _run(qtbot, tab)  # starting again drops the old jump host row before the new results
    assert [tab.table.item(r, 0).text() for r in range(tab.table.rowCount())] == rows
