"""Restore from backup: a dialog that says where and how to reach the device, then a tab
that runs the restore (core/restore.py) and shows what the device did with each line.

The device is reached over SSH at an address the person gives (the inventory's, by
default, through the device's jump host if it has one), or over a serial console. The
login is one of the saved logins (the device's own first), a username and password typed
for this restore only, or, over a console, none: a factory-new Cisco switch has no login.
Over a console the transport answers a factory-new device's setup prompts, and on Aruba
the login's password becomes the admin or manager password.

Nothing is sent before the person confirms a prompt that names the backup, the device and
the way in. Closing the tab does not stop a restore (that would leave the device half
done); Cancel stops it between lines.

Everything shown is redacted by the job, which also masks the login used.

See design.md, Backups: Restore from backup.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from tsunami.core.backups import BackupEntry, BackupError, read_for_restore
from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import PLATFORM_FAMILY, PlatformFamily
from tsunami.core.models.jobs import DeviceStatus
from tsunami.core.models.play import ConnectionMethod
from tsunami.core.redact import Redactor
from tsunami.core.restore import (
    FAMILY_BAUD,
    RestoreJob,
    RestorePlan,
    RestoreRequest,
    RestoreResult,
    address_in_config,
    plan_restore,
)
from tsunami.core.secret_store import LoginCredential
from tsunami.gui import theme
from tsunami.gui.context import AppContext
from tsunami.gui.icons import icon
from tsunami.gui.shell import Shell
from tsunami.gui.widgets.masked_text import MaskedTextView
from tsunami.gui.widgets.port_picker import PortPicker

BAUDS = (9600, 19200, 38400, 57600, 115200)
TYPED = "typed"
SAVED = "saved"
NO_LOGIN = "none"
SSH_HINT = (
    "The device needs an address, SSH and a login already. For a factory-new device, "
    "use the console."
)
CONSOLE_HINT = {
    PlatformFamily.IOS: (
        "Tsunami answers a factory-new switch's setup prompts: it declines the initial "
        "configuration dialog and stops autoinstall. A new switch needs no login; one "
        "that has a login set logs in with it."
    ),
    PlatformFamily.AOS_CX: (
        "A factory-new switch logs in as admin with no password and must set one: "
        "Tsunami sets it to the chosen login's password. A switch that has an admin "
        "password logs in with the login."
    ),
    PlatformFamily.AOS_S: (
        "A factory-new switch either has no password or asks for manager credentials at "
        "first login: Tsunami gives it the chosen login's. A switch that has a login logs "
        "in with it."
    ),
}


def run_dialog(dialog: QDialog) -> bool:
    """Show a modal dialog. A seam: tests replace it."""
    return dialog.exec() == QDialog.DialogCode.Accepted


def confirm(parent: QWidget | None, title: str, text: str) -> bool:
    """Ask before sending anything to the device. A seam: tests replace it."""
    answer = QMessageBox.question(
        parent,
        title,
        text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        QMessageBox.StandardButton.Cancel,
    )
    return answer == QMessageBox.StandardButton.Yes


def _label(entry: BackupEntry) -> str:
    from tsunami.gui.screens.backups import BackupsTab

    return BackupsTab._label(entry)


class RestoreDialog(QDialog):
    """Where the device is and how to log in. request holds the answer once accepted."""

    def __init__(
        self,
        ctx: AppContext,
        customer: str,
        device: str,
        entry: BackupEntry,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.ctx, self.customer, self.device, self.entry = ctx, customer, device, entry
        self.request: RestoreRequest | None = None
        inv = ctx.workspace.customers[customer]
        self.dev = inv.device(device)
        self.family = PLATFORM_FAMILY[self.dev.platform]
        self.device_login = inv.credential_for(self.dev)
        self.setWindowTitle("Restore from backup")
        self.setMinimumWidth(520)
        try:
            raw = read_for_restore(ctx.paths, customer, device, entry.path)
        except BackupError:
            raw = ""
        self._raw = raw
        self.plan: RestorePlan = plan_restore(raw, self.dev.platform)
        try:
            self.jump = ctx.jump_targets(customer, [device]).get(device)
            jump_problem = ""
        except TsunamiError as exc:
            self.jump, jump_problem = None, str(exc)

        layout = QVBoxLayout(self)
        heading = QLabel(f"Restore {device} from the backup taken {_label(entry)}")
        heading.setObjectName("Section")
        heading.setTextFormat(Qt.TextFormat.PlainText)
        heading.setWordWrap(True)
        layout.addWidget(heading)
        counts = f"{self.plan.line_count} lines to send ({self.dev.platform})"
        if self.plan.skipped:
            counts += (
                f"; {len(self.plan.skipped)} not sent: guarded commands that need a reboot, "
                "change how credentials are stored, or could cut the console session"
            )
        self.counts_label = QLabel(counts)
        self.counts_label.setObjectName("Muted")
        self.counts_label.setWordWrap(True)
        layout.addWidget(self.counts_label)

        method_row = QHBoxLayout()
        self.ssh_radio = QRadioButton("Network (SSH)")
        self.console_radio = QRadioButton("Console")
        self._methods = QButtonGroup(self)
        self._methods.addButton(self.ssh_radio)
        self._methods.addButton(self.console_radio)
        self.ssh_radio.setChecked(True)
        method_row.addWidget(QLabel("Connect over"))
        method_row.addWidget(self.ssh_radio)
        method_row.addWidget(self.console_radio)
        method_row.addStretch(1)
        layout.addLayout(method_row)

        self.where_stack = QStackedWidget()
        ssh_page = QWidget()
        ssh_form = QFormLayout(ssh_page)
        ssh_form.setContentsMargins(0, 0, 0, 0)
        self.host_edit = QLineEdit(str(self.dev.host))
        self.host_edit.textChanged.connect(lambda _t: self._update())
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(int(self.dev.port))
        ssh_form.addRow("Address", self.host_edit)
        ssh_form.addRow("Port", self.port_spin)
        self.jump_box = QCheckBox(
            f"Through jump host {self.jump.host}" if self.jump else "Through jump host"
        )
        self.jump_box.setChecked(self.jump is not None)
        self.jump_box.setVisible(self.jump is not None or bool(jump_problem))
        self.jump_box.setEnabled(self.jump is not None)
        if jump_problem:
            self.jump_box.setToolTip(f"The jump host's login cannot be read: {jump_problem}")
        ssh_form.addRow("", self.jump_box)
        console_page = QWidget()
        console_form = QFormLayout(console_page)
        console_form.setContentsMargins(0, 0, 0, 0)
        self.port_picker = PortPicker()
        self.baud_combo = QComboBox()
        for baud in BAUDS:
            self.baud_combo.addItem(str(baud), baud)
        self.baud_combo.setCurrentIndex(BAUDS.index(self._default_baud()))
        console_form.addRow("Serial port", self.port_picker)
        console_form.addRow("Speed", self.baud_combo)
        self.where_stack.addWidget(ssh_page)
        self.where_stack.addWidget(console_page)
        layout.addWidget(self.where_stack)

        login_form = QFormLayout()
        self.login_combo = QComboBox()
        logins = self._saved_logins()
        for name in logins:
            mark = "  (this device's login)" if name == self.device_login else ""
            self.login_combo.addItem(name + mark, (SAVED, name))
        self.login_combo.addItem("Type a username and password", (TYPED, ""))
        self.login_combo.addItem(
            "No login (a factory-new Cisco switch on the console)", (NO_LOGIN, "")
        )
        self.login_combo.currentIndexChanged.connect(lambda _i: self._update())
        login_form.addRow("Login", self.login_combo)
        self.username_edit = QLineEdit()
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.enable_edit = QLineEdit()
        self.enable_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.enable_edit.setPlaceholderText("only if it differs from the password")
        self._typed_rows = []
        for label, edit in (
            ("Username", self.username_edit),
            ("Password", self.password_edit),
            ("Enable password", self.enable_edit),
        ):
            row_label = QLabel(label)
            login_form.addRow(row_label, edit)
            self._typed_rows.append((row_label, edit))
        layout.addLayout(login_form)

        self.hint_label = QLabel()
        self.hint_label.setObjectName("Muted")
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)
        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setTextFormat(Qt.TextFormat.PlainText)
        self.warning_label.setStyleSheet(f"color: {theme.current().warning}")
        layout.addWidget(self.warning_label)

        self.save_box = QCheckBox("Save to startup config afterwards (write memory)")
        self.save_box.setChecked(True)
        layout.addWidget(self.save_box)
        self.keys_box = QCheckBox("Create SSH host keys if the switch has none")
        self.keys_box.setChecked(True)
        self.keys_box.setVisible(self.family is PlatformFamily.IOS)
        layout.addWidget(self.keys_box)

        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setStyleSheet(f"color: {theme.current().error}")
        layout.addWidget(self.error_label)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.restore_button = QPushButton("Restore…")
        self.restore_button.setProperty("primary", True)
        self.buttons.addButton(self.restore_button, QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.ssh_radio.toggled.connect(lambda _on: self._update())
        self._update()

    # -- state ---------------------------------------------------------------------------

    def _saved_logins(self) -> list[str]:
        try:
            names = sorted(self.ctx.secrets.list_logins())
        except Exception:  # noqa: BLE001 - the dialog still offers a typed login
            names = []
        if self.device_login in names:
            names.remove(self.device_login)
            names.insert(0, self.device_login)
        return names

    def _default_baud(self) -> int:
        baud = FAMILY_BAUD[self.family]
        model = getattr(self.dev, "model", None)
        if model:
            try:
                from tsunami.core.catalog import load_catalog

                baud = load_catalog().get(model).console_baud
            except Exception:  # noqa: BLE001 - unknown model: the platform's speed
                pass
        return baud if baud in BAUDS else FAMILY_BAUD[self.family]

    def method(self) -> ConnectionMethod:
        return ConnectionMethod.SSH if self.ssh_radio.isChecked() else ConnectionMethod.SERIAL

    def login_choice(self) -> tuple[str, str]:
        data = self.login_combo.currentData()
        return (data[0], data[1]) if data else (NO_LOGIN, "")

    def choose_login(self, kind: str, name: str = "") -> bool:
        """Select a login entry: (SAVED, name), (TYPED, "") or (NO_LOGIN, "")."""
        for i in range(self.login_combo.count()):
            data = self.login_combo.itemData(i)
            if data and (data[0], data[1]) == (kind, name):
                self.login_combo.setCurrentIndex(i)
                return True
        return False

    def _update(self) -> None:
        ssh = self.method() == ConnectionMethod.SSH
        self.where_stack.setCurrentIndex(0 if ssh else 1)
        typed = self.login_choice()[0] == TYPED
        for label, edit in self._typed_rows:
            label.setVisible(typed)
            edit.setVisible(typed)
        self.hint_label.setText(SSH_HINT if ssh else CONSOLE_HINT[self.family])
        host = self.host_edit.text().strip()
        if ssh and host and self._raw and not address_in_config(self._raw, host):
            self.warning_label.setText(
                f"The backup does not give {host} to any interface. If the restore changes "
                "the address you connect through, the session ends part way; restore over "
                "the console instead."
            )
        else:
            self.warning_label.setText("")
        self.warning_label.setVisible(bool(self.warning_label.text()))
        self.error_label.setText("")

    # -- the answer ----------------------------------------------------------------------

    def build_request(self) -> RestoreRequest | str:
        """The request, or why it cannot be made."""
        if not self.plan.line_count:
            return "The backup has no configuration lines to send."
        method = self.method()
        kind, name = self.login_choice()
        credential: LoginCredential | None = None
        label = "no login"
        if kind == SAVED:
            try:
                credential = self.ctx.secrets.get_login(name)
            except Exception:  # noqa: BLE001 - locked or missing: say so, never the error
                return f"The login {name} cannot be read. Unlock Tsunami, or check it is saved."
            label = f"login {name}"
        elif kind == TYPED:
            username = self.username_edit.text().strip()
            if not username:
                return "Type the username."
            credential = LoginCredential(
                username, self.password_edit.text(), self.enable_edit.text() or None
            )
            label = f"typed for this restore (user {username})"
        if method == ConnectionMethod.SSH:
            host = self.host_edit.text().strip()
            if not host or any(c.isspace() for c in host):
                return "Type the device's address."
            if credential is None:
                return "SSH needs a login. Choose one, or type a username and password."
            jump = self.jump if self.jump_box.isChecked() else None
            return self._request(
                method, credential, label, host=host, port=self.port_spin.value(), jump=jump
            )
        port = self.port_picker.port()
        if not port:
            return "Choose a serial port. Is the console cable plugged in?"
        if credential is None and self.family is not PlatformFamily.IOS:
            return "An Aruba switch needs a login: its password becomes the admin password."
        return self._request(
            method, credential, label, serial_port=port, baud=self.baud_combo.currentData()
        )

    def _request(
        self, method: ConnectionMethod, credential: Any, label: str, **where: Any
    ) -> RestoreRequest:
        return RestoreRequest(
            customer=self.customer,
            device=self.device,
            platform=self.dev.platform,
            backup=self.entry.path,
            method=method,
            credential=credential,
            login_label=label,
            save=self.save_box.isChecked(),
            ssh_keys=self.keys_box.isChecked() and self.family is PlatformFamily.IOS,
            **where,
        )

    def accept(self) -> None:
        built = self.build_request()
        if isinstance(built, str):
            self.error_label.setText(built)
            return
        self.request = built
        super().accept()

    def forget_typed(self) -> None:
        """Clear the typed password fields; the request keeps its own copy."""
        for edit in (self.password_edit, self.enable_edit):
            edit.clear()

    def confirm_text(self) -> str:
        req = self.request
        assert req is not None  # noqa: S101 - only called once accepted
        text = (
            f"Send {self.plan.line_count} lines from the backup taken {_label(self.entry)} "
            f"to {self.device} over {req.where}?\n\n"
            "A restore adds and changes configuration: lines on the device that the backup "
            "does not have are kept. What the device has now is saved as a backup first."
        )
        if not req.save:
            text += "\n\nThe result is not saved to startup, so a reload undoes it."
        return text


def start_restore(
    ctx: AppContext,
    shell: Shell,
    customer: str,
    device: str,
    entry: BackupEntry,
    parent: QWidget | None = None,
) -> RestoreTab | None:
    """Ask where and how, confirm, then open a tab that runs the restore."""
    from tsunami.gui.screens.backups import redactor_or_reason

    redactor = redactor_or_reason(ctx)
    if isinstance(redactor, str):
        shell.notify(redactor.replace("view backups", "restore a backup"), "warning")
        return None
    ws = ctx.workspace
    if ws is None or customer not in ws.customers or ws.customers[customer].device(device) is None:
        shell.notify(f"{device} is not in the loaded workspace.", "error")
        return None
    key = f"restore:{customer}/{device}"
    dialog = RestoreDialog(ctx, customer, device, entry, parent)
    try:
        accepted = run_dialog(dialog) and dialog.request is not None
        request = dialog.request
        prompt = dialog.confirm_text() if accepted else ""
    finally:
        dialog.forget_typed()  # the dialog outlives exec(); keep no password in it
        dialog.deleteLater()
    if not accepted or request is None:
        return None
    if not confirm(parent, "Restore from backup", prompt):
        return None
    created: list[RestoreTab] = []

    def make() -> RestoreTab:
        created.append(RestoreTab(ctx, shell, request, entry, redactor))
        return created[-1]

    tab = shell.open_tab(key, f"Restore: {device}", make, "history")
    if not created:  # the tab of an earlier restore of this device
        if isinstance(tab, RestoreTab) and tab.running:
            shell.notify(f"A restore of {device} is still running.", "warning")
            return tab
        shell.close_tab(key)
        tab = shell.open_tab(key, f"Restore: {device}", make, "history")
    if not isinstance(tab, RestoreTab):
        return None
    tab.start()
    return tab


class _Signals(QObject):
    log = Signal(str)
    progress = Signal(int, int)
    finished = Signal(object)  # RestoreResult | Exception


class _Task(QRunnable):
    def __init__(self, signals: _Signals, job: RestoreJob, transport: Any) -> None:
        super().__init__()
        self.signals, self.job, self.transport = signals, job, transport

    def run(self) -> None:  # worker thread
        result: Any
        try:
            result = self.job.run()
        except Exception as exc:  # noqa: BLE001 - reported to the person
            result = exc
        finally:
            close = getattr(self.transport, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001, S110 - closing is best effort
                    pass
        self.signals.finished.emit(result)


class RestoreTab(QWidget):
    def __init__(
        self,
        ctx: AppContext,
        shell: Shell,
        request: RestoreRequest,
        entry: BackupEntry,
        redactor: Redactor,
    ) -> None:
        super().__init__()
        self.ctx, self.shell, self.request, self.entry = ctx, shell, request, entry
        self.redactor = redactor
        self.running = False
        self.job: RestoreJob | None = None
        self.result: RestoreResult | None = None
        self._signals = _Signals()
        self._signals.log.connect(self._on_log)
        self._signals.progress.connect(self._on_progress)
        self._signals.finished.connect(self._on_finished)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(8)
        title = QLabel(f"Restore {request.device}")
        title.setObjectName("Title")
        layout.addWidget(title)
        subtitle = QLabel(
            f"{request.customer}  |  backup {_label(entry)}  |  {request.where}  |  "
            f"{request.login_label}"
        )
        subtitle.setObjectName("Muted")
        subtitle.setTextFormat(Qt.TextFormat.PlainText)
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        bar = QHBoxLayout()
        self.status_label = QLabel("Starting…")
        self.status_label.setObjectName("Section")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        bar.addWidget(self.status_label, 1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setIcon(icon("debug-stop", theme.current().muted))
        self.cancel_button.setToolTip("Stop between lines; the device is left partly restored")
        self.cancel_button.clicked.connect(lambda _=False: self.cancel())
        self.job_button = QPushButton("Open job")
        self.job_button.setIcon(icon("history", theme.current().muted))
        self.job_button.clicked.connect(lambda _=False: self.open_job())
        self.backups_button = QPushButton("Backups")
        self.backups_button.setIcon(icon("archive", theme.current().muted))
        self.backups_button.clicked.connect(lambda _=False: self.open_backups())
        for button in (self.cancel_button, self.job_button, self.backups_button):
            bar.addWidget(button)
        layout.addLayout(bar)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # busy until the first line is sent
        self.progress.setTextVisible(True)
        layout.addWidget(self.progress)
        self.log_view = MaskedTextView()
        layout.addWidget(self.log_view, 1)
        self.job_button.setEnabled(False)

    def start(self) -> bool:
        if self.running:
            return False
        transport = self.ctx.make_transport()
        try:
            self.job = RestoreJob(
                self.ctx.paths,
                self.request,
                transport=transport,
                redactor=self.redactor,
                history=self.ctx.history,
                keep=self.ctx.settings.backup_retention,
                on_log=self._signals.log.emit,
                on_progress=self._signals.progress.emit,
            )
        except TsunamiError as exc:
            self._finished_with(str(exc), theme.current().error)
            return False
        self.running = True
        self.cancel_button.setEnabled(True)
        QThreadPool.globalInstance().start(_Task(self._signals, self.job, transport))
        return True

    def cancel(self) -> None:
        if self.job is not None and self.running:
            self.job.cancel()
            self.status_label.setText("Cancelling after the current line…")

    def open_job(self) -> None:
        if self.result is not None and self.result.job_id:
            self.shell.open_job(self.result.job_id)

    def open_backups(self) -> None:
        from tsunami.gui.screens.backups import open_backups

        open_backups(self.ctx, self.shell, self.request.customer, self.request.device)

    def _on_log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.append_text(f"{stamp}  {text}")
        if self.running and not self.status_label.text().startswith("Cancelling"):
            self.status_label.setText(text.splitlines()[0][:200] if text else "")

    def _on_progress(self, sent: int, total: int) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(sent)
        self.progress.setFormat(f"{sent} of {total} lines sent")

    def _on_finished(self, result: Any) -> None:
        self.running = False
        colors = theme.current()
        if isinstance(result, Exception):
            self._finished_with(f"Restore failed: {self.redactor.text(str(result))}", colors.error)
            return
        self.result = result
        self.job_button.setEnabled(bool(result.job_id))
        color = {
            DeviceStatus.SUCCESS: colors.success,
            DeviceStatus.CANCELLED: colors.warning,
        }.get(result.status, colors.error)
        self.log_view.append_text("\n" + result.report())
        self._finished_with(result.summary, color)
        if result.total:
            self.progress.setRange(0, result.total)
            self.progress.setValue(result.sent)
        self.ctx.jobs_changed.emit()  # History, Last job, the backups list

    def _finished_with(self, text: str, color: str) -> None:
        self.running = False
        self.cancel_button.setEnabled(False)
        if self.progress.maximum() == 0:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color}")
