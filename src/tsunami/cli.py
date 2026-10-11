"""Dev CLI for Tsunami, run as python -m tsunami.

Headless entry point for development and integration tests. Not shipped as its own
exe in v1 (design.md, Architecture: Components).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tsunami import __version__, bootstrap
from tsunami.core.issues import TsunamiError
from tsunami.core.scaffold import init_workspace
from tsunami.core.workspace import load_workspace

# Subcommand: (help text, phase that implements it). Each phase adds its arguments.
_PLANNED = {
    "render": ("render a play for one device, secrets masked", 2),
    "run": ("run a play headless", 3),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tsunami", description="Tsunami dev CLI.")
    parser.add_argument("--version", action="version", version=f"tsunami {__version__}")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="load models, data files and the keyring backend, then exit",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    init = sub.add_parser("init", help="create a workspace skeleton")
    init.add_argument("path", type=Path, help="workspace folder (created if missing)")
    init.add_argument("--name", help="workspace name (default: the folder name)")
    validate = sub.add_parser("validate", help="validate every file in a workspace")
    validate.add_argument(
        "path", type=Path, nargs="?", default=Path("."), help="workspace folder (default: .)"
    )
    for name, (help_text, _phase) in _PLANNED.items():
        sub.add_parser(name, help=help_text)
    return parser


def _init(path: Path, name: str | None) -> int:
    path = path.resolve()
    name = name or path.name
    try:
        created = init_workspace(path, name=name)
    except TsunamiError as exc:
        print(f"tsunami init: {exc}", file=sys.stderr)
        return 1
    print(f"Initialized workspace {name!r} at {path} ({len(created)} items created)")
    return 0


def _validate(path: Path) -> int:
    workspace = load_workspace(path)
    for issue in workspace.issues:
        print(issue)
    errors, warnings = len(workspace.errors), len(workspace.warnings)
    print(f"{errors} error(s), {warnings} warning(s)")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.selftest:
        return bootstrap.selftest()
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "init":
        return _init(args.path, args.name)
    if args.command == "validate":
        return _validate(args.path)
    phase = _PLANNED[args.command][1]
    print(f"tsunami {args.command}: not implemented yet (Phase {phase})", file=sys.stderr)
    return 2
