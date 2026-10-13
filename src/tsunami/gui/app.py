"""tsunami.exe entry point (windowed).

See design.md, GUI; Packaging and installation: Build.
"""

from __future__ import annotations

import sys
from importlib.resources import files

from tsunami import bootstrap


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    code = bootstrap.handle_common_flags(argv, "tsunami")
    if code is not None:
        return code
    bootstrap.configure_keyring()

    from PySide6.QtWidgets import QApplication, QMessageBox

    from tsunami.core.history import History
    from tsunami.core.issues import TsunamiError
    from tsunami.core.logsetup import configure_logging
    from tsunami.core.models.settings import AppSettings, load_settings
    from tsunami.core.paths import AppPaths
    from tsunami.core.secret_store import KeyringSecretStore
    from tsunami.gui import icons, theme
    from tsunami.gui.context import AppContext
    from tsunami.gui.lock import LockManager
    from tsunami.gui.main_window import MainWindow
    from tsunami.gui.screens import load_views, register_all

    app = QApplication([sys.argv[0], *argv])
    app.setApplicationName("Tsunami")
    app.setOrganizationName("Tsunami")
    app.setWindowIcon(icons.app_icon())  # title bars; the exe resource is not used by Qt

    paths = AppPaths.from_env()
    try:
        settings = load_settings(paths.settings_file)
    except TsunamiError as exc:
        QMessageBox.warning(None, "Tsunami", f"{exc}\n\nStarting with default settings.")
        settings = AppSettings()
    configure_logging(paths.logs_dir / "app.log", level=settings.log_level)
    install_default_policy(paths)
    history = History(paths.history_db)
    history.mark_interrupted()
    secrets = KeyringSecretStore(index_path=paths.settings / "credential-index.json")

    def apply_theme() -> None:
        theme.apply(app, theme.resolve(ctx.settings.theme, app))

    ctx = AppContext(paths, settings, history, secrets)
    apply_theme()
    app.styleHints().colorSchemeChanged.connect(lambda _: apply_theme())
    ctx.settings_changed.connect(apply_theme)

    # The Tsunami password: set on first launch, else asked for before the window opens.
    lock = LockManager(paths, secrets, settings)
    if not lock.startup():
        history.close()
        return 0
    ctx.lock = lock
    ctx.settings_changed.connect(lambda: setattr(lock, "settings", ctx.settings))
    bridge = start_bridge(paths, history, secrets, lambda: lock.unlocked)

    window = MainWindow(ctx, load_views())
    register_all(window)
    window.show()
    window.poller.start()
    if settings.workspace:
        ctx.open_workspace(settings.workspace)
    try:
        return app.exec()
    finally:
        if bridge is not None:
            bridge.stop()
        history.close()


def start_bridge(paths, history, secrets, is_unlocked):
    """Serve MCP tool calls from tsunami-mcp.exe in this process, which alone holds the
    unlocked vault. None if another Tsunami window already serves them."""
    import logging

    from tsunami.mcp_server.bridge import BridgeServer
    from tsunami.mcp_server.enforcement import McpApp
    from tsunami.mcp_server.tools import Tools

    tools = Tools(McpApp(paths, history=history, secrets=secrets))
    bridge = BridgeServer(paths, tools.dispatch, is_unlocked=is_unlocked)
    try:
        bridge.start()
    except OSError:
        logging.getLogger(__name__).warning("MCP bridge not started", exc_info=True)
        return None
    return bridge


def install_default_policy(paths) -> None:
    """Copy the MCP policy template on first run; it ships with enabled: false."""
    if paths.policy_file.exists():
        return
    paths.policy_file.parent.mkdir(parents=True, exist_ok=True)
    template = files("tsunami.core.data").joinpath("default-mcp-policy.yml").read_text("utf-8")
    paths.policy_file.write_text(template, encoding="utf-8", newline="\n")
