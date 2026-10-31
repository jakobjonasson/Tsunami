# Packaging

Phase 6 (design.md, Packaging and installation). One PyInstaller onedir build with two executables, wrapped by a per-user Inno Setup installer.

| File | Purpose |
|---|---|
| `tsunami.spec` | PyInstaller spec: `tsunami.exe` (windowed) and `tsunami-mcp.exe` (console) share one COLLECT. Collects `core/data`, `core/migrations` and `gui/assets`, adds hidden imports, excludes unused Qt modules (and Qt entirely from the MCP exe). |
| `launch_gui.py`, `launch_mcp.py` | Entry scripts for the two exes. |
| `make_version_info.py` | Writes `version_info.txt` from `tsunami.__version__`. The spec runs it; never edit `version_info.txt` by hand. |
| `make_icon.py` | Draws `src/tsunami/gui/assets/tsunami.ico` (the exe, installer, window and tray icon) (16 to 256 px) with PySide6. The generated icon is committed; rerun only to change the design. |
| `installer.iss` | Inno Setup 6 script. `PrivilegesRequired=lowest`, installs to `%LOCALAPPDATA%\Programs\Tsunami`. **Never change `AppId`.** |
| `sign.ps1` | signtool hook (SHA-256, RFC 3161 timestamp, `verify /pa`). |
| `build.ps1` | Whole local build. |
| `smoke_mcp.py` | MCP stdio smoke test against the frozen `tsunami-mcp.exe`. |

## Build

```powershell
packaging\build.ps1            # sync, freeze, self-tests, installer if ISCC is installed
packaging\build.ps1 -Sign      # also sign (see below)
```

Output: `dist\Tsunami\` and `packaging\Output\Tsunami-<version>-setup.exe`. Build from the lockfile on the CI runner or a clean VM, not a daily workstation. The first PyInstaller run takes a few minutes.

By hand: `uv sync --locked --group build`, then `uv run pyinstaller packaging/tsunami.spec --noconfirm --clean --distpath dist --workpath build`.

## Test

```powershell
dist\Tsunami\tsunami-mcp.exe --selftest
$p = Start-Process dist\Tsunami\tsunami.exe --selftest -Wait -PassThru; $p.ExitCode   # windowed: exit code only
uv run python packaging/smoke_mcp.py dist\Tsunami\tsunami-mcp.exe                      # expects 21 tools
```

Launch check for the GUI: set `QT_QPA_PLATFORM=offscreen`, start `tsunami.exe`, wait a few seconds, confirm it is still running and `%LOCALAPPDATA%\Tsunami\logs\app.log` has no traceback.

`tsunami.exe --register-claude-desktop` adds `tsunami-mcp.exe` (next to it) to `%APPDATA%\Claude\claude_desktop_config.json`. The installer's final page runs it. The result shows in a message box, since the GUI exe has no console.

CI (`frozen-build` job in `.github/workflows/ci.yml`) does the build, both self-tests, the smoke test and the installer compile, and uploads the dist folder and installer as artifacts.

## Sign

```powershell
packaging\sign.ps1 -Thumbprint <sha1> -Files dist\Tsunami\tsunami.exe,dist\Tsunami\tsunami-mcp.exe,packaging\Output\Tsunami-<version>-setup.exe
```

Or set `TSUNAMI_SIGN_THUMBPRINT`, or `TSUNAMI_PFX_PATH` plus the password in `TSUNAMI_PFX_PASSWORD`. `-TimestampUrl` defaults to `http://timestamp.digicert.com`. Without a certificate the script exits 1, or 0 with `-Optional`. Sign the exes before compiling the installer so the installer contains signed binaries, then sign the installer.

## Uninstall

Removes the binaries. A prompt offers to delete `%LOCALAPPDATA%\Tsunami` and `%APPDATA%\Tsunami`; the default is to keep them (a silent uninstall always keeps them). Credentials stored in Windows Credential Manager are never removed by the uninstaller.
