# Tsunami

`docs/design.md` is the source of truth. `docs/implementation-plan.md` says how the work is split up and what state each phase is in. `docs/research/` holds sourced vendor facts behind the platform drivers. If code has to deviate from the spec, update the spec in the same change and add a line to its Revision notes saying why.

## Commands

- `uv sync`: install. Always run code through `uv run …`. Never call bare `python`: on the main dev machine it resolves to an unrelated 3.11 virtual environment.
- `uv run pytest`: unit tests. CML and serial tests skip unless `TSUNAMI_CML=1` or `TSUNAMI_SERIAL=1` is set.
- `uv run ruff check` and `uv run ruff format`: lint and format. CI fails on either.

## Rules

- `core/` must not import PySide6, `mcp`, `tsunami.gui` or `tsunami.mcp_server`. `tests/unit/test_architecture.py` enforces this.
- No `print` outside `cli.py` and `bootstrap.py`. Stdout belongs to the MCP protocol (ruff rule T20).
- Don't name a module after a stdlib or dependency package (`secrets`, `serial`, `mcp`, …). The architecture test enforces this.
- Never commit secrets, real customer or employer names, real hostnames, or real device IPs. Fixtures use placeholder names (`acme`) and RFC 5737 or private addresses. Device-output fixtures use `.txt`, because `.gitignore` blocks `*.cfg` as a guard.
- Secret values never appear in logs, exceptions, `history.db`, MCP output or `repr()`. Everything outbound goes through `core/redact.py`.
- Check any library parameter the spec names against the installed version before relying on it.
- Tests for the redactor, the resolver and the validator are written before or alongside the code they test.

## Workflow

- Commit and push straight to `main`. No `Co-Authored-By` or "Generated with" lines in commit messages.
- Bump `__version__` (`src/tsunami/__init__.py`) when shipping an installer: minor for new features, patch for fix-only builds.
- Every push runs CI (`.github/workflows/ci.yml`). Check it with `gh run list` and `gh run view <id> --log-failed`; the installer is the `tsunami-installer` artifact (see README, Releases).
- Real customer and employer names are blocked by local `pre-commit` and `commit-msg` hooks that read `.git/info/denylist`. Neither is versioned, so a fresh clone has no protection: ask the user for the terms and recreate them before the first commit. Never write the terms anywhere in the repo.
- A change that deviates from or extends the spec updates `docs/design.md` in the same commit, with a line in its Revision notes.

## Working notes

- GUI tests need `QT_QPA_PLATFORM=offscreen`. Modal dialogs go through a `run_dialog(dialog)` seam in each module (`gui/lock.py`, `gui/screens/inventory_dialogs.py`, `gui/screens/device_list.py`) so tests can fill and accept them; never leave a bare `exec()` a test can reach, or the run hangs.
- Context menus are built by a `menu_for(item)` method so tests can trigger actions by text.
- To look at the GUI, render widgets to PNG with `QT_QPA_PLATFORM=windows`, `WA_DontShowOnScreen` and `widget.grab()`. The offscreen platform has no fonts, so text does not draw there.
- Primary (accent) buttons need `setProperty("primary", True)` before the widget is first styled; setting it later leaves the default look.
- CI runs on 2-vCPU Windows runners: timing assertions need slack (see `tests/gui/test_responsiveness.py`).
- Tests use an in-memory keyring and temp `%APPDATA%`/`%LOCALAPPDATA%` (`tests/conftest.py`); never touch the real Credential Manager or Recycle Bin from a test. Vault tests lower the scrypt cost.
- `history.db` schema changes go in a new numbered file in `core/migrations/`, never an edit to an old one.
- Paramiko 5 has no SHA-1; `core/transports/legacy_ssh.py` adds it back as a fallback. Netmiko gets the transport through `connection_class()` in `netmiko_session.py`.

## Subagent work

Subagents get one work package from the implementation plan. Each one:
- owns only the files the package names
- stops when its listed tests and ruff pass
- reports spec ambiguities back instead of resolving them

Subagents do not edit `docs/design.md`.
