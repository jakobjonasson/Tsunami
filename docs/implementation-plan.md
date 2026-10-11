# Implementation plan

This file goes with [design.md](design.md), which is the spec. The spec says *what* to build. This file says *how* it gets built and *who* builds each part: Opus, Sonnet or Haiku. If the two disagree, the spec wins until someone updates it.

**Status:** Phases 0–3 are done, except the lab work (3.8, 3.11), which is deferred until hardware and CML are available. Phase 4 (GUI) is done. Phase 5 (MCP server) is done except the leak suite (5.4) and the Inspector and GUI round trip (5.5).

---

## Decisions

| Decision | Outcome |
|---|---|
| Product and package name | `tsunami`. Exes: `tsunami.exe` and `tsunami-mcp.exe`. Keyring service: `tsunami`. Data folders: `%LOCALAPPDATA%\Tsunami` and `%APPDATA%\Tsunami`. |
| Repo location | `C:\dev\tsunami`, outside OneDrive. Git is the backup. |
| Remote | Private GitHub repo `jakobjonasson/Tsunami`. |
| Real names | No real customer or employer names in anything committed. The spec and fixtures use placeholders (`acme`) and RFC 5737 addresses. A local `pre-commit` and `commit-msg` hook blocks the terms listed in `.git/info/denylist`. The hook and the list are deliberately not versioned. |
| Spec fixes | Agreed and applied: per-customer host keys, no username in exact-value redaction, the `add_to_inventory` mapping, and job heartbeats with device leases. See design.md, Revision notes. |
| Switch model dropdown | A hardware catalog of common Cisco and Aruba switch families (`core/data/hardware.yml`), chosen through a new `model` prompt type. See design.md, Hardware catalog. |
| Platforms | Cisco IOS/IOS-XE, HPE Aruba AOS-CX, and HPE Aruba AOS-S (ProCurve syntax) are all v1 platforms. Comware is deferred. Vendor specifics live in platform drivers; see design.md, Platform drivers, and `docs/research/aruba-cli.md`. |
| GUI layout | VS Code-style: activity bar, side bar, tabbed editor area, bottom panel, status bar, command palette. Qt `windows11` style, following the Windows light or dark setting. |
| Phase 1 spec items | Approved: initial plays may set `site`, `role` and `platform` in play vars, and the day-0 prompt form asks in two stages. |

## Open questions

1. **Catalog verification.** Netmiko device types and the AOS-CX console baud were checked against sources. Platform assignments, lifecycle, end-of-sale years, example SKUs and `show_version_model` patterns have not been checked yet. That happens in work package 1.9.
2. **Test hardware.** Still to confirm:
   - that the CML lab is reachable
   - who downloads the AOS-CX Switch Simulator (it needs an HPE support-portal login)
   - a physical AOS-S switch for the bench
   - which physical models get staged over console
3. **Git workflow.** Decided: commit straight to `main`.
4. **Aruba facts still marked *verify* in the spec**, all to be confirmed on a device in 3.10:
   - the AOS-CX auto-checkpoint behavior after a dropped session
   - how AOS-CX's revert-now path interacts with a running auto-checkpoint timer
   - the AOS-S first-login prompt text
   - whether `cfg-restore` leaves flash-stored credentials alone

---

## Repository layout

