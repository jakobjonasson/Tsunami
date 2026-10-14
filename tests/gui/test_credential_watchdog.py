"""The main window's credential watchdog (core/secret_store.py check_missing)."""

from __future__ import annotations

import logging

from tsunami.core.secret_store import KeyringSecretStore, LoginCredential


def test_a_login_removed_behind_tsunamis_back_is_reported(main_window, gui_ctx, tmp_path, caplog):
    assert main_window.check_credentials() == []  # the window recorded what was there
    other = KeyringSecretStore(index_path=tmp_path / "credential-index.json")
    other.delete_login("acme-netadmin")  # not through this Tsunami
    with caplog.at_level(logging.WARNING, logger="tsunami.gui.main_window"):
        assert main_window.check_credentials() == ["cred/acme-netadmin"]
    message = main_window.statusBar().currentMessage()
    assert "Login acme-netadmin disappeared from Windows Credential Manager" in message
    assert "Tsunami did not delete it" in message
    assert "acme-netadmin disappeared" in caplog.text
    assert main_window.check_credentials() == []  # reported once


def test_tsunamis_own_changes_are_not_reported(main_window, gui_ctx):
    main_window.check_credentials()
    gui_ctx.secrets.set_login("lab2", LoginCredential("u", "p"))
    gui_ctx.secrets.delete_login("acme-netadmin")
    assert main_window.check_credentials() == []
