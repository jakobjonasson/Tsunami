# PyInstaller spec: one onedir build, two executables sharing one COLLECT.
#   tsunami.exe      windowed GUI
#   tsunami-mcp.exe  console, stdio MCP server
# Build: uv run pyinstaller packaging/tsunami.spec --noconfirm --clean --distpath dist --workpath build
# See packaging/README.md and design.md, Packaging and installation.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

HERE = Path(SPECPATH)  # noqa: F821  (provided by PyInstaller)
ROOT = HERE.parent
SRC = ROOT / "src"

sys.path.insert(0, str(HERE))
import make_version_info  # noqa: E402

make_version_info.write()
VERSION_FILE = str(HERE / "version_info.txt")
ICON = str(SRC / "tsunami" / "gui" / "assets" / "tsunami.ico")  # also the window icon

# Package data read through importlib.resources.files(...): core/data, core/migrations and
# gui/assets. Same relative layout as in the source tree.
datas = []
for pattern, dest in (
    ("core/data/*.yml", "tsunami/core/data"),
    ("core/migrations/*.sql", "tsunami/core/migrations"),
    ("gui/assets/*.ttf", "tsunami/gui/assets"),
    ("gui/assets/*.csv", "tsunami/gui/assets"),
    ("gui/assets/*.txt", "tsunami/gui/assets"),
    ("gui/assets/*.ico", "tsunami/gui/assets"),
):
    for f in sorted((SRC / "tsunami").glob(pattern)):
        datas.append((str(f), dest))

# Modules that frozen builds commonly lose: lazily imported platform drivers, keyring's
# Windows backend (set explicitly in bootstrap.py), pywin32-ctypes (CredEnumerate), the
# MCP server, serial port enumeration.
hiddenimports = (
    collect_submodules("tsunami")
    + collect_submodules("netmiko")
    + collect_submodules("mcp", filter=lambda name: not name.startswith("mcp.cli"))
    + collect_submodules("keyring.backends")
    + collect_submodules("win32ctypes")
    + [
        "keyring.backends.Windows",
        "serial.tools.list_ports",
        "serial.tools.list_ports_windows",
        "jinja2.sandbox",
        "pydantic",
        "ruamel.yaml",
    ]
)
# Version lookups through importlib.metadata need the dist-info folders.
for dist in ("mcp", "keyring", "netmiko", "pydantic", "ruamel.yaml", "jinja2", "pyserial"):
    datas += copy_metadata(dist)

# Qt modules the app never imports (checked with grep over src/).
excludes = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2",
    "PySide6.QtQml",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtSensors",
    "PySide6.QtPositioning",
    "PySide6.QtLocation",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtRemoteObjects",
    "PySide6.QtSpatialAudio",
    "PySide6.QtTextToSpeech",
    "PySide6.QtVirtualKeyboard",
    "PySide6.QtScxml",
    "PySide6.QtStateMachine",
    "PySide6.QtSerialBus",
    "PySide6.QtSerialPort",
    "tkinter",
]

# The MCP exe never touches Qt (core/ and mcp_server/ do not import PySide6).
mcp_excludes = excludes + ["PySide6", "shiboken6"]


def analysis(script, extra_excludes, imports=hiddenimports):
    return Analysis(  # noqa: F821
        [str(HERE / script)],
        pathex=[str(SRC)],
        datas=datas,
        hiddenimports=imports,
        excludes=extra_excludes,
        noarchive=False,
    )


gui_a = analysis("launch_gui.py", excludes)
mcp_imports = [m for m in hiddenimports if not m.startswith("tsunami.gui")]
mcp_a = analysis("launch_mcp.py", mcp_excludes, mcp_imports)

gui_exe = EXE(  # noqa: F821
    PYZ(gui_a.pure),  # noqa: F821
    gui_a.scripts,
    [],
    exclude_binaries=True,
    name="tsunami",
    console=False,
    icon=ICON,
    version=VERSION_FILE,
    upx=False,
)
mcp_exe = EXE(  # noqa: F821
    PYZ(mcp_a.pure),  # noqa: F821
    mcp_a.scripts,
    [],
    exclude_binaries=True,
    name="tsunami-mcp",
    console=True,
    icon=ICON,
    version=VERSION_FILE,
    upx=False,
)

COLLECT(  # noqa: F821
    gui_exe,
    gui_a.binaries,
    gui_a.datas,
    mcp_exe,
    mcp_a.binaries,
    mcp_a.datas,
    strip=False,
    upx=False,
    name="Tsunami",
)
