# Packaging

Phase 6 (design.md, Packaging and installation). Planned contents:

- `tsunami.spec`: PyInstaller onedir build with two EXE targets (`tsunami.exe` windowed, `tsunami-mcp.exe` console) sharing one COLLECT. It must collect `tsunami/core/data/*`.
- `installer.iss`: Inno Setup, `PrivilegesRequired=lowest`, installs to `%LOCALAPPDATA%\Programs\Tsunami`, fixed `AppId`.
- `version_info.txt`: Windows version resource.
- `sign.ps1`: signtool hook for both exes and the installer.
- `assets/tsunami.ico`
