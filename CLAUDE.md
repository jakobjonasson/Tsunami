# Tsunami

`docs/design.md` is the source of truth. `docs/implementation-plan.md` says how the work is split up and what state each phase is in. If code has to deviate from the spec, update the spec in the same change and add a line to its Revision notes saying why.

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

## Subagent work

Subagents get one work package from the implementation plan. Each one:
- owns only the files the package names
- stops when its listed tests and ruff pass
- reports spec ambiguities back instead of resolving them

Subagents do not edit `docs/design.md`.
