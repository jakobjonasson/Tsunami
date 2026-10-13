"""Light and dark palettes and the style sheet for the VS Code-style shell.

Fusion style with Tsunami's own palettes: Qt's native windows11 style does not support
style sheets reliably, and the shell's chrome (activity bar, side bar, tabs, panel,
status bar) needs them. Colors are calm warm neutrals with one teal-blue accent. The scheme
follows the Windows light or dark setting unless Settings overrides it.

See design.md, GUI: Layout.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

UI_FONTS = ("Segoe UI Variable Text", "Segoe UI", "Inter", "Arial")
MONO_FONTS = ("Cascadia Mono", "Cascadia Code", "Consolas", "Courier New")


@dataclass(frozen=True)
class Palette:
    name: str
    activity: str  # activity bar
    sidebar: str
    editor: str  # tab content background
    panel: str
    tabs: str  # inactive tab strip
    border: str
    text: str
    muted: str
    accent: str
    accent_text: str
    selection: str
    hover: str
    input: str
    error: str
    warning: str
    success: str
    info: str


DARK = Palette(
    name="dark",
    activity="#1a1917",
    sidebar="#21201e",
    editor="#262624",
    panel="#21201e",
    tabs="#1d1c1a",
    border="#34322f",
    text="#e8e6e1",
    muted="#a19d96",
    accent="#4fb3c8",  # teal-blue: 6.2:1 on the editor background
    accent_text="#10191b",
    selection="#3d3a36",
    hover="#2e2c29",
    input="#1d1c1a",
    error="#f07167",
    warning="#e9b44c",
    success="#7fb77e",
    info="#7aa7d9",
)

LIGHT = Palette(
    name="light",
    activity="#e9e6dc",
    sidebar="#f2f0e8",
    editor="#fbfaf6",
    panel="#f2f0e8",
    tabs="#ebe8df",
    border="#dcd8cc",
    text="#1f1e1c",
    muted="#6b6760",
    accent="#1f7a8c",  # teal-blue: 5:1 with white text
    accent_text="#ffffff",
    selection="#e2ddd0",
    hover="#e6e2d7",
    input="#ffffff",
    error="#c4402f",
    warning="#a8730c",
    success="#3f7d3e",
    info="#2f6aa8",
)


def resolve(preference: str, app: QApplication | None = None) -> Palette:
    """The palette for a Settings theme value: system, light or dark."""
    if preference == "dark":
        return DARK
    if preference == "light":
        return LIGHT
    app = app or QApplication.instance()
    scheme = app.styleHints().colorScheme() if app is not None else Qt.ColorScheme.Unknown
    return DARK if scheme == Qt.ColorScheme.Dark else LIGHT


def ui_font(point_size: float = 9.5) -> QFont:
    font = QFont()
    font.setFamilies(list(UI_FONTS))
    font.setPointSizeF(point_size)
    return font


def mono_font(point_size: float = 9.5) -> QFont:
    font = QFont()
    font.setFamilies(list(MONO_FONTS))
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSizeF(point_size)
    return font


def apply(app: QApplication, palette: Palette) -> None:
    app.setStyle("Fusion")
    qp = QPalette()
    roles = {
        QPalette.ColorRole.Window: palette.sidebar,
        QPalette.ColorRole.WindowText: palette.text,
        QPalette.ColorRole.Base: palette.editor,
        QPalette.ColorRole.AlternateBase: palette.hover,
        QPalette.ColorRole.Text: palette.text,
        QPalette.ColorRole.Button: palette.hover,
        QPalette.ColorRole.ButtonText: palette.text,
        QPalette.ColorRole.Highlight: palette.selection,
        QPalette.ColorRole.HighlightedText: palette.text,
        QPalette.ColorRole.ToolTipBase: palette.editor,
        QPalette.ColorRole.ToolTipText: palette.text,
        QPalette.ColorRole.PlaceholderText: palette.muted,
        QPalette.ColorRole.Link: palette.accent,
    }
    for role, color in roles.items():
        qp.setColor(role, QColor(color))
    for role in (
        QPalette.ColorRole.Text,
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.ButtonText,
    ):
        qp.setColor(QPalette.ColorGroup.Disabled, role, QColor(palette.muted))
    app.setPalette(qp)
    app.setFont(ui_font())
    app.setStyleSheet(stylesheet(palette))
    app.setProperty("tsunami_palette", palette.name)


def current() -> Palette:
    app = QApplication.instance()
    return DARK if app is not None and app.property("tsunami_palette") == "dark" else LIGHT


def stylesheet(p: Palette) -> str:
    return f"""
QMainWindow, QDialog {{ background: {p.sidebar}; }}
QToolTip {{ background: {p.editor}; color: {p.text}; border: 1px solid {p.border}; padding: 4px; }}

