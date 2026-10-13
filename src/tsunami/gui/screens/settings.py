"""Settings screen: workspace path, MCP policy editor, Claude Desktop registration.

The side bar lists the sections; clicking one opens the Settings tab scrolled to it.
Each section has its own Save button.

See design.md, GUI; MCP interface: Policy file; Packaging and installation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from tsunami.core import registration
from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import PlatformFamily
from tsunami.core.models.policy import McpPolicy
from tsunami.core.models.settings import AppSettings
from tsunami.core.policy import PolicyError, load_policy, save_policy
from tsunami.gui import wheel
from tsunami.gui.context import AppContext
from tsunami.gui.shell import ActivityItem, Shell

SECTIONS = (
    ("general", "General"),
    ("errors", "Secrets and errors"),
    ("mcp", "MCP"),
    ("claude", "Claude"),
)

MCP_WARNING = (
    "Enabling MCP sends customer configuration and topology, with secrets redacted, to "
    "Claude. Confirm company policy and client agreements first."
)


def validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors(include_url=False, include_input=False, include_context=False):
        where = ".".join(str(p) for p in err["loc"])
        parts.append(f"{where}: {err['msg'].removeprefix('Value error, ')}")
    return "; ".join(parts)


def find_mcp_exe() -> Path | None:
    """tsunami-mcp.exe next to the frozen app, or None when running from source."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).with_name("tsunami-mcp.exe")
        if exe.is_file():
            return exe
    return None


class _FamilyLines(QWidget):
    """One editable list (a line per entry) for each platform family."""

    def __init__(self, data: dict[Any, list[str]]) -> None:
        super().__init__()
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        self.edits: dict[PlatformFamily, QPlainTextEdit] = {}
        for family in PlatformFamily:
            edit = QPlainTextEdit("\n".join(data.get(family, [])))
            edit.setMinimumHeight(70)
            edit.setMaximumHeight(140)
            self.edits[family] = edit
            form.addRow(family.value, edit)

    def values(self) -> dict[PlatformFamily, list[str]]:
        out = {}
        for family, edit in self.edits.items():
            lines = [ln.strip() for ln in edit.toPlainText().splitlines() if ln.strip()]
            if lines:
                out[family] = lines
        return out


