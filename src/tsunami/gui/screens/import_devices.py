"""Import devices into a customer from a CSV file.

open_import_dialog() picks a file (pick_csv_file, a seam for tests), then shows a dialog
with the column mapping, a "create missing sites" switch and a preview of every row with
its status. Nothing is written until Import; the dialog's save() writes only the rows
that passed, through core.inventory_import, then refreshes the workspace and notifies.

A CSV never carries secrets: a file with a password-like column is refused, and logins
are added in Credentials.

See design.md, Inventory and variables; GUI: Inventory.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tsunami.core.inventory_edit import InventoryEditError
from tsunami.core.inventory_import import (
    ERROR,
    FIELD_LABELS,
    FIELDS,
    OK,
    REQUIRED_FIELDS,
    SKIPPED,
    WARNING,
    ColumnMapping,
    CsvTable,
    ImportPlan,
    ImportRefused,
    ImportSummary,
    apply_import,
    auto_map,
    preview,
    read_csv,
)
from tsunami.core.issues import TsunamiError
from tsunami.core.models.inventory import DEVICE_TYPES
from tsunami.gui import theme, wheel
from tsunami.gui.context import AppContext
from tsunami.gui.shell import Shell

NONE_LABEL = "(none)"
FIXED_LABEL = "Fixed value…"
EXAMPLES = {
    "name": "",
    "host": "",
    "site": "site-a",
    "role": "access",
    "platform": "ios-xe",
    "model": "cisco-c9300",
    "port": "22",
    "credential": "login name",
}
COLUMNS = ("Row", "Name", "Host", "Site", "Type", "Platform", "Status", "Reason")


def run_dialog(dialog: QDialog) -> bool:
    """Show a dialog modally. True if it was accepted. Tests replace this."""
    return dialog.exec() == QDialog.DialogCode.Accepted


def pick_csv_file(parent: QWidget | None = None) -> Path | None:
    """Ask for the CSV file. A seam: tests replace it, so no file dialog opens."""
    name, _ = QFileDialog.getOpenFileName(
        parent,
        "Import devices from CSV",
        "",
        "CSV files (*.csv *.tsv *.txt);;All files (*)",
    )
    return Path(name) if name else None


def open_import_dialog(
    ctx: AppContext,
    shell: Shell,
    customer: str,
    site: str | None = None,
    parent: QWidget | None = None,
) -> bool:
    """Pick a CSV and import its devices into a customer, or one fixed site. True if
    devices were imported."""
    if parent is None and isinstance(shell, QWidget):
        parent = shell
    path = pick_csv_file(parent)
    if path is None:
        return False
    try:
        dialog = ImportDevicesDialog(ctx, shell, customer, path, site=site, parent=parent)
    except TsunamiError as exc:
        shell.notify(str(exc), "error")
        return False
    return run_dialog(dialog)


def _status_color(palette: theme.Palette, status: str) -> str:
    return {
        OK: palette.success,
        WARNING: palette.warning,
        ERROR: palette.error,
        SKIPPED: palette.muted,
    }[status]


class ImportDevicesDialog(QDialog):
    def __init__(
        self,
        ctx: AppContext,
        shell: Shell,
        customer: str,
        path: Path,
        *,
        site: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._loading = False
        if ctx.workspace_path is None:
            raise ImportRefused("No workspace is open.")
        self.ctx, self.shell, self.customer, self.site = ctx, shell, customer, site
        self.root = ctx.workspace_path
        self.plan: ImportPlan | None = None
        self.summary: ImportSummary | None = None
        self.table: CsvTable = read_csv(path)  # raises ImportRefused before any UI exists
        self.known_logins = self._login_names()
        self.setWindowTitle(f"Import devices into {customer}" + (f" / {site}" if site else ""))
        self.setMinimumSize(880, 600)
        self.resize(1120, 700)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(12)

        file_row = QHBoxLayout()
        self.file_label = QLabel()
        self.file_label.setObjectName("Muted")
        self.file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.change_button = QPushButton("Choose another file…")
        self.change_button.clicked.connect(lambda _=False: self.change_file())
        file_row.addWidget(self.file_label, 1)
        file_row.addWidget(self.change_button)
        outer.addLayout(file_row)

        self.combos: dict[str, wheel.ComboBox] = {}
        self.fixed_edits: dict[str, QLineEdit] = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(8)
        for index, name in enumerate(FIELDS):
            grid.addWidget(self._field_cell(name), index % 4, index // 4)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        outer.addLayout(grid)
        hint = QLabel(
            "* required. Each field comes from a CSV column or one fixed value for every row. "
            "Logins are added in Credentials; the CSV only names them."
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        options = QHBoxLayout()
        self.create_sites_check = QCheckBox("Create missing sites")
        self.create_sites_check.toggled.connect(lambda _=False: self.refresh())
        self.problems_check = QCheckBox("Only show rows that need attention")
        self.problems_check.toggled.connect(lambda _=False: self._apply_filter())
        options.addWidget(self.create_sites_check)
        options.addWidget(self.problems_check)
        options.addStretch(1)
        self.summary_label = QLabel()
        options.addWidget(self.summary_label)
        outer.addLayout(options)

        self.preview_table = QTableWidget(0, len(COLUMNS))
        self.preview_table.setHorizontalHeaderLabels(list(COLUMNS))
        self.preview_table.verticalHeader().setVisible(False)
        self.preview_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.preview_table.setWordWrap(False)
        self.preview_table.setShowGrid(False)
        header = self.preview_table.horizontalHeader()
        header.setStretchLastSection(True)
        for column in range(len(COLUMNS) - 1):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((46, 120, 118, 80, 140, 72, 72)):
            self.preview_table.setColumnWidth(column, width)
        outer.addWidget(self.preview_table, 1)

        self.error_label = QLabel()
        self.error_label.setObjectName("FormError")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {theme.current().error};")
        self.error_label.setVisible(False)
        outer.addWidget(self.error_label)

        self.buttons = QDialogButtonBox()
        self.import_button = QPushButton("Import")
        self.import_button.setProperty("primary", True)
        self.import_button.setDefault(True)
        self.buttons.addButton(self.import_button, QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._ok)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)

        self._load_table(self.table)

    # -- building ----------------------------------------------------------------------

    def _field_cell(self, name: str) -> QWidget:
        cell = QWidget()
        row = QHBoxLayout(cell)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        label = QLabel(FIELD_LABELS[name] + (" *" if name in REQUIRED_FIELDS else ""))
        label.setFixedWidth(64)
        combo = wheel.ComboBox()
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(14)
        combo.currentIndexChanged.connect(lambda _=0, n=name: self._mapping_changed(n))
        edit = QLineEdit()
        edit.setPlaceholderText(f"e.g. {EXAMPLES[name]}" if EXAMPLES[name] else "value")
        edit.setVisible(False)
        edit.textChanged.connect(lambda _="": self.refresh())
        row.addWidget(label)
        row.addWidget(combo, 1)
        row.addWidget(edit, 1)
        self.combos[name], self.fixed_edits[name] = combo, edit
        return cell

    def _login_names(self) -> list[str] | None:
        try:
            return list(self.ctx.secrets.list_logins())
        except TsunamiError:
            return None  # cannot list: skip the check rather than warn on every row

    def _load_table(self, table: CsvTable) -> None:
        self.table = table
        delimiter = {",": "comma", ";": "semicolon", "\t": "tab"}[table.delimiter]
        count = len(table.rows)
        self.file_label.setText(
            f"{table.path.name}: {count} row{'' if count == 1 else 's'}, {delimiter} separated"
        )
        self.file_label.setToolTip(str(table.path))
        auto = auto_map(table.headers)
        self._loading = True
        try:
            for name, combo in self.combos.items():
                combo.clear()
                combo.addItem(NONE_LABEL, None)
                for header in table.headers:
                    if header:
                        combo.addItem(header, header)
                combo.addItem(FIXED_LABEL, None)
                header = auto.columns.get(name)
                combo.setCurrentIndex(combo.findData(header) if header else 0)
                combo.setEnabled(True)
                self.fixed_edits[name].clear()
            if self.site:
                self.set_fixed("site", self.site)
                self.combos["site"].setEnabled(False)
                self.fixed_edits["site"].setEnabled(False)
            else:
                self.fixed_edits["site"].setEnabled(True)
        finally:
            self._loading = False
        for name in FIELDS:
            self._show_fixed(name)
        self.refresh()

    # -- mapping -----------------------------------------------------------------------

    def set_fixed(self, name: str, value: str) -> None:
        """Use one fixed value for a field in every row."""
        combo = self.combos[name]
        combo.setCurrentIndex(combo.count() - 1)
        self.fixed_edits[name].setText(value)

    def set_column(self, name: str, header: str | None) -> None:
        """Take a field from a CSV column (header name), or from none (None)."""
        combo = self.combos[name]
        index = combo.findData(header) if header else 0
        if index < 0:
            raise ValueError(f"no column {header!r}")
        combo.setCurrentIndex(index)

    def _is_fixed(self, name: str) -> bool:
        combo = self.combos[name]
        return combo.currentIndex() == combo.count() - 1

    def _show_fixed(self, name: str) -> None:
        self.fixed_edits[name].setVisible(self._is_fixed(name))

    def _mapping_changed(self, name: str) -> None:
        if self._loading:
            return
        self._show_fixed(name)
        self.refresh()

    def mapping(self) -> ColumnMapping:
        mapping = ColumnMapping()
        for name in FIELDS:
            combo = self.combos[name]
            if self._is_fixed(name):
                value = self.fixed_edits[name].text().strip()
                if value:
                    mapping.fixed[name] = value
            elif combo.currentData():
                mapping.columns[name] = combo.currentData()
        return mapping

    # -- preview -----------------------------------------------------------------------

    def refresh(self) -> None:
        if self._loading:
            return
        self.set_error("")
        try:
            self.plan = preview(
                self.root,
                self.customer,
                self.table,
                self.mapping(),
                known_logins=self.known_logins,
                create_sites=self.create_sites_check.isChecked(),
            )
        except TsunamiError as exc:
            self.plan = None
            self.set_error(str(exc))
        self._fill_preview()

    def _fill_preview(self) -> None:
        palette = theme.current()
        plan = self.plan
        rows = plan.rows if plan is not None else []
        table = self.preview_table
        table.setUpdatesEnabled(False)
        table.setRowCount(len(rows))
        bold = QFont(table.font())
        bold.setBold(True)
        for index, row in enumerate(rows):
            f = row.fields
            role = f["role"]
            values = (
                str(row.number),
                f["name"] or "",
                f["host"] or "",
                f["site"] or "",
                DEVICE_TYPES.get(role, role) if role else "",
                f["platform"] or "",
                row.status,
                row.reason,
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setToolTip(row.reason if column >= 6 and row.reason else text)
                if column == 0:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                if column == 6:
                    item.setForeground(QBrush(QColor(_status_color(palette, row.status))))
                    item.setFont(bold)
                elif column == 7 and row.status in (ERROR, WARNING):
                    item.setForeground(QBrush(QColor(_status_color(palette, row.status))))
                table.setItem(index, column, item)
        table.setUpdatesEnabled(True)
        self._apply_filter()
        self._update_summary()

    def _apply_filter(self) -> None:
        only = self.problems_check.isChecked()
        for index in range(self.preview_table.rowCount()):
            status = self.preview_table.item(index, 6).text()
            self.preview_table.setRowHidden(index, only and status in (OK, SKIPPED))

    def _update_summary(self) -> None:
        plan = self.plan
        ready = plan.ready if plan is not None else 0
        if plan is None:
            self.summary_label.setText("")
        else:
            parts = [f"{ready} ready"]
            if plan.warnings:
                parts.append(f"{plan.warnings} warning{'' if plan.warnings == 1 else 's'}")
            if plan.skipped:
                parts.append(f"{plan.skipped} already present")
            if plan.errors:
                parts.append(f"{plan.errors} error{'' if plan.errors == 1 else 's'}")
            if plan.new_sites:
                parts.append(
                    f"new site{'' if len(plan.new_sites) == 1 else 's'}: "
                    + ", ".join(plan.new_sites)
                )
            self.summary_label.setText(", ".join(parts))
        self.import_button.setText(
            f"Import {ready} device{'' if ready == 1 else 's'}" if ready else "Import"
        )
        self.import_button.setEnabled(ready > 0)

    def set_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def change_file(self) -> bool:
        path = pick_csv_file(self)
        if path is None:
            return False
        try:
            table = read_csv(path)
        except ImportRefused as exc:
            self.set_error(str(exc))
            return False
        self._load_table(table)
        return True

    # -- importing ---------------------------------------------------------------------

    def _ok(self) -> None:
        if self.save():
            self.accept()

    def save(self) -> bool:
        """Write the rows that passed. On refusal show it inline and keep the dialog."""
        self.set_error("")
        if self.plan is None or not self.plan.ready:
            self.set_error("There is nothing to import.")
            return False
        try:
            self.summary = apply_import(self.root, self.customer, self.plan)
        except (InventoryEditError, TsunamiError, OSError) as exc:
            self.set_error(str(exc))
            return False
        self.shell.refresh_all()
        self.shell.notify(self.summary.message())
        return True