```
tsunami/
├─ pyproject.toml            uv project, hatchling build, ruff and pytest config, entry points
├─ uv.lock                   committed
├─ .python-version           3.12
├─ .gitignore / .gitattributes
├─ README.md
├─ CLAUDE.md                 rules for Claude sessions and subagents
├─ docs/
│  ├─ design.md              the spec
│  ├─ implementation-plan.md this file
│  └─ research/aruba-cli.md  sourced AOS-CX / AOS-S facts behind the Aruba drivers
├─ .github/workflows/ci.yml  windows-latest: uv sync --locked, ruff, pytest, selftest
├─ src/tsunami/
│  ├─ __init__.py            __version__ (single source; frozen builds lose package metadata)
│  ├─ __main__.py            python -m tsunami → cli.main()
│  ├─ bootstrap.py           entry-point startup: keyring backend, --version, --selftest
│  ├─ cli.py                 dev CLI: init, validate, render, run
│  ├─ core/                  no PySide6 or mcp imports (enforced by a test)
│  │  ├─ paths.py            AppPaths from %LOCALAPPDATA% / %APPDATA%, never cwd       [done]
│  │  ├─ issues.py           Issue(severity, code, message, file, line); TsunamiError  [done]
│  │  ├─ yamlio.py           ruamel load with a line map; round-trip writes
│  │  ├─ models/
│  │  │  ├─ common.py        Slug, VarName, Platform, Netmiko maps, built-in vars      [done]
│  │  │  ├─ play.py          play schema; enums done, models in Phase 1
│  │  │  ├─ jobs.py          job and device statuses; enums done, records in Phase 3
│  │  │  ├─ workspace.py, inventory.py, settings.py, policy.py
│  │  ├─ workspace.py        loader, lint, Init workspace, schema_version check
│  │  ├─ schema_export.py    JSON Schemas + .vscode/settings.json
│  │  ├─ catalog.py          hardware catalog model and loader
│  │  ├─ resolver.py         8-level merge with per-key provenance
│  │  ├─ targets.py          sites/roles/devices/exclude → device list
│  │  ├─ prompts.py          prompt type validation and coercion, validation placeholders
│  │  ├─ render/
│  │  │  ├─ environment.py   SandboxedEnvironment; loader rejects include/import/extends
│  │  │  ├─ filters.py       ip, netmask, prefixlen, network, wildcard, vlan_range
│  │  │  ├─ lookup.py        snippet lookup: (drafts →) customer → platform → common
│  │  │  ├─ secret_refs.py   secret() sentinels, sink rule, mask and materialize
│  │  │  └─ renderer.py      play + device → rendered steps and hashes
│  │  ├─ secret_store.py     keyring store; protocol and constants done
│  │  ├─ redact.py           pattern layer + exact-value layer
│  │  ├─ configtree.py, diff.py               running/rendered config tree, missing/extra
│  │  ├─ facts.py, errordetect.py, checks.py
│  │  ├─ platforms/          one driver per family: base.py (protocol), ios.py, aoscx.py, aoss.py
│  │  ├─ transports/
│  │  │  ├─ base.py          Session protocol                                          [done]
│  │  │  ├─ ssh.py           Netmiko SSH, jump-host channel pool (≤10 per jump host)
│  │  │  ├─ hostkeys.py      per-customer trust on first use, key probe before login
│  │  │  ├─ console.py       cisco_ios_serial, day-0 dialogs
│  │  │  └─ mock.py          fake transport for scale tests
│  │  ├─ rollback.py, pipeline.py, runner.py, events.py
│  │  ├─ history.py, migrations/              SQLite (WAL), forward-only migrations
│  │  ├─ policy.py, pathguard.py, approvals.py
│  │  ├─ drafts.py, registration.py, logsetup.py
│  │  ├─ services.py         the single API the GUI, CLI and MCP server all call
│  │  └─ data/               hardware.yml, default-mcp-policy.yml                      [done]
│  ├─ gui/                   app.py (placeholder window), main_window, runner_thread, widgets/, screens/ (10)
│  └─ mcp_server/            server.py (entry stub), enforcement.py, tools.py
├─ tests/
│  ├─ conftest.py            temporary app-data dirs + in-memory keyring for every test
│  ├─ unit/                  entry points, architecture rules, paths, data files
│  └─ (added later) fixtures/, mcp/, gui/, integration/
└─ packaging/                Phase 6
```

Every module not marked done is a stub containing only a docstring. The docstring names the module's spec section and the work package that implements it.

---

## How the pieces connect

- **One API, three front ends.** `core/services.py` holds every operation. The MCP server wraps it in policy checks, redaction and audit rows. The GUI adds widgets and the CLI adds argparse. No front end contains logic of its own.
- **The runner knows nothing about the UI.** `Runner` receives everything it uses: play, targets, prompts, mode, a `Session` factory, a `SecretStore`, a `History`, a cancel `threading.Event`, an `on_event` callback and a `confirm_canary` callback. The GUI turns events into Qt signals, the CLI prints them, and the MCP server runs the runner on a background thread.
- **Session protocol** (`transports/base.py`): `send_config`, `send_exec`, `running_config`, `close`. The SSH, console and mock transports all implement it, so batching, max-fail, canary and rollback are tested against the mock first.
- **Rendered config has three forms.** Sentinel lines are internal only. Masked lines (`********`) are what gets displayed, stored and hashed. Materialized lines carry real secret values, exist only in apply mode, and are never stored.
- **Dependencies are passed in, not global.** `AppPaths`, `SecretStore`, `History` and `Policy` are all passed in. Only the entry points (via `bootstrap.py`) select the WinVault keyring backend.
- **Tooling enforces the guardrails:**
  - The architecture test fails if `core/` imports PySide6, `mcp`, `tsunami.gui` or `tsunami.mcp_server`, or if any module is named after a stdlib or dependency package.
  - Ruff rule `T20` bans `print` outside `cli.py` and `bootstrap.py`.
  - The `cml` and `serial` test markers skip unless their environment variable is set.
