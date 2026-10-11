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
    from tsunami.gui import theme
    from tsunami.gui.context import AppContext
    from tsunami.gui.main_window import MainWindow
    from tsunami.gui.screens import load_views, register_all

    app = QApplication([sys.argv[0], *argv])
    app.setApplicationName("Tsunami")
    app.setOrganizationName("Tsunami")

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

    window = MainWindow(ctx, load_views())
    register_all(window)
    window.show()
    window.poller.start()
    if settings.workspace:
        ctx.open_workspace(settings.workspace)
    try:
        return app.exec()
    finally:
        history.close()


def install_default_policy(paths) -> None:
    """Copy the MCP policy template on first run; it ships with enabled: false."""
    if paths.policy_file.exists():
        return
    paths.policy_file.parent.mkdir(parents=True, exist_ok=True)
    template = files("tsunami.core.data").joinpath("default-mcp-policy.yml").read_text("utf-8")
    paths.policy_file.write_text(template, encoding="utf-8", newline="\n")
