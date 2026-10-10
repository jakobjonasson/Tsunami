# Tsunami

Tsunami is a Windows desktop tool that pushes templated Cisco IOS/IOS-XE and HPE Aruba AOS-CX/AOS-S configuration to up to 200 devices over SSH or console. It has a GUI for people and a local MCP server for Claude. Claude can read inventory, draft plays, render and run read-only checks. It never sees credentials or secret values, and changes to customer devices need approval in the GUI.

**Status:** Phase 0 (skeleton). See [docs/implementation-plan.md](docs/implementation-plan.md) for build progress and [docs/design.md](docs/design.md) for the spec.

## Development setup

Requires Windows 10 or 11 and [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 itself if it isn't already present.

```powershell
uv sync                                  # create .venv and install everything
uv run python -m tsunami --version       # dev CLI
uv run python -m tsunami --selftest
uv run tsunami                           # GUI (placeholder window until Phase 4)
uv run pytest                            # unit tests; CML and serial tests skip by default
uv run ruff check
uv run ruff format
```

Integration tests need a CML lab (`TSUNAMI_CML=1`) or a COM port (`TSUNAMI_SERIAL=1`).

## Layout

| Path | Contents |
|---|---|
| `src/tsunami/core/` | All logic. No PySide6 or MCP imports. |
| `src/tsunami/gui/` | PySide6 GUI (`tsunami.exe`) |
| `src/tsunami/mcp_server/` | stdio MCP server (`tsunami-mcp.exe`) |
| `src/tsunami/cli.py` | Dev CLI (`python -m tsunami`) |
| `tests/` | Unit, MCP, GUI and integration tests |
| `packaging/` | PyInstaller spec, Inno Setup script, signing (Phase 6) |
