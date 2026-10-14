"""Startup shared by the entry points: tsunami.exe, tsunami-mcp.exe and python -m tsunami.

Only entry points call these. Nothing under core/ selects the keyring backend or
touches real app-data folders on import (design.md, Packaging and installation).
"""

from __future__ import annotations

import sys
from importlib.resources import files
from pathlib import Path

from tsunami import __version__

# Data files that must ship inside the package, including in the frozen build.
_REQUIRED_DATA = ("hardware.yml", "default-mcp-policy.yml")


def configure_keyring() -> None:
    """Select the Windows Credential Manager backend explicitly.

    keyring finds backends through package entry points, and frozen builds often lose
    that metadata, which fails at runtime.
    """
    import keyring
    from keyring.backends.Windows import WinVaultKeyring

    backend = WinVaultKeyring()
    # keyring's default is enterprise persistence, which roams with a domain profile.
    backend.persist = "local machine"
    keyring.set_keyring(backend)


def migrate_credentials(secrets: object) -> None:
    """Move logins from the old shared Credential Manager layout; a failure only logs."""
    import logging

    try:
        moved = secrets.migrate_legacy()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - never stop the app over this
        logging.getLogger(__name__).exception("credential store: migration failed")
        return
    if moved:
        logging.getLogger(__name__).info("credential store: moved %d entries", moved)


def selftest() -> int:
    """Load the models, data files and keyring backend. Returns an exit code."""
    import keyring
    from keyring.backends.Windows import WinVaultKeyring

    import tsunami.core.models.inventory  # noqa: F401
    import tsunami.core.models.jobs  # noqa: F401
    import tsunami.core.models.play  # noqa: F401
    import tsunami.core.models.workspace  # noqa: F401
    from tsunami.core.catalog import load_catalog
    from tsunami.core.issues import TsunamiError

    data = files("tsunami.core.data")
    missing = [name for name in _REQUIRED_DATA if not data.joinpath(name).is_file()]
    if missing:
        print(f"selftest: missing data files: {', '.join(missing)}", file=sys.stderr)
        return 1
    try:
        load_catalog()
    except TsunamiError as exc:
        print(f"selftest: hardware catalog: {exc}", file=sys.stderr)
        return 1

    configure_keyring()
    if not isinstance(keyring.get_keyring(), WinVaultKeyring):
        print("selftest: keyring backend is not WinVaultKeyring", file=sys.stderr)
        return 1

    print(f"selftest ok (tsunami {__version__})")
    return 0


def register_claude_desktop_flag() -> int:
    """Register the tsunami-mcp.exe next to this exe. Used by the installer's last page.

    The windowed tsunami.exe has no console, so the result also goes to a message box there.
    """
    from tsunami.core.issues import TsunamiError
    from tsunami.core.registration import register_claude_desktop_all

    exe = Path(sys.executable).parent / "tsunami-mcp.exe"
    try:
        if not exe.is_file():
            raise TsunamiError(f"tsunami-mcp.exe not found next to {Path(sys.executable).name}")
        written = register_claude_desktop_all(exe)
    except (TsunamiError, OSError) as exc:
        _report(f"Could not register with Claude Desktop: {exc}", error=True)
        return 1
    files = "\n".join(str(p) for p in written)
    _report(
        f"Registered with Claude Desktop in:\n{files}\n\nQuit Claude Desktop from the "
        "system tray and reopen it to load the Tsunami tools."
    )
    return 0


def _report(message: str, *, error: bool = False) -> None:
    if sys.stdout is not None and sys.stderr is not None:
        print(message, file=sys.stderr if error else sys.stdout)
        return
    try:  # windowed exe: no console, so show a message box
        import ctypes

        flags = 0x10 if error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(None, message, "Tsunami", flags)
    except (AttributeError, OSError):
        pass


def handle_common_flags(argv: list[str], prog: str) -> int | None:
    """Handle --version, --selftest and --register-claude-desktop. Returns an exit code if
    handled."""
    if "--version" in argv:
        print(f"{prog} {__version__}")
        return 0
    if "--selftest" in argv:
        return selftest()
    if "--register-claude-desktop" in argv:
        return register_claude_desktop_flag()
    return None
