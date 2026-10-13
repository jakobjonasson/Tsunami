"""Prompt widgets matched to prompt types, with inline validation.

make_prompt_widget() returns a PromptField: the editor for one prompt, a small error
message under it, and a uniform raw_value / set_value / set_error / changed interface.
Validation itself is core's prompts.coerce; the field only shows the result.

See design.md, GUI: Behavior rules; Play schema: prompts.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from tsunami.core.catalog import load_catalog
from tsunami.core.models.common import Platform
from tsunami.core.models.play import Prompt, PromptType
from tsunami.core.prompts import PromptContext
from tsunami.gui import theme, wheel

VENDOR_TITLES = {"cisco": "Cisco", "aruba": "Aruba"}
_COMBO_TYPES = {PromptType.CHOICE, PromptType.SITE, PromptType.ROLE, PromptType.MODEL}


class PromptField(QWidget):
    """One prompt's editor plus its error line."""

    changed = Signal()

    def __init__(
        self,
        name: str,
        prompt: Prompt,
        editor: QWidget,
        getter: Callable[[], Any],
        setter: Callable[[Any], None],
        notify: Signal,
    ) -> None:
        super().__init__()
        self.name = name
        self.prompt = prompt
        self.label_text = prompt.label or name
        self.editor = editor
        self.touched = False  # the person (or set_value) has entered something
        self._get = getter
        self._set = setter
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(editor)
        self._error = QLabel()
        self._error.setWordWrap(True)
        self._error.setVisible(False)
        layout.addWidget(self._error)
        if prompt.help:
            self.setToolTip(prompt.help)
            editor.setToolTip(prompt.help)
        notify.connect(self._edited)

    def _edited(self, *_args: object) -> None:
        self.touched = True
        self.changed.emit()

    def raw_value(self) -> Any:
        """What the person entered: int, bool or str. None or "" means nothing entered."""
        return self._get()

    def set_value(self, value: Any) -> None:
        self._set(value)

    def set_error(self, message: str | None) -> None:
        editor = self.editor
        if message:
            self._error.setText(message)
            self._error.setStyleSheet(f"color: {theme.current().error}; font-size: 8.5pt;")
            self._error.setVisible(True)
            self._error.setToolTip(message)
        else:
            self._error.clear()
            self._error.setVisible(False)
        if isinstance(editor, QLineEdit):
            editor.setProperty("invalid", bool(message))
            editor.style().unpolish(editor)
            editor.style().polish(editor)
        else:
            colour = theme.current().error
            editor.setStyleSheet(f"border: 1px solid {colour};" if message else "")

    @property
    def error_text(self) -> str:
        return self._error.text()

    def has_error(self) -> bool:
        return not self._error.isHidden()


def _default(prompt: Prompt) -> Any:
    """A literal default; Jinja-expression defaults are resolved by core at run time."""
    value = prompt.default
    if isinstance(value, str) and ("{{" in value or "{%" in value):
        return None
    return value


def model_combo(platforms: Collection[Platform] | None = None) -> QComboBox:
    """Catalog models grouped by vendor. Unsupported platforms are listed but disabled."""
    combo = wheel.ComboBox()
    combo.addItem("", "")
    for vendor, entries in load_catalog().by_vendor().items():
        shown = [e for e in entries if platforms is None or e.platform in platforms]
        if not shown:
            continue
        combo.addItem(VENDOR_TITLES.get(vendor, vendor.title()), None)
        model = combo.model()
        assert isinstance(model, QStandardItemModel)
        header = model.item(combo.count() - 1)
        header.setEnabled(False)
        header.setSelectable(False)
        for entry in shown:
            label = f"    {entry.label}"
            if not entry.selectable:
                label += " (not supported yet)"
            combo.addItem(label, entry.id)
            item = model.item(combo.count() - 1)
            item.setEnabled(entry.selectable)
            if entry.lifecycle == "end-of-sale":
                item.setToolTip("End of sale")
    return combo


