"""Settings screen: workspace path, MCP policy editor, Claude Desktop registration.

The side bar lists the sections; clicking one opens the Settings tab scrolled to it.
One Save bar writes whichever of settings.json and mcp-policy.yml has changed. Both files
can also be edited in VS Code; the tab reloads them when it is shown or the app regains
focus, and never overwrites unsaved edits without asking.

See design.md, GUI; MCP interface: Policy file; Packaging and installation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from tsunami.core import registration
from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import PlatformFamily
from tsunami.core.models.policy import McpPolicy
from tsunami.core.models.settings import AppSettings, load_settings, save_settings
from tsunami.core.policy import PolicyError, add_allowlist_entry, load_policy, save_policy
from tsunami.gui import wheel
from tsunami.gui.context import AppContext
from tsunami.gui.shell import ActivityItem, Shell

SECTIONS = (
    ("general", "General"),
    ("errors", "Advanced"),
    ("mcp", "MCP"),
    ("claude", "Claude"),
)

MCP_WARNING = (
    "Enabling MCP sends customer configuration and topology, with secrets redacted, to "
    "Claude. Confirm company policy and client agreements first."
)

GENERAL_KEYS = (
    "workspace",
    "theme",
    "log_level",
    "backup_retention",
    "ssh_legacy_algorithms",
    "auto_lock_minutes",
)
ERROR_KEYS = ("extra_sink_patterns", "benign_messages")
SETTINGS_KEYS = (*GENERAL_KEYS, *ERROR_KEYS)

TAB_TITLE = "Settings"
DIRTY_TITLE = "Settings ●"


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


def _key_line(path: Path, key: str) -> int | None:
    """The 1-based line of a top-level YAML key, or None if the file lacks it."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    return next((n for n, line in enumerate(lines, 1) if line.startswith(f"{key}:")), None)


