"""Dev CLI for Tsunami, run as python -m tsunami.

Headless entry point for development and integration tests. Not shipped as its own
exe in v1 (design.md, Architecture: Components).
"""

from __future__ import annotations

import argparse
import sys

from tsunami import __version__, bootstrap

# Subcommand: (help text, phase that implements it). Each phase adds its arguments.
_PLANNED = {
    "init": ("create a workspace skeleton", 1),
    "validate": ("validate a workspace or play", 1),
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
    for name, (help_text, _phase) in _PLANNED.items():
        sub.add_parser(name, help=help_text)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.selftest:
        return bootstrap.selftest()
    if args.command is None:
        parser.print_help()
        return 0
    phase = _PLANNED[args.command][1]
    print(f"tsunami {args.command}: not implemented yet (Phase {phase})", file=sys.stderr)
    return 2
