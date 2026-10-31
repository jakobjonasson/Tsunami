# Tsunami

Tsunami is a Windows desktop tool that pushes templated Cisco IOS/IOS-XE and HPE Aruba AOS-CX/AOS-S configuration to up to 200 devices over SSH or console. It has a GUI for people and a local MCP server for Claude. Claude can read inventory, draft plays, render and run read-only checks. It never sees credentials or secret values, and changes to customer devices need approval in the GUI.

**Status:** Feature-complete for v1 and confirmed working on real devices for every platform except AOS-CX (table below). About 2,470 unit and 170 GUI tests run in CI, plus a frozen-build check. Open items are listed under "Open items" in [docs/implementation-plan.md](docs/implementation-plan.md). The spec is [docs/design.md](docs/design.md); read [CLAUDE.md](CLAUDE.md) before changing code.

## Tested platforms

| Platform | Tested on | Status |
|---|---|---|
| HPE Aruba AOS-S (ProCurve) | 2530-48G, YA.16.11 | Confirmed working, including console day-0 (first-boot login, speed sense) |
| Cisco IOS | Catalyst 2960-X | Confirmed working |
| Cisco IOS-XE | ISR C1101-4P | Confirmed working |
| Cisco IOS on Linux (IOL) | CML lab | Confirmed working (rollback copy on the `unix:` file system) |
| HPE Aruba AOS-CX | Not yet | Built and unit-tested against simulated devices only; checkpoint rollback is unverified on hardware |

Older Cisco and HP switches that offer only SHA-1 SSH algorithms are reached through an automatic fallback (`core/transports/legacy_ssh.py`).

## How it works

- **Workspace:** a folder of YAML and Jinja files the user edits in VS Code: `inventory/` (customers, sites, devices; device types are roles), `plays/` (initial, push, baseline), `snippets/`, and `drafts/` (Claude's writing area). The GUI edits inventory through context menus too.
- **Engine (`core/`):** validates, renders templates (secrets as `secret('customer/name')` placeholders), diffs against running config, and pushes with canary, batches, backups and rollback. Every outbound string goes through one redactor.
- **GUI (`tsunami.exe`):** the only place changes are approved and run. It holds the unlocked credential vault (the Tsunami password, set on first launch) and serves Claude's tool calls.
- **MCP server (`tsunami-mcp.exe`):** a stdio relay. It holds no credentials: each tool call goes over a local named pipe to the GUI, which runs it only while unlocked (403 when locked, 503 when Tsunami is closed).
- **Local data:** `%APPDATA%\Tsunami` (settings, MCP policy), `%LOCALAPPDATA%\Tsunami` (`history.db`, backups, logs, known_hosts, `vault.json`). Logins and secrets live in Windows Credential Manager, encrypted under the Tsunami password: one entry per value (`tsunami/<key>`), local-machine persistence only (see `core/secret_store.py` for why).

## Development setup

Requires Windows 10 or 11 and [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 itself if it isn't already present.

```powershell
uv sync                                  # create .venv and install everything
uv run tsunami                           # GUI
uv run tsunami-mcp                       # MCP relay on stdio (needs the GUI running and unlocked)
uv run python -m tsunami --selftest      # dev CLI; also init, validate, render, run
uv run pytest tests/unit                 # unit tests (about 90 s)
$env:QT_QPA_PLATFORM="offscreen"; uv run pytest tests/gui   # GUI tests (about 3.5 min)
uv run ruff check
uv run ruff format
```

Integration tests need a CML lab (`TSUNAMI_CML=1`) or a COM port (`TSUNAMI_SERIAL=1`); they skip otherwise.

## Code map

| Path | Contents |
|---|---|
| `src/tsunami/core/` | All logic. No PySide6 or MCP imports (`tests/unit/test_architecture.py`). |
| `core/models/` | Pydantic models: inventory (device types, logins), plays, jobs (statuses), settings, MCP policy |
| `core/workspace.py`, `resolver.py`, `targets.py` | Loading and validating a workspace, variable merge with provenance, target selection |
| `core/render/`, `redact.py`, `platforms/` | Sandboxed rendering, `secret()` sentinels, the redactor, per-platform drivers (rules, facts, rollback) |
| `core/runner.py`, `pipeline.py`, `rollback.py` | Job orchestration (canary, batches, leases, heartbeats) and the per-device pipeline |
| `core/transports/` | SSH (Netmiko, jump hosts, host-key pinning, `legacy_ssh.py` SHA-1 fallback), console, mock |
| `core/vault.py`, `secret_store.py` | The Tsunami password and the encrypted Credential Manager store |
| `core/discovery.py` | Site discovery over CDP and LLDP: the chain run and the neighbor parsers |
| `core/backups.py`, `drift.py` | Config backups: saving, comparing (redacted), and the drift check |
| `core/approvals.py`, `history.py`, `migrations/` | Approval flow (ready / no_changes), `history.db` |
| `core/inventory_edit.py`, `drafts.py`, `pathguard.py` | GUI inventory edits, draft promotion, MCP path checks |
| `src/tsunami/gui/` | PySide6 GUI: `main_window.py` (VS Code-style shell), `lock.py` (password and auto-lock), `screens/` (one module per view) |
| `src/tsunami/mcp_server/` | `tools.py` (the tools, listed in `TOOL_NAMES`), `enforcement.py` (policy, redaction, audit), `bridge.py` (the relay), `server.py` |
| `tests/unit`, `tests/gui` | Unit and pytest-qt tests; `test_mcp_leaks.py` is a blind secret-leak suite |
| `packaging/` | PyInstaller spec, Inno Setup script, `build.ps1`, `sign.ps1` |

## Releases

Every push to `main` runs CI (GitHub Actions, Windows): lint, unit and GUI tests, then a frozen build with self-tests, an MCP smoke test and the Inno Setup installer. The installer is a build artifact:

```powershell
gh run download --repo jakobjonasson/Tsunami -n tsunami-installer -D <folder>
```

The version is `__version__` in `src/tsunami/__init__.py`; the installer and both exes take it from there. Builds are unsigned until a code-signing certificate is set up (`packaging/sign.ps1`). `packaging/README.md` covers local builds.