def _stamp(path: Path) -> tuple[int, int] | None:
    """(mtime, size) of a file, or None if it is missing. Detects an external edit."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


class _FamilyLines(QWidget):
    """One editable list (a line per entry) for each platform family."""

    changed = Signal()

    def __init__(self, data: dict[Any, list[str]] | None = None) -> None:
        super().__init__()
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        self.edits: dict[PlatformFamily, QPlainTextEdit] = {}
        for family in PlatformFamily:
            edit = QPlainTextEdit()
            edit.setMinimumHeight(70)
            edit.setMaximumHeight(140)
            edit.textChanged.connect(self.changed)
            self.edits[family] = edit
            form.addRow(family.value, edit)
        self.set_values(data or {})

    def set_values(self, data: dict[Any, list[str]]) -> None:
        for family, edit in self.edits.items():
            edit.setPlainText("\n".join(data.get(family, [])))

    def values(self) -> dict[PlatformFamily, list[str]]:
        out = {}
        for family, edit in self.edits.items():
            lines = [ln.strip() for ln in edit.toPlainText().splitlines() if ln.strip()]
            if lines:
                out[family] = lines
        return out


class SettingsTab(QWidget):
    def __init__(self, ctx: AppContext, shell: Shell) -> None:
        super().__init__()
        self.ctx, self.shell = ctx, shell
        self._loading = True  # form fields are being filled: not user edits
        self._busy = False  # this tab is writing ctx.settings itself
        self._errors: dict[str, str] = {}  # file kind -> why the file on disk is unusable
        self._conflicts: dict[str, Any] = {}  # file kind -> parsed disk content vs unsaved edits
        self._stamps: dict[str, tuple[int, int] | None] = {}
        self._settings_snap: dict[str, Any] = {}
        self._policy_snap: dict[str, Any] = {}
        self._loaded_policy = McpPolicy()
        self._loaded_enabled = False
        self.sections: dict[str, QWidget] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self._build_notice(root)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        self._layout = QVBoxLayout(body)
        self.scroll.setWidget(body)
        root.addWidget(self.scroll, 1)
        self._build_files()
        self._build_general()
        self._build_errors()
        self._build_mcp()
        self._build_claude()
        self._layout.addStretch(1)
        self._build_save_bar(root)

        self._stamps = {k: _stamp(self._path(k)) for k in ("settings", "policy")}
        self._populate_settings(self.ctx.settings)
        try:
            policy = load_policy(self.ctx.paths.policy_file)
        except PolicyError as exc:
            self._errors["policy"] = (
                f"{exc} The form shows the defaults; saving would replace the file."
            )
            policy = McpPolicy()
        self._populate_policy(policy)
        self.refresh_requests()
        self._loading = False
        self._refresh_notice()
        self._update_dirty()
        self.ctx.settings_changed.connect(self._on_ctx_settings)
        app = QGuiApplication.instance()
        if app is not None:
            app.applicationStateChanged.connect(self._on_app_state)

    # -- layout helpers ----------------------------------------------------------------------------

    def _section(self, key: str, title: str) -> QVBoxLayout:
        box = QGroupBox(title)
        self.sections[key] = box
        self._layout.addWidget(box)
        return QVBoxLayout(box)

    def scroll_to(self, key: str) -> None:
        box = self.sections.get(key)
        if box is not None:
            if key == "errors":
                self.set_advanced_expanded(True)
            self.scroll.ensureWidgetVisible(box, 0, 0)
            self.scroll.verticalScrollBar().setValue(box.y())

    def _path(self, kind: str) -> Path:
        return self.ctx.paths.settings_file if kind == "settings" else self.ctx.paths.policy_file

    def _file_name(self, kind: str) -> str:
        return self._path(kind).name

    # -- notice banner (disk errors and conflicts) -------------------------------------------------

    def _build_notice(self, root: QVBoxLayout) -> None:
        self.notice = QWidget()
        row = QHBoxLayout(self.notice)
        row.setContentsMargins(12, 8, 12, 8)
        self.notice_label = QLabel()
        self.notice_label.setObjectName("Banner")
        self.notice_label.setWordWrap(True)
        self.notice_reload = QPushButton("Reload from disk")
        self.notice_reload.clicked.connect(lambda _=False: self.reload_from_disk())
        self.notice_keep = QPushButton("Keep my edits")
        self.notice_keep.clicked.connect(lambda _=False: self.keep_my_edits())
        row.addWidget(self.notice_label, 1)
        row.addWidget(self.notice_reload)
        row.addWidget(self.notice_keep)
        self.notice.hide()
        root.addWidget(self.notice)

    def _refresh_notice(self) -> None:
        if self._errors:
            level, text = "error", "\n".join(self._errors.values())
            text += "\nTsunami keeps using the last good settings until the file is fixed."
            self.notice_reload.setText("Reload from disk")
            self.notice_keep.setVisible(any(self.is_dirty(k) for k in self._errors))
        elif self._conflicts:
            names = " and ".join(self._file_name(k) for k in self._conflicts)
            level = "warning"
            text = (
                f"{names} changed on disk, but this tab has unsaved edits. Reload to take the "
                "file's values (your edits here are discarded), or keep your edits and save "
                "over the file."
            )
            self.notice_reload.setText("Reload and discard my edits")
            self.notice_keep.show()
        else:
            self.notice.hide()
            return
        self.notice_label.setText(text)
        self.notice_label.setProperty("level", level)
        style = self.notice_label.style()
        style.unpolish(self.notice_label)
        style.polish(self.notice_label)
        self.notice.show()

    # -- the files strip ---------------------------------------------------------------------------

    def _build_files(self) -> None:
        layout = self._section("files", "Files")
        info = QLabel(
            f"Settings are plain files in {self.ctx.paths.settings_file.parent}. Edit them "
            "here or in VS Code; Tsunami picks up changes when you return to this tab."
        )
        info.setWordWrap(True)
        info.setObjectName("Muted")
        layout.addWidget(info)
        row = QHBoxLayout()
        self.open_settings_button = QPushButton("Open settings.json in VS Code")
        self.open_settings_button.clicked.connect(lambda _=False: self.open_file("settings"))
        self.open_policy_button = QPushButton("Open mcp-policy.yml in VS Code")
        self.open_policy_button.clicked.connect(lambda _=False: self.open_file("policy"))
        self.reload_button = QPushButton("Reload from disk")
        self.reload_button.setToolTip("Re-read both files; unsaved edits in this tab are dropped.")
        self.reload_button.clicked.connect(lambda _=False: self.reload_from_disk())
        row.addWidget(self.open_settings_button)
        row.addWidget(self.open_policy_button)
        row.addWidget(self.reload_button)
        row.addStretch(1)
        layout.addLayout(row)

    def open_allowlists(self) -> bool:
        """Open mcp-policy.yml in VS Code at the `show_allowlist:` line."""
        if self.is_dirty("policy"):
            self.shell.notify(
                "This tab has unsaved MCP changes. They are not in mcp-policy.yml until you "
                "save; edits made in VS Code meanwhile will ask which copy to keep.",
                "warning",
            )
        return self.open_file("policy", key="show_allowlist")

    def open_file(self, kind: str, *, key: str | None = None) -> bool:
        """Open settings.json or mcp-policy.yml in VS Code, creating it first if missing.
        key: a top-level YAML key to open the file at, when the file has it."""
        path = self._path(kind)
        if not path.is_file():
            try:
                if kind == "settings":
                    save_settings(self.ctx.settings, path)
                else:
                    save_policy(self._loaded_policy, path)
            except (TsunamiError, OSError) as exc:
                self.shell.notify(f"{path.name} could not be created: {exc}", "error")
                return False
            self._stamps[kind] = _stamp(path)
        self.shell.open_in_vscode(path, _key_line(path, key) if key else None)
        return True

    # -- save bar ----------------------------------------------------------------------------------

    def _build_save_bar(self, root: QVBoxLayout) -> None:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Plain)
        root.addWidget(line)
        bar = QWidget()
        bar.setObjectName("SaveBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 8, 12, 8)
        self.dirty_label = QLabel("Unsaved changes")
        self.dirty_label.setObjectName("Banner")
        self.dirty_label.setProperty("level", "warning")
        self.clean_label = QLabel("All changes saved")
        self.clean_label.setObjectName("Muted")
        self.discard_button = QPushButton("Discard changes")
        self.discard_button.clicked.connect(lambda _=False: self.discard_changes())
        self.save_button = QPushButton("Save")
        self.save_button.setProperty("primary", True)
        self.save_button.setToolTip("Write settings.json and mcp-policy.yml (whichever changed).")
        self.save_button.clicked.connect(lambda _=False: self.save_all())
        row.addWidget(self.dirty_label)
        row.addWidget(self.clean_label)
        row.addStretch(1)
        row.addWidget(self.discard_button)
        row.addWidget(self.save_button)
        root.addWidget(bar)

    # -- dirty tracking ----------------------------------------------------------------------------

    def _on_edit(self, *_: Any) -> None:
        if not self._loading:
            self._update_dirty()

    def is_dirty(self, kind: str | None = None) -> bool:
        if kind in (None, "settings") and self._settings_fields() != self._settings_snap:
            return True
        return kind in (None, "policy") and self._policy_fields() != self._policy_snap

    def _update_dirty(self) -> None:
        dirty = self.is_dirty()
        self.save_button.setEnabled(dirty)
        self.discard_button.setEnabled(dirty)
        self.dirty_label.setVisible(dirty)
        self.clean_label.setVisible(not dirty)
        editor = getattr(self.shell, "editor", None)
        if editor is not None and hasattr(editor, "indexOf"):
            index = editor.indexOf(self)
            if index >= 0:
                editor.setTabText(index, DIRTY_TITLE if dirty else TAB_TITLE)

    def discard_changes(self) -> None:
        self._populate_settings(self.ctx.settings)
        self._populate_policy(self._loaded_policy)
        self._update_dirty()

    # -- form values -------------------------------------------------------------------------------

    def _settings_fields(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace_edit.text().strip() or None,
            "theme": self.theme_combo.currentText(),
            "log_level": self.log_combo.currentText(),
            "backup_retention": self.retention_spin.value(),
            "ssh_legacy_algorithms": self.legacy_ssh_check.isChecked(),
            "auto_lock_minutes": self.auto_lock_spin.value(),
            "extra_sink_patterns": self.sink_lines.values(),
            "benign_messages": self.benign_lines.values(),
        }

    def _policy_fields(self) -> dict[str, Any]:
        return {
            "enabled": self.mcp_enabled.isChecked(),
            "apply": self.apply_combo.currentText(),
            "lab_allow_drafts": self.lab_drafts.isChecked(),
            "trust_new_host_keys": self.trust_keys.isChecked(),
            "inventory_edits": self.inventory_edits.isChecked(),
            "customers_blocked": [s for s, b in self.blocked_boxes.items() if b.isChecked()],
            "show_allowlist": self.allow_lines.values(),
            "pipes": self.pipe_lines.values(),
            "max_check_targets": self.max_targets.value(),
            "rate_limit_per_minute": self.rate_limit.value(),
            "approval_expiry_minutes": self.expiry.value(),
        }

    def _populate_settings(self, s: AppSettings) -> None:
        was_loading, self._loading = self._loading, True
        try:
            self.workspace_edit.setText(s.workspace or "")
            self.theme_combo.setCurrentText(s.theme)
            self.log_combo.setCurrentText(s.log_level)
            self.retention_spin.setValue(s.backup_retention)
            self.legacy_ssh_check.setChecked(s.ssh_legacy_algorithms)
            self.auto_lock_spin.setValue(s.auto_lock_minutes)
            self.sink_lines.set_values(s.extra_sink_patterns)
            self.benign_lines.set_values(s.benign_messages)
        finally:
            self._loading = was_loading
        self._settings_snap = self._settings_fields()
        self._update_advanced_summary()

    def _populate_policy(self, policy: McpPolicy) -> None:
        was_loading, self._loading = self._loading, True
        try:
            self.mcp_enabled.setChecked(policy.enabled)
            self.apply_combo.setCurrentText(policy.apply)
            self.lab_drafts.setChecked(policy.lab_allow_drafts)
            self.trust_keys.setChecked(policy.trust_new_host_keys)
            self.inventory_edits.setChecked(policy.inventory_edits)
            self._rebuild_blocked(policy)
            self.allow_lines.set_values(policy.show_allowlist)
            self.pipe_lines.set_values(policy.pipes)
            self.max_targets.setValue(policy.max_check_targets)
            self.rate_limit.setValue(policy.rate_limit_per_minute)
            self.expiry.setValue(policy.approval_expiry_minutes)
        finally:
            self._loading = was_loading
        self._loaded_policy = policy
        self._loaded_enabled = policy.enabled
        self._policy_snap = self._policy_fields()

    def _rebuild_blocked(self, policy: McpPolicy) -> None:
        for box in self.blocked_boxes.values():
            self._blocked_layout.removeWidget(box)
            box.deleteLater()
        self.blocked_boxes = {}
        known = sorted(set(self.ctx.workspace.customers if self.ctx.workspace else ()))
        for slug in [*known, *(b for b in policy.customers_blocked if b not in known)]:
            box = QCheckBox(slug)
            box.setChecked(slug in policy.customers_blocked)
            box.toggled.connect(self._on_edit)
            self.blocked_boxes[slug] = box
            self._blocked_layout.addWidget(box)

    # -- disk changes ------------------------------------------------------------------------------

    def showEvent(self, event: Any) -> None:  # noqa: N802 (Qt API)
        super().showEvent(event)
        self.check_external()

    def _on_app_state(self, state: Qt.ApplicationState) -> None:
        if state == Qt.ApplicationState.ApplicationActive and self.isVisible():
            self.check_external()

    def _on_ctx_settings(self) -> None:
        """Something else (the workspace opening, say) saved settings.json."""
        if self._busy:
            return
        self._stamps["settings"] = _stamp(self._path("settings"))
        if not self.is_dirty("settings"):
            self._populate_settings(self.ctx.settings)
            self._update_dirty()

    def _read_disk(self, kind: str) -> AppSettings | McpPolicy:
        path = self._path(kind)
        return load_settings(path) if kind == "settings" else load_policy(path)

    def check_external(self) -> None:
        """Pick up an edit made outside Tsunami (VS Code, say) to either file."""
        for kind in ("settings", "policy"):
            stamp = _stamp(self._path(kind))
            if stamp == self._stamps.get(kind):
                continue
            self._stamps[kind] = stamp
            try:
                parsed = self._read_disk(kind)
            except (TsunamiError, OSError) as exc:
                self._errors[kind] = str(exc)
                self._conflicts.pop(kind, None)
                continue
            self._errors.pop(kind, None)
            if self.is_dirty(kind):
                self._conflicts[kind] = parsed
            else:
                self._conflicts.pop(kind, None)
                self._apply_loaded(kind, parsed)
                self.shell.notify(f"Reloaded {self._file_name(kind)} from disk.")
        self._refresh_notice()
        self._update_dirty()

    def reload_from_disk(self) -> bool:
        """Re-read both files, dropping unsaved edits. A bad file is reported and skipped."""
        ok = True
        for kind in ("settings", "policy"):
            self._stamps[kind] = _stamp(self._path(kind))
            try:
                parsed = self._read_disk(kind)
            except (TsunamiError, OSError) as exc:
                self._errors[kind] = str(exc)
                ok = False
                continue
            self._errors.pop(kind, None)
            self._conflicts.pop(kind, None)
            self._apply_loaded(kind, parsed)
        self._refresh_notice()
        self._update_dirty()
        if ok:
            self.shell.notify("Reloaded settings and MCP policy from disk.")
        return ok

    def keep_my_edits(self) -> None:
        """Keep the form as it is; the next save replaces the file on disk."""
        self._conflicts.clear()
        for kind in [k for k in self._errors if self.is_dirty(k)]:
            del self._errors[kind]
        self._refresh_notice()

    def _apply_loaded(self, kind: str, parsed: Any) -> None:
        """Make a validated file's content the live state: form, ctx and (for MCP) status."""
        if kind == "settings":
            self._busy = True
            try:
                self.ctx.settings = parsed
                self.ctx.settings_changed.emit()
            finally:
                self._busy = False
            self._populate_settings(parsed)
            workspace = parsed.workspace
            if workspace and Path(workspace) != self.ctx.workspace_path:
                if Path(workspace).is_dir():
                    self.ctx.open_workspace(workspace)
                else:
                    self.shell.notify(f"Workspace folder not found: {workspace}", "warning")
        else:
            # Nothing to push to the MCP server: tsunami-mcp re-reads mcp-policy.yml on every
            # call (McpApp.policy), so the file on disk is what the next call enforces.
            self._populate_policy(parsed)
            self._refresh_status()

    def _refresh_status(self) -> None:
        refresh = getattr(self.shell, "_refresh_mcp", None)
        if callable(refresh):
            refresh()

    # -- saving ------------------------------------------------------------------------------------

    def _validate_settings(self, keys: tuple[str, ...]) -> AppSettings | None:
        fields = self._settings_fields()
        update = {k: fields[k] for k in keys}
        try:
            return AppSettings.model_validate(
                {**self.ctx.settings.model_dump(mode="json"), **update}
            )
        except ValidationError as exc:
            if any(str(err["loc"][0]) in ERROR_KEYS for err in exc.errors()):
                self.set_advanced_expanded(True)
            self.shell.notify(validation_message(exc), "error")
            return None

    def _validate_policy(self) -> McpPolicy | None:
        try:
            return McpPolicy.model_validate(self._policy_fields())
        except ValidationError as exc:
            self.shell.notify(validation_message(exc), "error")
            return None

    def _commit(self, settings_keys: tuple[str, ...], policy: bool) -> bool:
        """Validate everything first, then write the files; nothing is written if any is bad.

        Every save path comes through here, so none writes over a file that changed on disk
        (an edit in VS Code, say) until the person reloads it or keeps their edits."""
        self.check_external()
        writing = {"settings"} if settings_keys else set()
        if policy:
            writing.add("policy")
        if any(k in self._conflicts or (k in self._errors and self.is_dirty(k)) for k in writing):
            self.shell.notify(
                "A settings file changed on disk. Reload it or keep your edits, then save.",
                "warning",
            )
            return False
        new_settings = new_policy = None
        if settings_keys:
            new_settings = self._validate_settings(settings_keys)
            if new_settings is None:
                return False
        if policy:
            new_policy = self._validate_policy()
            if new_policy is None:
                return False
            if new_policy.enabled and not self._loaded_enabled:
                answer = QMessageBox.question(self, "Enable MCP", f"{MCP_WARNING}\n\nEnable MCP?")
                if answer != QMessageBox.StandardButton.Yes:
                    return False
        saved: list[str] = []
        if new_settings is not None:
            self._busy = True
            try:
                self.ctx.save_settings(new_settings)
            except (TsunamiError, OSError) as exc:
                self.shell.notify(f"Settings were not saved: {exc}", "error")
                return False
            finally:
                self._busy = False
            self._settings_snap.update(
                {k: v for k, v in self._settings_fields().items() if k in settings_keys}
            )
            self._finish_save("settings")
            saved.append("Settings")
            workspace = new_settings.workspace
            if (
                "workspace" in settings_keys
                and workspace
                and Path(workspace) != self.ctx.workspace_path
            ):
                if Path(workspace).is_dir():
                    self.ctx.open_workspace(workspace)
                else:
                    self.shell.notify(f"Workspace folder not found: {workspace}", "warning")
        if new_policy is not None:
            try:
                save_policy(new_policy, self.ctx.paths.policy_file)
            except (TsunamiError, OSError) as exc:
                self.shell.notify(f"The policy was not saved: {exc}", "error")
                self._update_dirty()
                return False
            self._loaded_policy = new_policy
            self._loaded_enabled = new_policy.enabled
            self._policy_snap = self._policy_fields()
            self._finish_save("policy")
            self._refresh_status()
            saved.append("MCP policy")
        self._update_advanced_summary()
        self._refresh_notice()
        self._update_dirty()
        self.shell.notify(" and ".join(saved) + " saved.")
        return True

    def _finish_save(self, kind: str) -> None:
        self._stamps[kind] = _stamp(self._path(kind))
        self._errors.pop(kind, None)
        self._conflicts.pop(kind, None)

    def save_all(self) -> bool:
        """Write whichever file has unsaved edits; refuses when the file changed on disk."""
        self.check_external()
        keys = SETTINGS_KEYS if self.is_dirty("settings") else ()
        policy = self.is_dirty("policy")
        if not keys and not policy:
            return True
        return self._commit(keys, policy)

    def save_general(self) -> bool:
        return self._commit(GENERAL_KEYS, False)

    def save_errors(self) -> bool:
        return self._commit(ERROR_KEYS, False)

    def save_mcp(self) -> bool:
        return self._commit((), True)

    # -- General -----------------------------------------------------------------------------------

    def _build_general(self) -> None:
        layout = self._section("general", "General")
        form = QFormLayout()
        row = QHBoxLayout()
        self.workspace_edit = QLineEdit()
        self.workspace_edit.textChanged.connect(self._on_edit)
        browse = QPushButton("Browse…")
        browse.clicked.connect(lambda _=False: self._browse_workspace())
        row.addWidget(self.workspace_edit, 1)
        row.addWidget(browse)
        form.addRow("Workspace", row)
        self.theme_combo = wheel.ComboBox()
        self.theme_combo.addItems(["system", "light", "dark"])
        self.theme_combo.currentTextChanged.connect(self._on_edit)
        form.addRow("Theme", self.theme_combo)
        self.log_combo = wheel.ComboBox()
        self.log_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.log_combo.currentTextChanged.connect(self._on_edit)
        form.addRow("Log level", self.log_combo)
        self.retention_spin = wheel.SpinBox()
        self.retention_spin.setRange(1, 1000)
        # pipeline.py: after each pre-change backup, only the newest N .cfg files in that
        # device's backup folder are kept.
        self.retention_spin.setSuffix(" backups per device")
        self.retention_spin.setToolTip(
            "How many config backups Tsunami keeps for each device. A backup is taken before "
            "each change is applied; once a device has more than this, the oldest are deleted."
        )
        self.retention_spin.valueChanged.connect(self._on_edit)
        form.addRow("Backup retention", self.retention_spin)
        self.legacy_ssh_check = QCheckBox("Allow SHA-1 SSH for devices that offer nothing newer")
        self.legacy_ssh_check.setToolTip(
            "SHA-1 algorithms are offered after every modern one, so modern devices still "
            "use modern crypto. Turn off to refuse devices that need SHA-1."
        )
        self.legacy_ssh_check.toggled.connect(self._on_edit)
        form.addRow("SSH", self.legacy_ssh_check)
        lock_row = QHBoxLayout()
        self.auto_lock_spin = wheel.SpinBox()
        self.auto_lock_spin.setRange(0, 480)
        self.auto_lock_spin.setSuffix(" min")
        self.auto_lock_spin.setSpecialValueText("never")
        self.auto_lock_spin.valueChanged.connect(self._on_edit)
        lock_row.addWidget(self.auto_lock_spin)
        self.password_button = QPushButton("Change Tsunami password…")
        self.password_button.setEnabled(getattr(self.ctx, "lock", None) is not None)
        self.password_button.clicked.connect(lambda _=False: self._change_password())
        lock_row.addWidget(self.password_button)
        lock_row.addStretch(1)
        form.addRow("Auto-lock", lock_row)
        layout.addLayout(form)

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

    # -- Advanced: secrets and errors --------------------------------------------------------------

    def _build_errors(self) -> None:
        layout = self._section("errors", "Advanced")
        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText("Secrets and errors")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setAutoRaise(True)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.advanced_toggle.setToolTip("Show or hide these settings")
        self.advanced_toggle.toggled.connect(self.set_advanced_expanded)
        layout.addWidget(self.advanced_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.advanced_summary = QLabel()
        self.advanced_summary.setObjectName("Muted")
        self.advanced_summary.setWordWrap(True)
        layout.addWidget(self.advanced_summary)

        self.advanced_body = QWidget()
        inner = QVBoxLayout(self.advanced_body)
        inner.setContentsMargins(0, 0, 0, 0)
        hint = QLabel(
            "One entry per line. A sink pattern is a regular expression with a "
            "(?P<secret>...) group marking the token. Benign messages are regular "
            "expressions for device output that looks like an error but is not."
        )
        hint.setWordWrap(True)
        inner.addWidget(hint)
        inner.addWidget(QLabel("Extra sink patterns"))
        self.sink_lines = _FamilyLines()
        self.sink_lines.changed.connect(self._on_edit)
        inner.addWidget(self.sink_lines)
        inner.addWidget(QLabel("Benign error messages"))
        self.benign_lines = _FamilyLines()
        self.benign_lines.changed.connect(self._on_edit)
        inner.addWidget(self.benign_lines)
        layout.addWidget(self.advanced_body)
        self.advanced_body.hide()

    def set_advanced_expanded(self, expanded: bool) -> None:
        self.advanced_toggle.blockSignals(True)
        self.advanced_toggle.setChecked(expanded)
        self.advanced_toggle.blockSignals(False)
        self.advanced_toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self.advanced_body.setVisible(expanded)
        self.advanced_summary.setVisible(not expanded)

    def _update_advanced_summary(self) -> None:
        patterns = sum(len(v) for v in self.sink_lines.values().values())
        benign = sum(len(v) for v in self.benign_lines.values().values())
        self.advanced_summary.setText(
            "Extra patterns that mask secrets in device output, and device messages to treat "
            "as harmless. Most setups need neither. "
            f"Now: {patterns} sink pattern{'s' if patterns != 1 else ''}, "
            f"{benign} benign message{'s' if benign != 1 else ''}."
        )

    # -- MCP ---------------------------------------------------------------------------------------

    def _build_mcp(self) -> None:
        layout = self._section("mcp", "MCP")
        warning = QLabel(MCP_WARNING)
        warning.setWordWrap(True)
        warning.setObjectName("McpWarning")
        warning.setStyleSheet("font-weight: bold;")
        layout.addWidget(warning)
        form = QFormLayout()
        self.mcp_enabled = QCheckBox("MCP server enabled")
        self.mcp_enabled.toggled.connect(self._on_edit)
        form.addRow(self.mcp_enabled)
        self.apply_combo = wheel.ComboBox()
        self.apply_combo.addItems(["never", "lab_only"])
        self.apply_combo.currentTextChanged.connect(self._on_edit)
        form.addRow("Apply mode", self.apply_combo)
        self.trust_keys = QCheckBox("Claude may connect to devices whose host key is not on file")
        self.trust_keys.setToolTip(
            "The key is recorded on first contact, as the window's own runs do. A device "
            "whose key has changed is always refused."
        )
        self.trust_keys.toggled.connect(self._on_edit)
        form.addRow("Host keys", self.trust_keys)
        self.lab_drafts = QCheckBox("Lab customers may receive draft plays")
        self.lab_drafts.toggled.connect(self._on_edit)
        form.addRow(self.lab_drafts)
        self.inventory_edits = QCheckBox("Claude may add customers, sites and devices")
        self.inventory_edits.setToolTip(
            "Add only: Claude never edits or deletes inventory. Customers it adds are never "
            "lab customers, and Claude cannot make first contact with a device it added."
        )
        self.inventory_edits.toggled.connect(self._on_edit)
        form.addRow("Inventory", self.inventory_edits)
        layout.addLayout(form)

        layout.addWidget(QLabel("Customers hidden from MCP"))
        self.blocked_boxes: dict[str, QCheckBox] = {}
        self._blocked_layout = QVBoxLayout()
        layout.addLayout(self._blocked_layout)

        layout.addWidget(QLabel("Allowlist requests from Claude"))
        self._requests_layout = QVBoxLayout()
        layout.addLayout(self._requests_layout)
        self._request_timer = QTimer(self)
        self._request_timer.setInterval(2000)
        self._request_timer.timeout.connect(self.refresh_requests)
        self._request_timer.start()

        allow_row = QHBoxLayout()
        allow_row.addWidget(QLabel("Show allowlist (a command prefix per line)"), 1)
        self.open_allowlists_button = QPushButton("Edit allowlists in VS Code")
        self.open_allowlists_button.setToolTip(
            "Open mcp-policy.yml at the show allowlist; the pipe list and hidden customers "
            "are in the same file. Tsunami reloads it when you come back to this tab."
        )
        self.open_allowlists_button.clicked.connect(lambda _=False: self.open_allowlists())
        allow_row.addWidget(self.open_allowlists_button)
        layout.addLayout(allow_row)
        self.allow_lines = _FamilyLines()
        self.allow_lines.changed.connect(self._on_edit)
        layout.addWidget(self.allow_lines)
        layout.addWidget(QLabel("Allowed pipe modifiers (a word per line)"))
        self.pipe_lines = _FamilyLines()
        self.pipe_lines.changed.connect(self._on_edit)
        layout.addWidget(self.pipe_lines)

        limits = QFormLayout()
        self.max_targets = wheel.SpinBox()
        self.max_targets.setRange(1, 200)
        self.max_targets.valueChanged.connect(self._on_edit)
        limits.addRow("Max check targets", self.max_targets)
        self.rate_limit = wheel.SpinBox()
        self.rate_limit.setRange(1, 600)
        self.rate_limit.valueChanged.connect(self._on_edit)
        limits.addRow("Rate limit per minute", self.rate_limit)
        self.expiry = wheel.SpinBox()
        self.expiry.setRange(1, 1440)
        self.expiry.valueChanged.connect(self._on_edit)
        limits.addRow("Approval expiry (minutes)", self.expiry)
        layout.addLayout(limits)

    # -- allowlist requests ------------------------------------------------------------------------

    def refresh_requests(self) -> None:
        """Show the pending show-allowlist requests Claude made, each with Approve and Deny."""
        try:
            pending = self.ctx.history.pending_allowlist_requests()
        except Exception:  # noqa: BLE001 - a busy database must not break the UI
            return
        key = [r["id"] for r in pending]
        if key == getattr(self, "_request_ids", None):
            return
        self._request_ids = key
        self.request_rows: dict[int, QWidget] = {}
        while (item := self._requests_layout.takeAt(0)) is not None:
            if item.widget() is not None:
                item.widget().deleteLater()
        if not pending:
            self._requests_layout.addWidget(QLabel("No pending requests."))
        for req in pending:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            label = QLabel(f"[{req['family']}] {req['command']}: {req['reason']}")
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            h.addWidget(label, 1)
            approve = QPushButton("Approve")
            approve.clicked.connect(lambda _=False, i=req["id"]: self.decide_request(i, True))
            deny = QPushButton("Deny")
            deny.clicked.connect(lambda _=False, i=req["id"]: self.decide_request(i, False))
            h.addWidget(approve)
            h.addWidget(deny)
            self.request_rows[req["id"]] = row
            self._requests_layout.addWidget(row)

    def decide_request(self, request_id: int, approved: bool) -> bool:
        """Approve (adds the command to the policy file) or deny a request from Claude."""
        pending = {r["id"]: r for r in self.ctx.history.pending_allowlist_requests()}
        req = pending.get(request_id)
        if req is None:
            self.refresh_requests()
            return False
        if approved:
            if self.is_dirty("policy") or "policy" in self._errors:
                self.shell.notify(
                    "Save or discard your MCP policy edits before approving a request.", "warning"
                )
                return False
            try:
                added = add_allowlist_entry(
                    self._path("policy"), req["family"], req["command"]
                )
                parsed = load_policy(self._path("policy"))
            except (TsunamiError, OSError) as exc:
                self.shell.notify(f"The policy was not saved: {exc}", "error")
                return False
            self._stamps["policy"] = _stamp(self._path("policy"))
            self._apply_loaded("policy", parsed)
            self._update_dirty()
            if not added:
                self.shell.notify("That command is already on the allowlist.")
        self.ctx.history.decide_allowlist_request(
            request_id, approved=approved, by=self.ctx.windows_user()
        )
        self.refresh_requests()
        return True

    # -- Claude ------------------------------------------------------------------------------------

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