- **Heavy imports are lazy.** Netmiko, paramiko and PySide6 load inside the modules that use them.

---

## Spec issues

### Applied (see design.md, Revision notes)

- Per-customer `known_hosts` plus a key probe before first login.
- No username in exact-value redaction.
- Hardware catalog, the `model` prompt type, and the `add_to_inventory` field mapping.
- Job heartbeats, the `interrupted` status, and device leases with `device_busy`.
- `render_changed` added to the device statuses.
- The MCP package is `mcp_server`. Modules are named so they cannot shadow stdlib or dependency packages: `secret_refs.py`, `console.py`.
- The architecture diagram is redrawn in Mermaid.
- Initial plays may set `site`, `role` and `platform` in play vars, and the day-0 prompts are asked in two stages.
- Aruba AOS-CX and AOS-S platform drivers, CLI families, per-family snippet folders, and a per-family MCP allowlist.
- Secret-bearing lines are diffed by their non-secret prefix. This fixes permanent false drift on every platform, IOS hashes included.
- Aruba day-0 sets the forced admin or manager password to the customer login credential's password, so the switch is reachable with the stored login straight after staging. **Confirm this is the behavior you want.**
- The GUI is VS Code-style.

### Pending: decide before the phase that needs it

| # | Issue | Proposed | Needed by |
|---|---|---|---|
| 2 | keyring has no list API, but the Credentials screen and the exact-value redactor both need to enumerate names. | Use `CredEnumerate` through pywin32-ctypes, filtered to the `tsunami` service (to be verified). Fallback: a names index in `%APPDATA%\Tsunami`. | 2.3 |
| 3 | Facts that change over time (uptime) in a template would trip `render_changed` on every approved job. | A lint warning on templates that reference volatile facts. | 2.2 |
| 4 | Python 3.12 now receives security fixes only. The spec allows 3.13. | Stay on 3.12; revisit in Phase 6. | 6.1 |

---

## Build approach

### Who builds what

| Model | Gets | Examples |
|---|---|---|
| **Opus 5.5** (lead) | Interface definitions; anything where a subtle bug leaks a secret or leaves a device half-configured; all reviews and merges | models and cross-field validation, yamlio line mapping, resolver, `secret()` sink rule, redactor, runner/pipeline/rollback, host keys and jump host, MCP enforcement/pathguard/approvals, frozen-build debugging |
| **Sonnet 5.5** | Self-contained modules with a written interface and a test file to pass | prompt validators, targets, filters/sandbox/lookup, keyring store, history + migrations, configtree/diff, facts/error detection/checks, mock transport, console dialogs, GUI screens, MCP tools + allowlist, packaging, catalog research, independent redactor fixtures and leak tests |
| **Haiku 5.5** | Mechanical work: copying verbatim from the spec, or filling in tables | sample workspace from the appendix, broken-workspace variants from a list I supply, parametrized edge-case tables, boilerplate |

**Rule of thumb:** delegate only when the task has a written interface and a test file. A subagent starts cold and has to re-read the spec, so anything around 50 lines is cheaper to write directly.

### Delegation protocol

1. **I write the interface first:** signatures, dataclasses and docstrings, plus the key tests. For the redactor, resolver and validator, the tests come before the code.
2. **Each agent's prompt states:**
   - the spec sections to read
   - the files it owns and the files it must not touch
   - the commands that define done: `uv run pytest <its tests>` and `uv run ruff check`

   The prompt also tells the agent to report spec ambiguities back rather than resolve them itself.
3. **Parallel agents each get their own git worktree.** I review each diff and run the full test suite before merging.
4. **Security-critical modules are never delegated.** Where a second check helps (the redactor, the secret-leak suite), a separate agent writes adversarial fixtures without seeing my implementation.
5. **I check each phase's acceptance criteria against the spec.** Only I edit `design.md`.

### Phases

Each phase ends with the acceptance criteria listed in design.md, Build plan.

**Phase 1: Models and workspace**

