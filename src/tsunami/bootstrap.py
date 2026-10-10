"""Startup shared by the entry points: tsunami.exe, tsunami-mcp.exe and python -m tsunami.

Only entry points call these. Nothing under core/ selects the keyring backend or
touches real app-data folders on import (design.md, Packaging and installation).
"""

from __future__ import annotations

import sys
from importlib.resources import files

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

    keyring.set_keyring(WinVaultKeyring())


def selftest() -> int:
    """Load the models, data files and keyring backend. Returns an exit code."""
    import keyring
    from keyring.backends.Windows import WinVaultKeyring

    import tsunami.core.models.common  # noqa: F401
    import tsunami.core.models.jobs  # noqa: F401
    import tsunami.core.models.play  # noqa: F401

    data = files("tsunami.core.data")
    missing = [name for name in _REQUIRED_DATA if not data.joinpath(name).is_file()]
    if missing:
        print(f"selftest: missing data files: {', '.join(missing)}", file=sys.stderr)
        return 1

    configure_keyring()
    if not isinstance(keyring.get_keyring(), WinVaultKeyring):
        print("selftest: keyring backend is not WinVaultKeyring", file=sys.stderr)
        return 1

    print(f"selftest ok (tsunami {__version__})")
    return 0


def handle_common_flags(argv: list[str], prog: str) -> int | None:
    """Handle --version and --selftest for the exes. Returns an exit code if handled."""
    if "--version" in argv:
        print(f"{prog} {__version__}")
        return 0
    if "--selftest" in argv:
        return selftest()
    return None
