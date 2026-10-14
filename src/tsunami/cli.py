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

RUN_MODES = ("check", "compliance", "apply", "render")


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
    for name, help_text in (
        ("render", "render a play for one device, secrets masked"),
        ("run", "run a play headless (check mode unless --mode says otherwise)"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("play", help="play path inside the workspace, e.g. plays/push/x.yml")
        cmd.add_argument("-w", "--workspace", type=Path, default=Path("."), help="default: .")
        cmd.add_argument(
            "--set", action="append", default=[], metavar="NAME=VALUE", help="a prompt answer"
        )
        cmd.add_argument("--tags", help="comma-separated tag filter")
        if name == "render":
            cmd.add_argument("-d", "--device", help="target device (omit for initial plays)")
        else:
            cmd.add_argument("--mode", choices=RUN_MODES, default="check")
            cmd.add_argument(
                "-d", "--device", action="append", default=[], help="limit to this device"
            )
            cmd.add_argument("--port", help="serial port for day-0 plays, e.g. COM3")
            cmd.add_argument("--yes", action="store_true", help="continue past the canary")
    return parser


def _prompts(items: list[str]) -> dict[str, str]:
    prompts = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep:
            raise TsunamiError(f"--set needs NAME=VALUE, got {item!r}")
        prompts[name.strip()] = value
    return prompts


def _render(args: argparse.Namespace) -> int:
    from tsunami.core.services import render_play

    tags = [t.strip() for t in args.tags.split(",")] if args.tags else None
    workspace = load_workspace(args.workspace)
    try:
        rendering = render_play(
            workspace, args.play, device=args.device, prompts=_prompts(args.set), tags=tags
        )
    except TsunamiError as exc:
        print(f"tsunami render: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(rendering.masked_text())
    return 0


def _run(args: argparse.Namespace) -> int:
    from tsunami.core.events import (
        BatchFinished,
        CanaryWaiting,
        DeviceLog,
        DeviceStatusChanged,
        JobFinished,
        MaxFailReached,
    )
    from tsunami.core.history import History
    from tsunami.core.logsetup import configure_logging
    from tsunami.core.models.jobs import JobStatus
    from tsunami.core.models.play import RunMode
    from tsunami.core.models.settings import load_settings
    from tsunami.core.paths import AppPaths
    from tsunami.core.runner import Runner, RunRequest
    from tsunami.core.secret_store import KeyringSecretStore
    from tsunami.core.transports import DeviceTransport

    bootstrap.configure_keyring()
    paths = AppPaths.from_env()
    settings = load_settings(paths.settings_file)
    configure_logging(paths.logs_dir / "app.log", level=settings.log_level)
    workspace = load_workspace(args.workspace)
    history = History(paths.history_db)
    history.mark_interrupted()

    def show(event) -> None:
        if isinstance(event, DeviceStatusChanged):
            detail = f" ({event.detail})" if event.detail else ""
            print(f"{event.device}: {event.status}{detail}")
        elif isinstance(event, DeviceLog) and args.verbose_log:
            print(f"{event.device} | {event.text}")
        elif isinstance(event, BatchFinished):
            print(f"batch {event.batch} done: {event.failed} failed of {event.attempted}")
        elif isinstance(event, MaxFailReached):
            print(f"max_fail_percent {event.max_fail_percent} exceeded; no further batches")
        elif isinstance(event, CanaryWaiting):
            print(f"canary {event.device} finished")
        elif isinstance(event, JobFinished):
            print(f"job {event.job_id}: {event.status} {event.counts}")

    def confirm(device: str) -> bool:
        if args.yes:
            return True
        return input(f"Continue past canary {device}? [y/N] ").strip().lower().startswith("y")

    secrets = KeyringSecretStore(
        index_path=paths.settings / "credential-index.json", cipher=_unlock(paths)
    )
    bootstrap.migrate_credentials(secrets)
    try:
        runner = Runner(
            workspace,
            transport=DeviceTransport(paths, settings),
            history=history,
            secrets=secrets,
            paths=paths,
            app_settings=settings,
            on_event=show,
            confirm_canary=confirm,
        )
        job_id = runner.run(
            RunRequest(
                args.play,
                RunMode(args.mode),
                prompts=_prompts(args.set),
                tags=[t.strip() for t in args.tags.split(",")] if args.tags else None,
                limit=args.device or None,
                serial_port=args.port,
            )
        )
        job = history.get_job(job_id)
    except TsunamiError as exc:
        print(f"tsunami run: {exc}", file=sys.stderr)
        return 1
    finally:
        history.close()
    if job.summary.get("error"):
        print(f"tsunami run: {job.summary['error']}", file=sys.stderr)
    return 0 if job.status == JobStatus.COMPLETED else 1


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


def _unlock(paths):
    """The vault cipher, after asking for the Tsunami password; None if none is set."""
    import getpass

    from tsunami.core.vault import Vault

    vault = Vault(paths.vault_file)
    if not vault.exists():
        return None
    return vault.unlock(getpass.getpass("Tsunami password: "))


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.selftest:
        return bootstrap.selftest()
    if args.command == "init":
        return _init(args.path, args.name)
    if args.command == "validate":
        return _validate(args.path)
    if args.command == "render":
        return _render(args)
    if args.command == "run":
        args.verbose_log = False
        return _run(args)
    parser.print_help()
    return 0