class SettingsTab(QScrollArea):
    def __init__(self, ctx: AppContext, shell: Shell) -> None:
        super().__init__()
        self.ctx, self.shell = ctx, shell
        self.setWidgetResizable(True)
        body = QWidget()
        self._layout = QVBoxLayout(body)
        self.setWidget(body)
        self.sections: dict[str, QGroupBox] = {}
        self._build_general()
        self._build_errors()
        self._build_mcp()
        self._build_claude()
        self._layout.addStretch(1)

    def _section(self, key: str, title: str) -> QVBoxLayout:
        box = QGroupBox(title)
        self.sections[key] = box
        self._layout.addWidget(box)
        return QVBoxLayout(box)

    def _save_button(self, layout: QVBoxLayout, slot: Any) -> QPushButton:
        button = QPushButton("Save")
        button.clicked.connect(lambda _=False: slot())
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(button)
        layout.addLayout(row)
        return button

    def scroll_to(self, key: str) -> None:
        box = self.sections.get(key)
        if box is not None:
            self.ensureWidgetVisible(box, 0, 0)
            self.verticalScrollBar().setValue(box.y())

    def _apply_settings(self, update: dict[str, Any]) -> bool:
        """Validate the changed fields through AppSettings and save them."""
        current = self.ctx.settings
        try:
            new = AppSettings.model_validate({**current.model_dump(mode="json"), **update})
        except ValidationError as exc:
            self.shell.notify(validation_message(exc), "error")
            return False
        try:
            self.ctx.save_settings(current.model_copy(update=new.model_dump()))
        except (TsunamiError, OSError) as exc:
            self.shell.notify(f"Settings were not saved: {exc}", "error")
            return False
        self.shell.notify("Settings saved.")
        return True

    # -- General -----------------------------------------------------------------------

    def _build_general(self) -> None:
        layout = self._section("general", "General")
        s = self.ctx.settings
        form = QFormLayout()
        row = QHBoxLayout()
        self.workspace_edit = QLineEdit(s.workspace or "")
        browse = QPushButton("Browse…")
        browse.clicked.connect(lambda _=False: self._browse_workspace())
        row.addWidget(self.workspace_edit, 1)
        row.addWidget(browse)
        form.addRow("Workspace", row)
        self.theme_combo = wheel.ComboBox()
        self.theme_combo.addItems(["system", "light", "dark"])
        self.theme_combo.setCurrentText(s.theme)
        form.addRow("Theme", self.theme_combo)
        self.log_combo = wheel.ComboBox()
        self.log_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.log_combo.setCurrentText(s.log_level)
        form.addRow("Log level", self.log_combo)
        self.retention_spin = wheel.SpinBox()
        self.retention_spin.setRange(1, 1000)
        self.retention_spin.setValue(s.backup_retention)
        form.addRow("Backup retention", self.retention_spin)
        self.legacy_ssh_check = QCheckBox("Allow SHA-1 SSH for devices that offer nothing newer")
        self.legacy_ssh_check.setChecked(s.ssh_legacy_algorithms)
        self.legacy_ssh_check.setToolTip(
            "SHA-1 algorithms are offered after every modern one, so modern devices still "
            "use modern crypto. Turn off to refuse devices that need SHA-1."
        )
        form.addRow("SSH", self.legacy_ssh_check)
        lock_row = QHBoxLayout()
        self.auto_lock_spin = wheel.SpinBox()
        self.auto_lock_spin.setRange(0, 480)
        self.auto_lock_spin.setSuffix(" min")
        self.auto_lock_spin.setSpecialValueText("never")
        self.auto_lock_spin.setValue(s.auto_lock_minutes)
        lock_row.addWidget(self.auto_lock_spin)
        self.password_button = QPushButton("Change Tsunami password…")
        self.password_button.setEnabled(getattr(self.ctx, "lock", None) is not None)
        self.password_button.clicked.connect(lambda _=False: self._change_password())
        lock_row.addWidget(self.password_button)
        lock_row.addStretch(1)
        form.addRow("Auto-lock", lock_row)
        layout.addLayout(form)
        self._save_button(layout, self.save_general)

    def _change_password(self) -> None:
        lock = getattr(self.ctx, "lock", None)
        if lock is not None and lock.change_password(self):
            self.shell.notify("Tsunami password changed.")

    def _browse_workspace(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose workspace folder", self.workspace_edit.text()
        )
        if chosen:
            self.workspace_edit.setText(chosen)

    def save_general(self) -> bool:
        workspace = self.workspace_edit.text().strip() or None
        ok = self._apply_settings(
            {
                "workspace": workspace,
                "theme": self.theme_combo.currentText(),
                "log_level": self.log_combo.currentText(),
                "backup_retention": self.retention_spin.value(),
                "ssh_legacy_algorithms": self.legacy_ssh_check.isChecked(),
                "auto_lock_minutes": self.auto_lock_spin.value(),
            }
        )
        if ok and workspace and Path(workspace) != self.ctx.workspace_path:
            if Path(workspace).is_dir():
                self.ctx.open_workspace(workspace)
            else:
                self.shell.notify(f"Workspace folder not found: {workspace}", "warning")
        return ok

    # -- Secrets and errors ----------------------------------------------------------------

    def _build_errors(self) -> None:
        layout = self._section("errors", "Secrets and errors")
        s = self.ctx.settings
        hint = QLabel(
            "One entry per line. A sink pattern is a regular expression with a "
            "(?P<secret>...) group marking the token. Benign messages are regular "
            "expressions for device output that looks like an error but is not."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addWidget(QLabel("Extra sink patterns"))
        self.sink_lines = _FamilyLines(s.extra_sink_patterns)
        layout.addWidget(self.sink_lines)
        layout.addWidget(QLabel("Benign error messages"))
        self.benign_lines = _FamilyLines(s.benign_messages)
        layout.addWidget(self.benign_lines)
        self._save_button(layout, self.save_errors)

    def save_errors(self) -> bool:
        return self._apply_settings(
            {
                "extra_sink_patterns": self.sink_lines.values(),
                "benign_messages": self.benign_lines.values(),
            }
        )

    # -- MCP -------------------------------------------------------------------------------

    def _build_mcp(self) -> None:
        layout = self._section("mcp", "MCP")
        warning = QLabel(MCP_WARNING)
        warning.setWordWrap(True)
        warning.setObjectName("McpWarning")
        warning.setStyleSheet("font-weight: bold;")
        layout.addWidget(warning)
        try:
            policy = load_policy(self.ctx.paths.policy_file)
        except PolicyError as exc:
            self.shell.notify(str(exc), "error")
            policy = McpPolicy()
        self._loaded_enabled = policy.enabled
        form = QFormLayout()
        self.mcp_enabled = QCheckBox("MCP server enabled")
        self.mcp_enabled.setChecked(policy.enabled)
        form.addRow(self.mcp_enabled)
        self.apply_combo = wheel.ComboBox()
        self.apply_combo.addItems(["never", "lab_only"])
        self.apply_combo.setCurrentText(policy.apply)
        form.addRow("Apply mode", self.apply_combo)
        self.trust_keys = QCheckBox("Claude may connect to devices whose host key is not on file")
        self.trust_keys.setChecked(policy.trust_new_host_keys)
        self.trust_keys.setToolTip(
            "The key is recorded on first contact, as the window's own runs do. A device "
            "whose key has changed is always refused."
        )
        form.addRow("Host keys", self.trust_keys)
        self.lab_drafts = QCheckBox("Lab customers may receive draft plays")
        self.lab_drafts.setChecked(policy.lab_allow_drafts)
        form.addRow(self.lab_drafts)
        layout.addLayout(form)

        layout.addWidget(QLabel("Customers hidden from MCP"))
        self.blocked_boxes: dict[str, QCheckBox] = {}
        known = sorted(set(self.ctx.workspace.customers if self.ctx.workspace else ()))
        for slug in [*known, *(b for b in policy.customers_blocked if b not in known)]:
            box = QCheckBox(slug)
            box.setChecked(slug in policy.customers_blocked)
            self.blocked_boxes[slug] = box
            layout.addWidget(box)

        layout.addWidget(QLabel("Show allowlist (a command prefix per line)"))
        self.allow_lines = _FamilyLines(policy.show_allowlist)
        layout.addWidget(self.allow_lines)
        layout.addWidget(QLabel("Allowed pipe modifiers (a word per line)"))
        self.pipe_lines = _FamilyLines(policy.pipes)
        layout.addWidget(self.pipe_lines)

        limits = QFormLayout()
        self.max_targets = wheel.SpinBox()
        self.max_targets.setRange(1, 200)
        self.max_targets.setValue(policy.max_check_targets)
        limits.addRow("Max check targets", self.max_targets)
        self.rate_limit = wheel.SpinBox()
        self.rate_limit.setRange(1, 600)
        self.rate_limit.setValue(policy.rate_limit_per_minute)
        limits.addRow("Rate limit per minute", self.rate_limit)
        self.expiry = wheel.SpinBox()
        self.expiry.setRange(1, 1440)
        self.expiry.setValue(policy.approval_expiry_minutes)
        limits.addRow("Approval expiry (minutes)", self.expiry)
        layout.addLayout(limits)
        self._save_button(layout, self.save_mcp)

    def save_mcp(self) -> bool:
        try:
            policy = McpPolicy.model_validate(
                {
                    "enabled": self.mcp_enabled.isChecked(),
                    "apply": self.apply_combo.currentText(),
                    "lab_allow_drafts": self.lab_drafts.isChecked(),
                    "trust_new_host_keys": self.trust_keys.isChecked(),
                    "customers_blocked": [
                        s for s, b in self.blocked_boxes.items() if b.isChecked()
                    ],
                    "show_allowlist": self.allow_lines.values(),
                    "pipes": self.pipe_lines.values(),
                    "max_check_targets": self.max_targets.value(),
                    "rate_limit_per_minute": self.rate_limit.value(),
                    "approval_expiry_minutes": self.expiry.value(),
                }
            )
        except ValidationError as exc:
            self.shell.notify(validation_message(exc), "error")
            return False
        if policy.enabled and not self._loaded_enabled:
            answer = QMessageBox.question(self, "Enable MCP", f"{MCP_WARNING}\n\nEnable MCP?")
            if answer != QMessageBox.StandardButton.Yes:
                return False
        try:
            save_policy(policy, self.ctx.paths.policy_file)
        except (TsunamiError, OSError) as exc:
            self.shell.notify(f"The policy was not saved: {exc}", "error")
            return False
        self._loaded_enabled = policy.enabled
        refresh = getattr(self.shell, "_refresh_mcp", None)
        if callable(refresh):
            refresh()
        self.shell.notify("MCP policy saved.")
        return True

    # -- Claude -----------------------------------------------------------------------------

    def _build_claude(self) -> None:
        layout = self._section("claude", "Claude")
        self.claude_notice = QLabel("")
        self.claude_notice.setWordWrap(True)
        layout.addWidget(self.claude_notice)
        self.register_button = QPushButton("Register with Claude Desktop")
        self.register_button.clicked.connect(lambda _=False: self.register_claude())
        layout.addWidget(self.register_button)
        layout.addWidget(QLabel("Claude Code command"))
        self.code_command = QLineEdit()
        self.code_command.setReadOnly(True)
        self.copy_button = QPushButton("Copy")
        self.copy_button.clicked.connect(lambda _=False: self.copy_command())
        row = QHBoxLayout()
        row.addWidget(self.code_command, 1)
        row.addWidget(self.copy_button)
        layout.addLayout(row)
        self.refresh_claude()

    def refresh_claude(self) -> None:
        exe = find_mcp_exe()
        if exe is None:
            self.claude_notice.setText(
                "tsunami-mcp.exe was not found. Registration works from the installed "
                "app; a development run has no executable to register."
            )
            self.register_button.setEnabled(False)
            self.code_command.setText("")
            self.copy_button.setEnabled(False)
        else:
            self.claude_notice.setText(f"Registers {exe}")
            self.register_button.setEnabled(True)
            self.code_command.setText(registration.claude_code_command(exe))
            self.copy_button.setEnabled(True)

    def register_claude(self) -> bool:
        exe = find_mcp_exe()
        if exe is None:
            self.shell.notify("tsunami-mcp.exe was not found next to the app.", "error")
            return False
        try:
            written = registration.register_claude_desktop_all(exe)
        except (TsunamiError, OSError) as exc:
            self.shell.notify(f"Registration failed: {exc}", "error")
            return False
        self.claude_notice.setText(
            "Registered in " + ", ".join(str(p) for p in written) + " (a .bak copy is "
            "next to each). Quit Claude Desktop from the system tray and reopen it; closing "
            "the window does not reload the config."
        )
        self.shell.notify("Registered with Claude Desktop. Quit it from the tray and reopen it.")
        return True

    def copy_command(self) -> None:
        QGuiApplication.clipboard().setText(self.code_command.text())


class SettingsSidebar(QListWidget):
    def __init__(self, ctx: AppContext, shell: Shell) -> None:
        super().__init__()
        self.ctx, self.shell = ctx, shell
        for key, title in SECTIONS:
            self.addItem(title)
            self.item(self.count() - 1).setData(0x0100, key)  # Qt.UserRole
        self.itemClicked.connect(lambda item: open_settings(ctx, shell, item.data(0x0100)))


def open_settings(ctx: AppContext, shell: Shell, section: str | None = None) -> QWidget:
    tab = shell.open_tab("settings", "Settings", lambda: SettingsTab(ctx, shell), "settings-gear")
    if section and isinstance(tab, SettingsTab):
        tab.scroll_to(section)
    return tab


def register(shell: Shell) -> None:
    shell.register_command(
        "settings.open",
        "Preferences: Open settings",
        lambda: open_settings(shell.ctx, shell),
        "Ctrl+,",
    )


VIEW = ActivityItem(
    "settings",
    "Settings",
    "settings-gear",
    lambda ctx, shell: SettingsSidebar(ctx, shell),
    bottom=True,
)