| # | Work package | Owner | Depends on |
|---|---|---|---|
| 1.1 ✅ | yamlio + issues: ruamel load with a line map; Pydantic error `loc` → file:line | Opus | — |
| 1.2 ✅ | Models: workspace, inventory, play, including per-type defaults and cross-field rules | Opus | 1.1 |
| 1.3 ✅ | `prompts.py` validators, coercion, placeholders + edge-case tests | Sonnet | `PromptType` (done) |
| 1.4 ✅ | `targets.py` + AND/OR/exclude/empty-target tests | Sonnet | 1.2 |
| 1.5 ✅ | `resolver.py` with provenance, tests first | Opus | 1.2 |
| 1.6 ✅ | Init workspace, schema export, `.vscode/settings.json`, CLI `init` / `validate` | Sonnet | 1.2 |
| 1.7 ✅ | Fixtures: appendix workspace + minimal `base` / `access-ports` snippets; broken variants | Haiku | — |
| 1.8 ✅ | Workspace loader, `services.py` start, golden error tests, phase acceptance | Opus | all |
| 1.9 ✅ | `catalog.py` model and loader; verify catalog entries against vendor sources | Sonnet (web research); Opus reviews | — |

Phase 1 acceptance: the sample workspace validates clean; 21 golden broken copies report the right file and line; resolver, target, prompt and catalog tests pass (1,240 tests in total). VS Code autocompletion comes from the generated schemas but has not been checked by hand yet.

**Phase 2: Rendering and secrets**

| # | Work package | Owner | Depends on |
|---|---|---|---|
| 2.1 ✅ | Sandboxed environment and filters + sandbox-escape tests. Snippet lookup was built in 1.8 (`render/lookup.py`). The renderer must split rendered text on `\n` only, never `str.splitlines()` | Sonnet | Phase 1 |
| 2.2 ✅ | `secret()` sentinels, sink rule, renderer, settings model | Opus | 2.1 |
| 2.3 ✅ | Keyring-backed `SecretStore`, including enumeration (pending issue 2) | Sonnet | — |
| 2.4 ✅ | `redact.py`, both layers | Opus | — |
| 2.5 ✅ | Redactor fixture configs + adversarial tests for all three families, written without seeing 2.4 | Sonnet (separate agent) | — |
| 2.6 ✅ | CLI `render` | Opus | 2.2 |
| 2.7 ✅ | `PlatformDriver` protocol + IOS driver, rendering side: sink and redactor patterns, interface validation, config-tree rules, guarded commands | Opus | 2.2, 2.4 |
| 2.8 ✅ | AOS-CX and AOS-S drivers, rendering side. Opus writes the sink and redactor patterns; Sonnet writes interface validation, config-tree rules (`exit` blocks, quoting) and the reboot-required lint | Opus + Sonnet | 2.7 |

Phase 2 acceptance: `tsunami render` prints masked config for any sample device; the blind redactor suite (written without seeing the code) found 13 leaks, all fixed, and now passes on IOS, IOS-XE, AOS-CX and AOS-S fixtures; sink-rule violations fail with the offending file and line. 1,931 tests.

**Phase 3: Execution engine (headless)**

| # | Work package | Owner | Depends on |
|---|---|---|---|
| 3.1 ✅ | Runner (canary, batches, max-fail, 50-session cap, cancel, heartbeats), pipeline, rollback | Opus | Phase 2 |
| 3.2 ✅ | Mock transport + 200-device scale test | Sonnet | Session protocol |
| 3.3 ✅ | `history.py` + migrations (including leases and heartbeats), `logsetup.py` | Sonnet | jobs enums |
| 3.4 ✅ | configtree + diff + compliance, including the stale `ntp server` case | Sonnet | — |
| 3.5 ✅ | facts, errordetect, checks + show-output fixtures | Sonnet | — |
| 3.6 ✅ | SSH transport, jump-host channel pool, per-customer host keys | Opus | 3.1 |
| 3.7 ✅ | Console transport with configurable day-0 dialog patterns, fake-device tests | Sonnet | Session protocol |
| 3.8 ⏸ | CML integration suite (CLI `run` is done) | Opus + you | everything above, plus the CML lab |
| 3.9 ✅ | IOS driver, run side: facts, error patterns, flash backup, `configure replace`, revert timer, day-0 dialogs | Opus | 3.1 |
| 3.10 ✅ | Aruba drivers, run side. Sonnet: facts parsers, error patterns, the serial session subclasses, day-0 dialogs. Opus: checkpoint, `cfg-restore` and job-based rollback, and the AOS-CX `check_config_mode` subclass | Sonnet + Opus | 3.1, 2.8 |
| 3.11 ⏸ | Aruba lab: get the AOS-CX simulator booting as a CML custom node; bench AOS-S tests; resolve every *verify* item in design.md, Platform drivers | Opus + you | 3.10, hardware |

