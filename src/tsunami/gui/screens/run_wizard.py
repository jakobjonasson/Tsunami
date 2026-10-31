"""Run wizard: targets, prompts, mode, tag filter, masked preview.

One scrolling form per play: pick targets, answer prompts (validated as you type), choose
a mode and tags, preview the masked render for every device, then Start. Start only
works on inputs that have just been previewed; any change disables it again.

Initial (day-0) plays ask in two stages: model, site and role first, then the rest,
pre-filled from variables those answers resolve.

See design.md, GUI: Views (Run wizard); Play schema: prompts, Run-time options.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from tsunami.core.catalog import load_catalog
from tsunami.core.issues import TsunamiError
from tsunami.core.logins import allowed_logins
from tsunami.core.models.common import PLATFORM_FAMILY, Platform, PlatformFamily
from tsunami.core.models.inventory import Device
from tsunami.core.models.play import GLOBAL_CUSTOMER, ConnectionMethod, PlayType, RunMode
from tsunami.core.prompts import PromptContext, PromptValueError, coerce
from tsunami.core.render.renderer import Renderer
from tsunami.core.runner import RunRequest
from tsunami.core.services import FIRST_STAGE, collect_prompts, play_entry
from tsunami.core.targets import resolve_targets
from tsunami.gui import theme, wheel
from tsunami.gui.context import AppContext
from tsunami.gui.shell import Shell
from tsunami.gui.widgets.masked_text import MaskedTextView
from tsunami.gui.widgets.port_picker import PortPicker
from tsunami.gui.widgets.prompt_widgets import PromptField, make_prompt_widget

MODE_LABELS = {
    RunMode.CHECK: "Check (diff only, no changes)",
    RunMode.APPLY: "Apply (change the devices)",
    RunMode.RENDER: "Render (no connection)",
    RunMode.COMPLIANCE: "Compliance (drift report, no changes)",
}


def open_run_wizard(
    ctx: AppContext,
    shell: Shell,
    play_path: Path | str,
    devices: list[str] | None = None,
    customer: str | None = None,
    *,
    prompts: dict[str, Any] | None = None,
    mode: RunMode | None = None,
) -> QWidget | None:
    """Open (or focus) the run wizard tab for a play. With devices, only those start ticked.
    For a global play, customer preselects the Customer dropdown. prompts and mode fill
    the form from an earlier job (the job view's fallback when it cannot rebuild one)."""
    if ctx.workspace is None:
        shell.notify("Open a workspace first.", "warning")
        return None
    path = Path(play_path)
    if path.is_absolute() and ctx.workspace_path is not None:
        try:
            path = path.relative_to(ctx.workspace_path)
        except ValueError:
            pass
    raw = ctx.workspace.plays.get(path)
    if raw is None or raw.play is None:
        shell.notify(
            f"{path.as_posix()} failed validation" if raw else f"no play at {path.as_posix()}",
            "error",
        )
        return None
    if raw.play.customer == GLOBAL_CUSTOMER:
        if customer is not None and customer not in ctx.workspace.customers:
            customer = None
    else:
        customer = None
        try:
            play_entry(ctx.workspace, path)
        except TsunamiError as exc:
            shell.notify(str(exc), "error")
            return None
    tab = shell.open_tab(
        f"run-wizard:{path.as_posix()}",
        f"Run: {raw.play.name}",
        lambda: RunWizard(ctx, shell, path, customer),
        "play",
    )
    if customer is not None and isinstance(tab, RunWizard):
        tab.set_customer(customer)
    if devices is not None and isinstance(tab, RunWizard):
        tab.preselect_devices(devices)
    if prompts and isinstance(tab, RunWizard):
        tab.fill_prompts(prompts)
    if mode is not None and isinstance(tab, RunWizard):
        tab.set_mode(mode)
    return tab


def _card(title: str) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("Card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 12, 14, 12)
    heading = QLabel(title)
    heading.setStyleSheet("font-weight: 600;")
    layout.addWidget(heading)
    return frame, layout


class RunWizard(QWidget):
    def __init__(
        self, ctx: AppContext, shell: Shell, play_path: Path, customer: str | None = None
    ) -> None:
        super().__init__()
        assert ctx.workspace is not None
        self.ctx = ctx
        self.shell = shell
        self.path = play_path
        raw = ctx.workspace.plays[play_path]
        assert raw.play is not None
        self.is_global = raw.play.customer == GLOBAL_CUSTOMER
        # A global play is unbound until a customer is chosen; then entry/play/cust follow it.
        self.entry = raw if self.is_global else play_entry(ctx.workspace, play_path)
        play = self.entry.play
        assert play is not None
        self.play = play
        self.cust: Any = None if self.is_global else ctx.workspace.customer(play.customer)
        self.customer_combo: QComboBox | None = None
        self.catalog = load_catalog()
        self.initial = play.type == PlayType.INITIAL
        self.devices: tuple[Device, ...] = ()
        self.target_error = ""

        self.fields: dict[str, PromptField] = {}  # shared prompts
        self.device_fields: dict[tuple[str, str], PromptField] = {}  # per-device prompts
        self._device_rows: dict[str, list[QWidget]] = {}
        self._user_edited: set[str] = set()
        self._prefilling = False
        self._last_stage1: dict[str, Any] | None = None
        self._previewed = False
        self.port_picker: PortPicker | None = None
        self.target_list = QListWidget()  # replaced by _build_targets once bound

        self._build()
        if not self.is_global:
            self._bind_content()
        elif customer is not None:
            self.set_customer(customer)
        self._update_buttons()

    def customer_name(self) -> str | None:
        """The customer the wizard runs for; None while a global play has none chosen."""
        return None if self.cust is None else self.play.customer

    def set_customer(self, customer: str) -> None:
        """Choose the customer of a global play (rebinds and reloads everything)."""
        if self.customer_combo is None:
            return
        index = self.customer_combo.findData(customer)
        if index >= 0:
            self.customer_combo.setCurrentIndex(index)

    def _customer_changed(self, _index: int = 0) -> None:
        assert self.customer_combo is not None and self.ctx.workspace is not None
        customer = self.customer_combo.currentData()
        raw = self.ctx.workspace.plays[self.path]
        if customer is None:
            self.entry, self.cust = raw, None
            assert raw.play is not None
            self.play = raw.play
        else:
            try:
                self.entry = play_entry(self.ctx.workspace, self.path, customer)
            except TsunamiError as exc:
                self.shell.notify(str(exc), "error")
                return
            assert self.entry.play is not None
            self.play = self.entry.play
            self.cust = self.ctx.workspace.customer(customer)
        self._bind_content()
        self._update_buttons()

    def _bind_content(self) -> None:
        """(Re)build targets, prompts, options and preview for the bound customer."""
        while self._holder_layout.count():
            child = self._holder_layout.takeAt(0).widget()
            if child is not None:
                child.setParent(None)
                child.deleteLater()
        self.fields, self.device_fields, self._device_rows = {}, {}, {}
        self._user_edited = set()
        self._prefilling = False
        self._last_stage1 = None
        self._previewed = False
        self.devices = ()
        self.target_error = ""
        self.port_picker = None
        self.target_list = QListWidget()
        self.subtitle.setText(self._subtitle_text())
        if self.cust is None:
            hint = QLabel("Choose the customer to run this play for.")
            hint.setObjectName("Muted")
            self._holder_layout.addWidget(hint)
            return
        self._holder_layout.addWidget(self._build_targets())
        self._holder_layout.addWidget(self._build_prompts())
        self._holder_layout.addWidget(self._build_options())
        self._holder_layout.addWidget(self._build_preview())
        self._update_stages()

    def _subtitle_text(self) -> str:
        play = self.play
        who = "any customer" if self.cust is None else play.customer
        text = f"{play.type} play for {who}"
        if self.is_global and self.cust is not None:
            text += " (global play)"
        return text + (f" - {play.description}" if play.description else "")

    # -- construction ---------------------------------------------------------------

    def _build(self) -> None:
        play = self.play
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(20, 16, 20, 16)
        col.setSpacing(12)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        title = QLabel(play.name)
        title.setObjectName("Title")
        col.addWidget(title)
        self.subtitle = QLabel(self._subtitle_text())
        self.subtitle.setObjectName("Muted")
        self.subtitle.setWordWrap(True)
        col.addWidget(self.subtitle)

        if self.is_global:
            frame, card = _card("Customer")
            self.customer_combo = wheel.ComboBox()
            self.customer_combo.setObjectName("Customer")
            self.customer_combo.addItem("Choose a customer…", None)
            for name in sorted(self.ctx.workspace.customers):  # type: ignore[union-attr]
                self.customer_combo.addItem(name, name)
            self.customer_combo.currentIndexChanged.connect(self._customer_changed)
            card.addWidget(self.customer_combo)
            col.addWidget(frame)

        holder = QWidget()
        self._holder_layout = QVBoxLayout(holder)
        self._holder_layout.setContentsMargins(0, 0, 0, 0)
        self._holder_layout.setSpacing(12)
        col.addWidget(holder)
        col.addStretch(1)

        bar = QFrame()
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(20, 8, 20, 8)
        self.status_label = QLabel("")
        self.status_label.setObjectName("Muted")
        bar_layout.addWidget(self.status_label, 1)
        self.preview_button = QPushButton("Preview")
        self.preview_button.clicked.connect(self.preview)
        self.start_button = QPushButton("Start")
        self.start_button.setProperty("primary", True)
        self.start_button.clicked.connect(self.start)
        bar_layout.addWidget(self.preview_button)
        bar_layout.addWidget(self.start_button)
        outer.addWidget(bar)

    def _build_targets(self) -> QFrame:
        frame, layout = _card("Targets")
        self.target_list = QListWidget()
        self.target_list.setObjectName("Targets")
        if self.initial:
            label = QLabel("New device (day-0)")
            layout.addWidget(label)
            self.target_list.setVisible(False)
        else:
            result = resolve_targets(
                self.play.targets,
                self.cust.devices,
                sites=self.cust.sites.keys(),
                roles=self.ctx.workspace.roles.keys(),  # type: ignore[union-attr]
            )
            self.devices = result.devices
            if not result.ok:
                self.target_error = "; ".join(f.message for f in result.findings)
                err = QLabel(self.target_error)
                err.setStyleSheet(f"color: {theme.current().error};")
                layout.addWidget(err)
            for device in self.devices:
                item = QListWidgetItem(
                    f"{device.name}  ({device.site}, {device.role}, {device.platform})"
                )
                item.setData(Qt.ItemDataRole.UserRole, device.name)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked)
                self.target_list.addItem(item)
            rows = max(1, min(len(self.devices), 8))
            self.target_list.setFixedHeight(rows * 26 + 8)
            self.target_list.itemChanged.connect(self._targets_changed)
            layout.addWidget(self.target_list)
        return frame

    def _build_prompts(self) -> QFrame:
        frame, layout = _card("Prompts")
        base = PromptContext(
            frozenset(self.cust.sites),
            frozenset(self.ctx.workspace.roles),  # type: ignore[union-attr]
            self.catalog.ids(),
            None,
            self._pushable_logins(),
        )
        self._stage1_names = [
            n for n, p in self.play.prompts.items() if self.initial and p.type in FIRST_STAGE
        ]
        self._stage1_box = QWidget()
        self._stage2_box = QWidget()
        form1 = QFormLayout(self._stage1_box)
        form2 = QFormLayout(self._stage2_box)
        for form in (form1, form2):
            form.setContentsMargins(0, 0, 0, 0)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        per_device: list[tuple[str, Any]] = []
        for name, prompt in self.play.prompts.items():
            if prompt.per_device and not self.initial:
                per_device.append((name, prompt))
                continue
            field = make_prompt_widget(name, prompt, base)
            self.fields[name] = field
            label = QLabel(field.label_text)
            label.setToolTip(prompt.help or "")
            (form1 if name in self._stage1_names else form2).addRow(label, field)
            field.changed.connect(lambda n=name: self._field_changed(n))
        if not self.fields and not per_device:
            layout.addWidget(QLabel("This play asks no questions."))
        self._stage1_box.setVisible(bool(self._stage1_names))
        layout.addWidget(self._stage1_box)
        layout.addWidget(self._stage2_box)

        self._grid_box = QWidget()
        grid = QGridLayout(self._grid_box)
        grid.setContentsMargins(0, 0, 0, 0)
        if per_device:
            caption = QLabel("Per device")
            caption.setObjectName("Muted")
            layout.addWidget(caption)
            grid.addWidget(QLabel("Device"), 0, 0)
            for col, (name, prompt) in enumerate(per_device, start=1):
                head = QLabel(prompt.label or name)
                head.setToolTip(prompt.help or "")
                grid.addWidget(head, 0, col)
            for row, device in enumerate(self.devices, start=1):
                widgets: list[QWidget] = []
                label = QLabel(device.name)
                grid.addWidget(label, row, 0, Qt.AlignmentFlag.AlignTop)
                widgets.append(label)
                for col, (name, prompt) in enumerate(per_device, start=1):
                    field = make_prompt_widget(name, prompt, base)
                    self.device_fields[(device.name, name)] = field
                    grid.addWidget(field, row, col)
                    widgets.append(field)
                    field.changed.connect(lambda n=name, d=device.name: self._field_changed(n, d))
                self._device_rows[device.name] = widgets
        layout.addWidget(self._grid_box)
        self._grid_box.setVisible(bool(per_device))
        return frame

    def _build_options(self) -> QFrame:
        frame, layout = _card("Options")
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        self.mode_combo = wheel.ComboBox()
        modes = [RunMode.CHECK, RunMode.APPLY, RunMode.RENDER]
        if self.play.type == PlayType.BASELINE:
            modes.append(RunMode.COMPLIANCE)
        for mode in modes:
            self.mode_combo.addItem(MODE_LABELS[mode], mode)
        self.mode_combo.currentIndexChanged.connect(self._invalidate)
        form.addRow("Mode", self.mode_combo)

        tags = sorted({t for step in self.play.steps for t in step.tags})
        self.tag_boxes: dict[str, QCheckBox] = {}
        if tags:
            tag_row = QHBoxLayout()
            for tag in tags:
                box = QCheckBox(tag)
                box.toggled.connect(self._invalidate)
                self.tag_boxes[tag] = box
                tag_row.addWidget(box)
            tag_row.addStretch(1)
            holder = QWidget()
            holder.setLayout(tag_row)
            tag_row.setContentsMargins(0, 0, 0, 0)
            holder.setToolTip(
                "Only steps with a checked tag run, plus steps tagged always. None checked: all."
            )
            form.addRow("Tags", holder)

        settings = self.play.settings()
        if (
            self.initial
            and settings.method == ConnectionMethod.SERIAL
            and self.play.connection.port in (None, "ask")
        ):
            self.port_picker = PortPicker()
            self.port_picker.currentIndexChanged.connect(self._invalidate)
            form.addRow("Serial port", self.port_picker)
        layout.addLayout(form)
        return frame

    def _build_preview(self) -> QFrame:
        frame, layout = _card("Preview")
        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {theme.current().error};")
        self.error_label.setVisible(False)
        self.error_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.error_label)
        self.preview_tabs = QTabWidget()
        self.preview_tabs.setMinimumHeight(260)
        self.preview_tabs.setVisible(False)
        layout.addWidget(self.preview_tabs)
        self.preview_hint = QLabel(
            "Preview shows the masked config for every selected device. Secrets appear as ********."
        )
        self.preview_hint.setObjectName("Muted")
        self.preview_hint.setWordWrap(True)
        layout.addWidget(self.preview_hint)
        return frame

    # -- state helpers ---------------------------------------------------------------

    def selected_devices(self) -> list[Device]:
        if self.initial:
            return []
        checked = {
            self.target_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.target_list.count())
            if self.target_list.item(i).checkState() == Qt.CheckState.Checked
        }
        return [d for d in self.devices if d.name in checked]

    def preselect_devices(self, names: list[str]) -> None:
        """Tick exactly these devices and untick the rest."""
        wanted = set(names)
        for i in range(self.target_list.count()):
            item = self.target_list.item(i)
            on = item.data(Qt.ItemDataRole.UserRole) in wanted
            item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def fill_prompts(self, values: dict[str, Any]) -> None:
        """Fill shared prompts from an earlier job; a value that no longer fits is skipped."""
        for name, value in values.items():
            field = self.fields.get(name)
            if field is None:
                continue
            try:
                field.set_value(value)
            except (TypeError, ValueError):
                try:
                    field.set_value(int(value))
                except (TypeError, ValueError):
                    continue

    def set_device_checked(self, name: str, checked: bool) -> None:
        for i in range(self.target_list.count()):
            item = self.target_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == name:
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def mode(self) -> RunMode:
        return self.mode_combo.currentData()

    def set_mode(self, mode: RunMode) -> None:
        index = self.mode_combo.findData(mode)
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)

    def selected_tags(self) -> list[str] | None:
        tags = [t for t, box in self.tag_boxes.items() if box.isChecked()]
        return tags or None

    def visible_prompt_names(self) -> list[str]:
        return [n for n, f in self.fields.items() if f.isVisibleTo(self)]

    def preview_device_names(self) -> list[str]:
        return [self.preview_tabs.tabText(i) for i in range(self.preview_tabs.count())]

    def _limit(self) -> list[str] | None:
        chosen = [d.name for d in self.selected_devices()]
        return None if len(chosen) == len(self.devices) else chosen

    def _context(self, family: PlatformFamily | None) -> PromptContext:
        return PromptContext(
            frozenset(self.cust.sites),
            frozenset(self.ctx.workspace.roles),  # type: ignore[union-attr]
            self.catalog.ids(),
            family,
            self._pushable_logins(),
        )

    def _pushable_logins(self) -> frozenset[str]:
        """The logins a login prompt offers: those the customer allows that are saved."""
        allowed = allowed_logins(self.cust)
        try:
            saved = set(self.ctx.secrets.list_logins())
        except TsunamiError:
            return allowed  # locked: the names are checked again when the play renders
        return frozenset(allowed & saved)

    def _shared_family(self, stage1: dict[str, Any] | None = None) -> PlatformFamily | None:
        if self.initial:
            platform = self.play.vars.get("platform")
            if platform is None:
                for name, prompt in self.play.prompts.items():
                    if prompt.type.value != "model":
                        continue
                    raw = (stage1 or {}).get(name) or self._raw(self.fields.get(name))
                    if raw and raw in self.catalog.ids():
                        platform = self.catalog.get(raw).platform
            return PLATFORM_FAMILY[Platform(platform)] if platform else None
        families = {PLATFORM_FAMILY[d.platform] for d in self.selected_devices()}
        return next(iter(families)) if len(families) == 1 else None

    @staticmethod
    def _raw(field: PromptField | None) -> Any:
        return field.raw_value() if field is not None else None

    def _check_field(
        self, field: PromptField, family: PlatformFamily | None
    ) -> tuple[Any, str | None]:
        """(coerced value, error). An empty field is fine when a default will fill it."""
        prompt = field.prompt
        raw = field.raw_value()
        if raw is None or raw == "":
            if prompt.default is not None:
                return None, None
            return None, "required" if prompt.required else None
        try:
            value = coerce(
                prompt.type,
                raw,
                context=self._context(family),
                choices=prompt.choices,
                pattern=prompt.pattern,
                minimum=prompt.min,
                maximum=prompt.max,
            )
        except PromptValueError as exc:
            return None, str(exc)
        return value, None

    def _stage1_values(self) -> dict[str, Any] | None:
        """Coerced first-stage answers, or None while any is missing or invalid."""
        values: dict[str, Any] = {}
        for name in self._stage1_names:
            field = self.fields[name]
            value, error = self._check_field(field, None)
            if error or value is None:
                if field.prompt.required or error:
                    return None
                continue
            values[name] = value
        return values

    def _validate_all(self, show_errors: bool) -> bool:
        ok = True
        stage1 = self._stage1_values() if self.initial else None
        if self.initial and stage1 is None:
            ok = False
        family = self._shared_family(stage1)
        for name, field in self.fields.items():
            in_stage1 = name in self._stage1_names
            if not in_stage1 and self._stage2_box.isHidden():
                field.set_error(None)
                continue
            _value, error = self._check_field(field, family)
            ok = ok and error is None
            field.set_error(error if (show_errors or field.touched) else None)
        selected = {d.name for d in self.selected_devices()}
        for (device_name, _name), field in self.device_fields.items():
            if device_name not in selected:
                field.set_error(None)
                continue
            device = next(d for d in self.devices if d.name == device_name)
            _value, error = self._check_field(field, PLATFORM_FAMILY[device.platform])
            ok = ok and error is None
            field.set_error(error if (show_errors or field.touched) else None)
        if not self.initial and not selected:
            ok = False
        return ok

    # -- reactions -------------------------------------------------------------------

    def _field_changed(self, name: str, device: str | None = None) -> None:
        if not self._prefilling and device is None:
            self._user_edited.add(name)
        self._invalidate()
        if self.initial:
            self._update_stages()
        self._validate_all(False)

    def _targets_changed(self, *_args: object) -> None:
        selected = {d.name for d in self.selected_devices()}
        for name, widgets in self._device_rows.items():
            for widget in widgets:
                widget.setVisible(name in selected)
        self._invalidate()
        self._validate_all(False)

    def _update_stages(self) -> None:
        """Show the second-stage prompts once the first stage is valid, and pre-fill them."""
        if not self.initial or not self._stage1_names:
            return
        values = self._stage1_values()
        self._stage2_box.setVisible(values is not None)
        if values is None:
            self._last_stage1 = None
            return
        if values == self._last_stage1:
            return
        self._last_stage1 = values
        assert self.ctx.workspace is not None
        try:
            resolved = self.ctx.workspace.resolve_vars(
                self.play.customer, play=self.entry, prompt_values=values
            ).values
        except TsunamiError:
            return
        self._prefilling = True
        try:
            for name, field in self.fields.items():
                if name in self._stage1_names or name in self._user_edited:
                    continue
                if field.prompt.default is not None or name not in resolved:
                    continue
                value = resolved[name]
                if isinstance(value, (str, int)) and not isinstance(value, bool):
                    field.set_value(value)
                    field.touched = False
        finally:
            self._prefilling = False

    def _invalidate(self, *_args: object) -> None:
        self._previewed = False
        self._clear_preview()
        self._update_buttons()

    def _clear_preview(self) -> None:
        self.preview_tabs.clear()
        self.preview_tabs.setVisible(False)
        self.error_label.setVisible(False)
        self.error_label.setText("")

    def _update_buttons(self) -> None:
        self.start_button.setEnabled(self._previewed)
        self.preview_button.setEnabled(
            self.cust is not None and (self.initial or bool(self.devices))
        )
        if self._previewed:
            self.status_label.setText("Preview is up to date. Ready to start.")
        else:
            self.status_label.setText("Preview before starting.")

    def _show_error(self, message: str) -> None:
        self.preview_tabs.clear()
        self.preview_tabs.setVisible(False)
        self.error_label.setText(message)
        self.error_label.setVisible(True)

    # -- preview ---------------------------------------------------------------------

    def _shared_raw(self) -> dict[str, Any]:
        raw: dict[str, Any] = {}
        for name, field in self.fields.items():
            if name in self._stage1_names or not self._stage2_box.isHidden() or not self.initial:
                value = field.raw_value()
                if value is not None and value != "":
                    raw[name] = value
        return raw

    def _device_raw(self, device: str) -> dict[str, Any]:
        raw: dict[str, Any] = {}
        for (dev, name), field in self.device_fields.items():
            value = field.raw_value()
            if dev == device and value is not None and value != "":
                raw[name] = value
        return raw

    def preview(self) -> bool:
        """Validate, render every selected device and show the masked result."""
        self._previewed = False
        if self.cust is None:
            self._update_buttons()
            return False
        self._clear_preview()
        if self.target_error:
            self._show_error(self.target_error)
            self._update_buttons()
            return False
        if not self.initial and not self.selected_devices():
            self._show_error("Select at least one device.")
            self._update_buttons()
            return False
        if not self._validate_all(True):
            self._show_error("Fix the highlighted fields, then preview again.")
            self._update_buttons()
            return False
        assert self.ctx.workspace is not None
        workspace = self.ctx.workspace
        renderer = Renderer(workspace, self.ctx.settings, self.ctx.secrets)
        tags = self.selected_tags()
        shared = self._shared_raw()
        rendered: list[tuple[str, str]] = []
        try:
            if self.initial:
                values = collect_prompts(workspace, self.entry, shared)
                label = str(values.get("hostname") or "new-device").lower()
                rendering = renderer.render(
                    self.entry, device=None, prompt_values=values, tags=tags
                )
                rendered.append((label, rendering.masked_text()))
            else:
                for device in self.selected_devices():
                    raw = {**shared, **self._device_raw(device.name)}
                    values = collect_prompts(workspace, self.entry, raw, device=device.name)
                    rendering = renderer.render(
                        self.entry, device=device.name, prompt_values=values, tags=tags
                    )
                    rendered.append((device.name, rendering.masked_text()))
        except TsunamiError as exc:
            self._show_error(str(exc))
            self._update_buttons()
            return False
        for label, text in rendered:
            self.preview_tabs.addTab(MaskedTextView(text), label)
        self.preview_tabs.setVisible(True)
        self._previewed = True
        self._update_buttons()
        return True

    # -- start -----------------------------------------------------------------------

    def build_request(self) -> RunRequest:
        device_prompts = {
            d.name: raw for d in self.selected_devices() if (raw := self._device_raw(d.name))
        }
        return RunRequest(
            self.entry.path,
            self.mode(),
            prompts=self._shared_raw(),
            device_prompts=device_prompts,
            tags=self.selected_tags(),
            limit=self._limit(),
            serial_port=self.port_picker.port() if self.port_picker else None,
            customer=self.customer_name() if self.is_global else None,
        )

    def start(self) -> None:
        if not self._previewed or self.cust is None:
            return
        if self.port_picker is not None and not self.port_picker.port():
            self.shell.notify("Choose a serial port first.", "warning")
            return
        request = self.build_request()
        if request.mode == RunMode.APPLY:
            count = 1 if self.initial else len(self.selected_devices())
            answer = QMessageBox.question(
                self,
                "Apply changes",
                f"Apply '{self.play.name}' to {count} device{'s' if count != 1 else ''}? "
                "This changes the device configuration.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.shell.start_run(request, f"{self.play.name} ({request.mode})")
        self._invalidate()
