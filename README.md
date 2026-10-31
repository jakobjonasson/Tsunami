# Tsunami

Tsunami is a Windows desktop tool that pushes templated Cisco IOS/IOS-XE and HPE Aruba AOS-CX/AOS-S configuration to up to 200 devices over SSH or console. It has a GUI for people and a local MCP server for Claude. Claude can read inventory, draft plays, render and run read-only checks. It never sees credentials or secret values, and changes to customer devices need approval in the GUI.

**Status:** Feature-complete for v1 and confirmed working on real devices for every platform except AOS-CX (table below). About 2,800 unit and 250 GUI tests run in CI, plus a frozen-build check. Open items are listed under "Open items" in [docs/implementation-plan.md](docs/implementation-plan.md). The spec is [docs/design.md](docs/design.md); read [CLAUDE.md](CLAUDE.md) before changing code.

## Tested platforms

| Platform | Tested on | Status |
|---|---|---|
| HPE Aruba AOS-S (ProCurve) | 2530-48G, YA.16.11 | Confirmed working, including console day-0 (first-boot login, speed sense) |
| Cisco IOS | Catalyst 2960-X | Confirmed working |
| Cisco IOS-XE | ISR C1101-4P | Confirmed working |
| Cisco IOS on Linux (IOL) | CML lab | Confirmed working (rollback copy on the `unix:` file system) |
| HPE Aruba AOS-CX | Not yet | Built and unit-tested against simulated devices only; checkpoint rollback is unverified on hardware |

Older Cisco and HP switches that offer only SHA-1 SSH algorithms are reached through an automatic fallback (`core/transports/legacy_ssh.py`).

## What it does

**Plays and runs**
- Three play types: `initial` (day-0 staging over console or SSH, optionally adding the new device to the inventory), `push` (changes) and `baseline` (a customer's standard; compliance reports missing lines, and extra lines under the prefixes a step `owns`).
- Four run modes: render (no connection), check (a diff against running config, nothing sent), compliance (baseline plays) and apply.
- Apply runs a canary device first, then batches, with a failure threshold, pre- and post-checks, a config backup, rollback on failure or a revert timer, and `write memory` when the checks pass.
- Typed prompts with dropdowns (site, role, hardware model, a saved login) and per-device answers.
- Secrets reach templates only as `secret('name')`, or `login(name).password` for a saved login. Both are placeholders that may sit only on password lines, and real values exist only in memory during apply.
- History of every job with masked renders and redacted diffs and output; retry failed devices, apply after a check, or run again.

**Inventory and onboarding**
- Customers with sites, devices, device types (roles), a default login per customer and per site, logins on single devices, an optional jump host, and `push_logins` (accounts plays may create on devices).
- Import devices from CSV (Save example CSV gives every column), or edit them through the Inventory menus.
- Discover a site over CDP and LLDP: start from seeds, add each device and follow its neighbors inside a scope. Check for unlearned neighbors lists neighbors missing from the inventory.
- Trust host keys in bulk without logging in, the jump host's included. A changed key is shown with both fingerprints and replaced only after confirmation.
- Device page: the model, version and serial the device reported, recent jobs, Test connection, Open SSH (MobaXterm when installed, else OpenSSH) and Backups.

**Backups and drift**
- Backups come from every apply, from discovery and from drift checks. The viewer shows one or compares two, always through the redactor.
- Back up and check for drift (a site or a customer): a first backup for new devices, and for the rest a redacted diff since the newest backup.

**Claude over MCP** (25 tools, listed in `TOOL_NAMES`)
- Read the workspace and history: `list_customers`, `list_devices`, `list_plays`, `list_snippets`, `read_play`, `read_snippet`, `resolved_vars`, `validate`, `render`, `list_jobs`, `get_job`.
- Write drafts under `drafts/` (`write_draft`, `delete_draft`); only a person promotes one.
- Read devices: `show` for allowlisted commands (matched by command prefix), `check` and `compliance`. `request_allowlist_addition` asks a person to allow another `show` command in Settings.
- Change devices: `request_run`, approved in the GUI (lab customers may skip approval if the policy allows), and `cancel_job`.
- Add to the inventory when Settings allow it (off by default): `add_customer`, `add_site`, `add_role`, `add_devices` (with `create_sites` and `create_roles`). Edits and removals are only proposed: `propose_inventory_change` waits for approval, and `get_inventory_proposal` reports the outcome.
- `mcp-policy.yml` sets whether MCP is on, whether lab changes need approval, blocked customers, the rate limit and the show allowlist. Edit it in Settings or in VS Code.
- Claude never receives a credential or secret value, every result is redacted, and every call is audited.

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
uv run pytest tests/unit                 # unit tests (about 2 min)
$env:QT_QPA_PLATFORM="offscreen"; uv run pytest tests/gui   # GUI tests (about 6 min)
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
| `core/vault.py`, `secret_store.py`, `logins.py` | The Tsunami password, the encrypted Credential Manager store, and which logins a customer's plays may push |
| `core/discovery.py`, `hostkey_trust.py`, `inventory_import.py` | Onboarding: site discovery over CDP and LLDP, bulk host-key trust, CSV import |
| `core/backups.py`, `drift.py` | Config backups: saving, comparing (redacted), and the drift check |
| `core/approvals.py`, `history.py`, `migrations/` | Approval flow (ready / no_changes), `history.db` |
| `core/inventory_edit.py`, `inventory_proposals.py`, `drafts.py`, `pathguard.py` | Inventory edits, inventory changes Claude proposes, draft promotion, MCP path checks |
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