Phase 3 status: every engine piece is built and tested against the mock transport, including a 200-device run within the 50-session cap, rollback on rejected lines and failed post-checks, max-fail, canary, leases, and secret materialization with no leaks into history or events (2,115 tests). The CML and hardware acceptance criteria wait for 3.8 and 3.11.

To verify on hardware in 3.11, in addition to the *verify* items in design.md:
- AOS-S first-login manager-password prompt text, and whether a retype prompt follows.
- AOS-S console flow control (XON/XOFF default vs HPE's "off" guidance) and whether the boot dialog needs 9600 baud.
- Netmiko `serial_login` and `HPProcurveSSH` session preparation after the dialog phase leaves the console at a prompt.
- AOS-CX forced-password and empty-password prompt text on each firmware.
- IOS-XE autoinstall timing against the 5-second wake carriage return.
- AOS-S `cfg-backup`, `cfg-restore`, `alias` and `job` from manager context vs config context.

**Phase 4: GUI**

| # | Work package | Owner | Depends on |
|---|---|---|---|
| 4.1 ✅ | VS Code-style shell: activity bar, side bar, tabbed editor area, bottom panel, status bar, command palette and quick open, theme. Plus `runner_thread`, masked text widget, approvals poller and tray | Opus | Phase 3 |
| 4.2 ✅ | The ten screens, 2–3 agents at a time in worktrees (see the grouping below) | Sonnet | 4.1 |
| 4.3 ✅ | pytest-qt smoke tests | Sonnet | 4.2 |
| 4.4 ✅ | Leak review of Credentials and the masked views; responsiveness check with 200 mock devices | Opus | 4.2 |

Screen groups for 4.2:
- Workspace, Inventory, Plays, History
- Run wizard (including the model dropdown and the two-stage prompts)
- Job view
- Approvals and Drafts
- Credentials and Settings

**Phase 5: MCP server**

| # | Work package | Owner | Depends on |
|---|---|---|---|
| 5.1 ✅ | Enforcement wrapper, pathguard (symlinks, junctions), approval hashing, lab exception | Opus | Phase 4 |
| 5.2 ✅ | Policy model and loader + per-family show-allowlist matcher + tests | Sonnet | — |
| 5.3 ✅ | The MCP tools on top of `services`, tool descriptions | Sonnet | 5.1 |
| 5.4 | Scripted secret-leak client suite, written without seeing 5.3 | Sonnet (separate agent) | 5.1 |
| 5.5 | MCP Inspector pass (open); approval round trip with the GUI ✅ (`tests/gui/test_mcp_round_trip.py`) | Opus + you | 5.3 |

**Phase 6: Packaging and day-0**

| # | Work package | Owner | Depends on |
|---|---|---|---|
| 6.1 | PyInstaller spec (two EXEs, one COLLECT, data files), version resource, `--selftest` on the frozen build | Sonnet; Opus debugs failures that appear only in the frozen build | Phase 5 |
| 6.2 | Inno Setup script, `sign.ps1` hook | Sonnet | 6.1 |
| 6.3 | Claude Desktop registration (GUI button + installer page); `claude mcp add` instructions | Sonnet | 6.1 |
| 6.4 | CI frozen-build job | Sonnet | 6.1 |
| 6.5 | Clean-VM install, serial day-0 on a physical switch, EDR coordination | You, with Opus | 6.1–6.4 |

---

## Phase 0 checklist

- [x] Own repo at `C:\dev\tsunami`, branch `main`, remote set, local denylist hooks installed
- [x] Spec copied to `docs/design.md`, with the name change, real names replaced and agreed fixes applied
- [x] `pyproject.toml`, `.python-version`, `uv.lock`, ruff and pytest config
- [x] Package skeleton: stubs everywhere, plus enums, `Session`, `SecretStore`, `AppPaths` and `Issue` written in full
- [x] `--version` and `--selftest` on the CLI, GUI and MCP entry points
- [x] Tests: entry points, architecture rules, paths, data files
- [x] CI workflow on `windows-latest`
- [x] `CLAUDE.md`, `README.md`, `.gitignore`, `.gitattributes`
- [ ] CI green on the first push (check the Actions tab)
