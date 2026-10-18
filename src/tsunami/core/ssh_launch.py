"""Open an interactive SSH session to a device in the operator's own terminal.

MobaXterm is used when it is installed (`MobaXterm.exe -newtab "<command>"`, from
MobaXterm's documented command-line switches); otherwise Windows OpenSSH opens in a new
console window. Only the host, port and username are ever put on a command line. A password
is typed by the person at the terminal's prompt: Tsunami never passes one, so a stored
credential cannot leak through a process list.

Pure command building lives here so it can be tested; launching goes through `spawn`, the
one place a process starts, which tests replace.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from tsunami.core.issues import TsunamiError

# CREATE_NEW_CONSOLE is Windows-only; the value is stable.
_CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)

# Hosts and usernames come from the inventory and the credential store. The MobaXterm
# command is one string that its own shell parses, so anything outside a plain hostname,
# address or login name is refused rather than quoted.
_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_USER = re.compile(r"^[A-Za-z0-9._-]+(@[A-Za-z0-9._-]+)?$")


class SshLaunchError(TsunamiError):
    """No SSH client to start, or a host or username that cannot be put on a command line."""


def find_mobaxterm(
    env: Mapping[str, str] | None = None,
    *,
    exists: Callable[[Path], bool] = Path.is_file,
    which: Callable[[str], str | None] = shutil.which,
) -> Path | None:
    """MobaXterm.exe in the usual install folders, else on PATH; None if not installed."""
    env = os.environ if env is None else env
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = env.get(var)
        if base:
            candidate = Path(base) / "Mobatek" / "MobaXterm" / "MobaXterm.exe"
            if exists(candidate):
                return candidate
    found = which("MobaXterm.exe")
    return Path(found) if found else None


def find_openssh(
    env: Mapping[str, str] | None = None,
    *,
    exists: Callable[[Path], bool] = Path.is_file,
    which: Callable[[str], str | None] = shutil.which,
) -> Path | None:
    """Windows OpenSSH's ssh.exe: on PATH, else in System32\\OpenSSH."""
    env = os.environ if env is None else env
    found = which("ssh")
    if found:
        return Path(found)
    root = env.get("SystemRoot") or env.get("windir")
    if root:
        candidate = Path(root) / "System32" / "OpenSSH" / "ssh.exe"
        if exists(candidate):
            return candidate
    return None


def ssh_target(host: str, username: str | None) -> str:
    """`user@host`, or just `host` without a username. Raises SshLaunchError if either part
    could not be passed safely."""
    if not _HOST.match(host or ""):
        raise SshLaunchError(f"cannot open SSH: {host!r} is not a plain host name or address")
    if not username:
        return host
    if not _USER.match(username):
        raise SshLaunchError("cannot open SSH: the login name has characters that are not allowed")
    return f"{username}@{host}"


def build_command(
    host: str,
    port: int,
    username: str | None = None,
    *,
    mobaxterm: Path | None = None,
    openssh: Path | None = None,
) -> list[str]:
    """The argv that opens an interactive session. MobaXterm when given, else OpenSSH.

    Never takes a password, and nothing secret can appear in the result."""
    if not isinstance(port, int) or not 0 < port < 65536:
        raise SshLaunchError(f"cannot open SSH: {port!r} is not a port number")
    target = ssh_target(host, username)
    if mobaxterm is not None:
        return [str(mobaxterm), "-newtab", f"ssh -p {port} {target}"]
    if openssh is None:
        raise SshLaunchError(
            "No SSH client found: install MobaXterm or the Windows OpenSSH client."
        )
    return [str(openssh), "-p", str(port), target]


def spawn(argv: list[str], **kwargs: Any) -> Any:
    """Start the terminal. The one place a process starts; tests replace this."""
    return subprocess.Popen(  # noqa: S603 - argv is built from validated parts, no shell
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **kwargs,
    )


def open_ssh(host: str, port: int, username: str | None = None) -> list[str]:
    """Open a terminal to the device and return the command that was started. Looks for
    MobaXterm, then Windows OpenSSH; raises SshLaunchError if neither is installed."""
    moba = find_mobaxterm()
    argv = build_command(
        host,
        port,
        username,
        mobaxterm=moba,
        openssh=None if moba is not None else find_openssh(),
    )
    try:
        if moba is not None:  # a GUI program: nothing special
            spawn(argv)
        else:  # a console program: give it a console window of its own
            spawn(argv, creationflags=_CREATE_NEW_CONSOLE)
    except OSError as exc:
        raise SshLaunchError(f"could not start {argv[0]}: {exc.strerror or exc}") from None
    return argv