def _combo_field(
    name: str,
    prompt: Prompt,
    items: list[tuple[str, Any]],
    combo: QComboBox | None = None,
) -> tuple[QComboBox, Callable[[], Any], Callable[[Any], None]]:
    if combo is None:
        combo = wheel.ComboBox()
        combo.addItem("", "")
        for text, data in items:
            combo.addItem(text, data)

    def get() -> str:
        data = combo.currentData()
        return data if isinstance(data, str) else ""

    def set_(value: Any) -> None:
        index = combo.findData(str(value))
        combo.setCurrentIndex(max(index, 0))

    return combo, get, set_


def make_prompt_widget(
    name: str,
    prompt: Prompt,
    context: PromptContext,
    *,
    platforms: Collection[Platform] | None = None,
) -> PromptField:
    """The editor for one prompt. `platforms` limits the model dropdown."""
    ptype = prompt.type
    if ptype in (PromptType.VLAN, PromptType.INT):
        spin = wheel.SpinBox()
        if ptype == PromptType.VLAN:
            spin.setRange(1, 4094)
        else:
            spin.setRange(
                prompt.min if prompt.min is not None else -(2**31),
                prompt.max if prompt.max is not None else 2**31 - 1,
            )
        spin.setAlignment(Qt.AlignmentFlag.AlignLeft)
        state = {"set": False}  # an untouched spin box must not silently submit its minimum

        def get_spin() -> int | None:
            return spin.value() if state["set"] else None

        def set_spin(value: Any) -> None:
            try:
                number = int(value)
            except (TypeError, ValueError):
                return
            state["set"] = True
            spin.setValue(number)

        # Connected before the field's own handler, so raw_value() is ready when it fires.
        spin.valueChanged.connect(lambda _v: state.__setitem__("set", True))
        field = PromptField(name, prompt, spin, get_spin, set_spin, spin.valueChanged)
        spin.lineEdit().textEdited.connect(lambda _t: _mark(field, state))
    elif ptype == PromptType.BOOL:
        box = QCheckBox()
        field = PromptField(
            name,
            prompt,
            box,
            box.isChecked,
            lambda v: box.setChecked(v if isinstance(v, bool) else str(v).lower() == "true"),
            box.toggled,
        )
    elif ptype in _COMBO_TYPES:
        if ptype == PromptType.MODEL:
            combo, getter, setter = _combo_field(name, prompt, [], model_combo(platforms))
        elif ptype == PromptType.CHOICE:
            items = [(c, c) for c in prompt.choices or []]
            combo, getter, setter = _combo_field(name, prompt, items)
        elif ptype == PromptType.SITE:
            combo, getter, setter = _combo_field(
                name, prompt, [(s, s) for s in sorted(context.sites)]
            )
        else:
            combo, getter, setter = _combo_field(
                name, prompt, [(r, r) for r in sorted(context.roles)]
            )
        field = PromptField(name, prompt, combo, getter, setter, combo.currentIndexChanged)
    else:
        line = QLineEdit()
        if ptype == PromptType.VLAN_LIST:
            line.setPlaceholderText("10,20,30-35")
        elif ptype == PromptType.IPV4_INTERFACE:
            line.setPlaceholderText("10.1.1.5/24")
        elif ptype == PromptType.IPV4_NETWORK:
            line.setPlaceholderText("10.1.1.0/24")
        elif ptype == PromptType.IPV4:
            line.setPlaceholderText("10.1.1.1")
        field = PromptField(
            name,
            prompt,
            line,
            lambda: line.text(),
            lambda v: line.setText(", ".join(map(str, v)) if isinstance(v, list) else str(v)),
            line.textChanged,
        )

    default = _default(prompt)
    if default is not None:
        field.set_value(default)
        field.touched = False
    return field


def _mark(field: PromptField, state: dict[str, bool]) -> None:
    if not field.touched:
        state["set"] = True
        field._edited()