#ActivityBar {{ background: {p.activity}; border-right: 1px solid {p.border}; }}
#ActivityBar QToolButton {{ border: none; border-left: 2px solid transparent; padding: 10px; }}
#ActivityBar QToolButton:hover {{ background: {p.hover}; }}
#ActivityBar QToolButton:checked {{ border-left: 2px solid {p.accent}; background: {p.sidebar}; }}

#SideBar {{ background: {p.sidebar}; }}
#SideScroll, #SideScroll > QWidget#qt_scrollarea_viewport {{ background: {p.sidebar}; border: none; }}
#SideBarHeader {{ color: {p.muted}; font-weight: 600; letter-spacing: 1px; padding: 10px 12px 6px 12px; }}
#SideBarAction {{ border: none; border-radius: 4px; padding: 3px; margin-top: 4px; }}
#SideBarAction:hover {{ background: {p.hover}; }}

QTabWidget#Editor::pane, QTabWidget#Panel::pane {{ border: none; }}
QTabWidget#Editor > QWidget {{ background: {p.editor}; }}
QTabBar {{ background: {p.tabs}; }}
QTabBar::tab {{ background: {p.tabs}; color: {p.muted}; padding: 7px 14px; border: none;
               border-right: 1px solid {p.border}; }}
QTabBar::tab:selected {{ background: {p.editor}; color: {p.text}; border-top: 2px solid {p.accent}; }}
QTabBar::tab:hover:!selected {{ background: {p.hover}; }}
QTabWidget#Panel QTabBar::tab {{ background: {p.panel}; text-transform: uppercase; border-right: none; }}
QTabWidget#Panel QTabBar::tab:selected {{ border-top: none; border-bottom: 1px solid {p.accent}; }}

QStatusBar {{ background: {p.activity}; color: {p.muted}; border-top: 1px solid {p.border}; }}
QStatusBar QLabel {{ padding: 0 8px; }}
QStatusBar QLabel[alarm="true"] {{ background: {p.error}; color: #ffffff; font-weight: 600; }}
QStatusBar QLabel[attention="true"] {{ color: {p.accent}; font-weight: 600; }}

QSplitter::handle {{ background: {p.border}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}

QTreeView, QListView, QTableView, QPlainTextEdit, QTextEdit {{
    background: {p.editor}; border: none; selection-background-color: {p.selection};
    selection-color: {p.text}; outline: 0; }}
#SideBar QTreeView, #SideBar QListView {{ background: {p.sidebar}; }}
QTreeView::item, QListView::item {{ padding: 3px 2px; }}
QTreeView::item:hover, QListView::item:hover {{ background: {p.hover}; }}
QHeaderView::section {{ background: {p.sidebar}; color: {p.muted}; border: none;
                        border-bottom: 1px solid {p.border}; padding: 4px 6px; }}

QLineEdit, QSpinBox, QComboBox, QPlainTextEdit[editable="true"] {{
    background: {p.input}; border: 1px solid {p.border}; border-radius: 4px; padding: 4px 6px; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid {p.accent}; }}
/* A styled spin box loses Fusion's arrows (they draw as empty boxes); numbers are typed,
   and the arrow keys and mouse wheel still step them. */
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ width: 0; border: none; }}
QLineEdit[invalid="true"] {{ border: 1px solid {p.error}; }}

QPushButton {{ background: {p.hover}; border: 1px solid {p.border}; border-radius: 4px;
               padding: 5px 14px; }}
QPushButton:hover {{ border-color: {p.accent}; }}
QPushButton[primary="true"] {{ background: {p.accent}; color: {p.accent_text}; border: none;
                               font-weight: 600; }}
QPushButton[danger="true"] {{ color: {p.error}; }}
QPushButton:disabled {{ color: {p.muted}; border-color: {p.border}; }}

#Banner[level="error"] {{ background: {p.error}; color: #ffffff; padding: 8px; font-weight: 600; }}
#Banner[level="warning"] {{ background: {p.warning}; color: #1a1917; padding: 8px; }}
#Card {{ background: {p.editor}; border: 1px solid {p.border}; border-radius: 8px; }}
#Muted {{ color: {p.muted}; }}
#FormError {{ color: {p.error}; }}
#Section {{ color: {p.muted}; font-weight: 600; }}
QPushButton#Link {{ background: transparent; border: none; border-radius: 4px; color: {p.accent};
                    padding: 6px 10px; text-align: left; }}
QPushButton#Link:hover {{ background: {p.hover}; }}
#Keys {{ color: {p.muted}; background: {p.hover}; border: 1px solid {p.border}; border-radius: 4px;
         padding: 2px 8px; font-family: "{MONO_FONTS[0]}", "{MONO_FONTS[2]}"; font-size: 8.5pt; }}
#Title {{ font-size: 14pt; font-weight: 600; }}
"""
