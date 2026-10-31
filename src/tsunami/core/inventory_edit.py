"""Edit inventory files from the GUI: customers, sites and devices.

Each change loads the file for round-trip editing (comments and layout are kept),
applies the change, validates the result with the models the loader uses, and writes it
atomically. Rules that span files (a device's site exists, its type is known, device
names are unique) are checked before anything is written. Deleting a customer, or a
site's file, sends it to the Windows Recycle Bin so it can be restored.

See design.md, Inventory and variables; GUI: Inventory.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from tsunami.core.catalog import load_catalog
from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import require_slug
from tsunami.core.models.inventory import DEVICE_TYPES, CustomerFile, Device, DevicesFile, VarsFile
from tsunami.core.yamlio import (
    RoundTripDoc,
    YamlLoadError,
    dump_roundtrip,
    load_roundtrip,
    load_yaml_text,
)

CUSTOMERS = Path("inventory", "customers")
ROLES = Path("inventory", "roles")
DEVICE_KEYS = ("name", "host", "port", "site", "role", "platform", "model", "credential", "vars")
CUSTOMER_KEYS = (
    "name",
    "credential",
    "jump_host",
    "lab",
    "vars",
    "role_vars",
    "site_credentials",
    "push_logins",
)
OPTIONAL_DEVICE_KEYS = frozenset({"port", "model", "credential", "vars"})


class InventoryEditError(TsunamiError):
    """An inventory change was refused. Nothing was written."""


def send_to_recycle_bin(path: Path) -> None:
    """Move a file or folder to the Recycle Bin, so the deletion can be undone."""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    fo_delete, allow_undo, no_confirm, silent, no_error_ui = 3, 0x40, 0x10, 0x4, 0x400
    op = SHFILEOPSTRUCTW(
        None,
        fo_delete,
        str(Path(path).resolve()) + "\0",  # the list is double-NUL terminated
        None,
        allow_undo | no_confirm | silent | no_error_ui,
        False,
        None,
        None,
    )
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code != 0 or op.fAnyOperationsAborted:
        raise InventoryEditError(f"could not move {Path(path).name} to the Recycle Bin ({code})")


class InventoryEditor:
    def __init__(self, root: Path, *, recycle: Callable[[Path], None] = send_to_recycle_bin):
        self.root = Path(root)
        self.recycle = recycle

    # -- customers --------------------------------------------------------------------

    def create_customer(self, slug: str, *, name: str, credential: str, lab: bool = False):
        folder = self._customer_dir(slug, must_exist=False)
        if folder.exists():
            raise InventoryEditError(f"customer {slug!r} already exists")
        data = CommentedMap(name=name, credential=credential, lab=lab)
        self._validate(CustomerFile, data, "customer.yml")
        (folder / "sites").mkdir(parents=True)
        dump_roundtrip(_new_doc(folder / "customer.yml", data))

    def update_customer(self, slug: str, **fields: Any) -> None:
        """Set customer.yml fields; a None value removes an optional one (jump_host)."""
        unknown = set(fields) - {"name", "credential", "lab", "jump_host", "push_logins"}
        if unknown:
            raise InventoryEditError(f"cannot edit customer field(s): {', '.join(sorted(unknown))}")
        doc = self._customer_doc(slug)
        for key, value in fields.items():
            _set_key(doc.data, key, _plain(value), CUSTOMER_KEYS)
        self._validate(CustomerFile, doc.data, "customer.yml")
        dump_roundtrip(doc)

    def delete_customer(self, slug: str) -> None:
        self.recycle(self._customer_dir(slug))

    # -- sites --------------------------------------------------------------------------

    def create_site(
        self, customer: str, site: str, *, credential: str | None = None, vars_text: str = ""
    ) -> None:
        path = self._site_path(customer, site, must_exist=False)
        if path.exists():
            raise InventoryEditError(f"site {site!r} already exists for {customer!r}")
        text = self._checked_vars_text(vars_text, path)
        if credential:
            self._set_site_credential(customer, site, credential)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def update_site(
        self,
        customer: str,
        site: str,
        *,
        new_name: str | None = None,
        credential: str | None = None,
        vars_text: str | None = None,
        set_credential: bool = False,
    ) -> None:
        """Rename a site, set or clear its default login (set_credential), or replace its
        variables (vars_text, YAML). Renaming moves every device at the site with it."""
        path = self._site_path(customer, site)
        if vars_text is not None:
            path.write_text(
                self._checked_vars_text(vars_text, path), encoding="utf-8", newline="\n"
            )
        if set_credential:
            self._set_site_credential(customer, site, credential or None)
        if new_name and new_name != site:
            target = self._site_path(customer, new_name, must_exist=False)
            if target.exists():
                raise InventoryEditError(f"site {new_name!r} already exists for {customer!r}")
            doc = self._customer_doc(customer)
            defaults = doc.data.get("site_credentials")
            if isinstance(defaults, Mapping) and site in defaults:
                defaults[new_name] = defaults.pop(site)
                self._validate(CustomerFile, doc.data, "customer.yml")
            devices = self._devices_doc(customer)
            for item in devices.data:
                if item.get("site") == site:
                    item["site"] = new_name
            path.rename(target)
            dump_roundtrip(doc)
            if devices.data:
                dump_roundtrip(devices)

    def delete_site(self, customer: str, site: str, *, delete_devices: bool = False) -> None:
        path = self._site_path(customer, site)
        devices = self._devices_doc(customer)
        at_site = [item for item in devices.data if item.get("site") == site]
        if at_site and not delete_devices:
            names = ", ".join(str(item.get("name")) for item in at_site)
            raise InventoryEditError(f"site {site!r} still has devices: {names}")
        if at_site:
            devices.data[:] = [item for item in devices.data if item.get("site") != site]
            dump_roundtrip(devices)
        self._set_site_credential(customer, site, None)
        self.recycle(path)

    # -- roles --------------------------------------------------------------------------

    def create_role(self, role: str, *, vars_text: str = "") -> None:
        """Add inventory/roles/<role>.yml. Built-in device types cannot be redefined."""
        if role in DEVICE_TYPES:
            raise InventoryEditError(f"role {role!r} is a built-in device type")
        path = self.root / ROLES / f"{_slug(role, 'role')}.yml"
        if path.exists():
            raise InventoryEditError(f"role {role!r} already exists")
        text = self._checked_vars_text(vars_text, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    # -- devices ------------------------------------------------------------------------

    def add_device(self, customer: str, fields: Mapping[str, Any]) -> None:
        devices = self._devices_doc(customer)
        item = CommentedMap()
        for key in DEVICE_KEYS:
            value = fields.get(key)
            if value is not None and not (key == "port" and value == 22):
                item[key] = _plain(value)
        devices.data.append(item)
        self._check_devices(customer, devices.data)
        dump_roundtrip(devices)

    def add_devices(
        self,
        customer: str,
        batch: Sequence[Mapping[str, Any]],
        *,
        create_sites: Sequence[str] = (),
    ) -> None:
        """Add many devices, and the sites they need, with one validation and one write.

        All or nothing: if the result is invalid nothing is added and the sites created
        for this call are removed again. Calling add_device per row would revalidate the
        whole file each time.
        """
        if not batch:
            return
        devices = self._devices_doc(customer)
        for fields in batch:
            item = CommentedMap()
            for key in DEVICE_KEYS:
                value = fields.get(key)
                if value is not None and not (key == "port" and value == 22):
                    item[key] = _plain(value)
            devices.data.append(item)
        created: list[Path] = []
        try:
            for site in create_sites:
                path = self._site_path(customer, site, must_exist=False)
                if path.exists():
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8", newline="\n")
                created.append(path)
            self._check_devices(customer, devices.data)
            dump_roundtrip(devices)
        except BaseException:
            for path in created:
                path.unlink(missing_ok=True)
            raise

    def update_device(self, customer: str, name: str, fields: Mapping[str, Any]) -> None:
        """Set device fields; a None value removes an optional one (model, credential...)."""
        unknown = set(fields) - set(DEVICE_KEYS)
        if unknown:
            raise InventoryEditError(f"unknown device field(s): {', '.join(sorted(unknown))}")
        devices = self._devices_doc(customer)
        item = self._find_device(devices.data, name)
        for key, value in fields.items():
            if value is None and key not in OPTIONAL_DEVICE_KEYS:
                raise InventoryEditError(f"device field {key!r} is required")
            if key == "port" and value == 22:
                value = None  # the default stays implicit, as in hand-written files
            _set_key(item, key, _plain(value), DEVICE_KEYS)
        self._check_devices(customer, devices.data)
        dump_roundtrip(devices)

    def delete_device(self, customer: str, name: str) -> None:
        devices = self._devices_doc(customer)
        devices.data.remove(self._find_device(devices.data, name))
        dump_roundtrip(devices)

    # -- helpers ------------------------------------------------------------------------

    def _customer_dir(self, slug: str, *, must_exist: bool = True) -> Path:
        folder = self.root / CUSTOMERS / _slug(slug, "customer")
        if must_exist and not (folder / "customer.yml").is_file():
            raise InventoryEditError(f"unknown customer {slug!r}")
        return folder

    def _site_path(self, customer: str, site: str, *, must_exist: bool = True) -> Path:
        path = self._customer_dir(customer) / "sites" / f"{_slug(site, 'site')}.yml"
        if must_exist and not path.is_file():
            raise InventoryEditError(f"unknown site {site!r} for {customer!r}")
        return path

    def _customer_doc(self, slug: str) -> RoundTripDoc:
        return _load(self._customer_dir(slug) / "customer.yml", CommentedMap)

    def _devices_doc(self, customer: str) -> RoundTripDoc:
        path = self._customer_dir(customer) / "devices.yml"
        if not path.is_file():
            return _new_doc(path, _block_seq())
        doc = _load(path, CommentedSeq)
        if not doc.data:  # "[]" loads as a flow list; new items read better in block style
            doc.data = _block_seq()
        return doc

    def _set_site_credential(self, customer: str, site: str, credential: str | None) -> None:
        doc = self._customer_doc(customer)
        defaults = doc.data.get("site_credentials")
        if credential is None:
            if isinstance(defaults, Mapping) and site in defaults:
                del defaults[site]
                if not defaults:
                    del doc.data["site_credentials"]
                dump_roundtrip(doc)
            return
        if not isinstance(defaults, Mapping):
            defaults = CommentedMap()
            _set_key(doc.data, "site_credentials", defaults, CUSTOMER_KEYS)
        defaults[site] = credential
        self._validate(CustomerFile, doc.data, "customer.yml")
        dump_roundtrip(doc)

    def _checked_vars_text(self, text: str, path: Path) -> str:
        text = text if text.strip() else "{}\n"
        try:
            data = load_yaml_text(text, path).data
        except YamlLoadError as exc:
            raise InventoryEditError(str(exc)) from None
        self._validate(VarsFile, {} if data is None else data, path.name)
        return text if text.endswith("\n") else text + "\n"

    def _check_devices(self, customer: str, items: list) -> None:
        self._validate(DevicesFile, items, "devices.yml")
        folder = self._customer_dir(customer)
        catalog = load_catalog()
        seen: set[str] = set()
        for item in items:
            device = Device.model_validate(_to_plain(item))
            if device.name in seen:
                raise InventoryEditError(f"device name {device.name!r} is already used")
            seen.add(device.name)
            if not (folder / "sites" / f"{device.site}.yml").is_file():
                raise InventoryEditError(f"site {device.site!r} does not exist")
            if (
                device.role not in DEVICE_TYPES
                and not (self.root / ROLES / f"{device.role}.yml").is_file()
            ):
                raise InventoryEditError(f"type {device.role!r} is not a device type or role")
            if device.model is not None:
                if device.model not in catalog.ids():
                    raise InventoryEditError(f"model {device.model!r} is not in the catalog")
                if catalog.get(device.model).platform != device.platform:
                    raise InventoryEditError(
                        f"model {device.model!r} runs {catalog.get(device.model).platform}, "
                        f"not {device.platform}"
                    )

    @staticmethod
    def _find_device(items: list, name: str) -> CommentedMap:
        for item in items:
            if isinstance(item, Mapping) and item.get("name") == name:
                return item
        raise InventoryEditError(f"unknown device {name!r}")

    @staticmethod
    def _validate(model: Any, data: Any, label: str) -> None:
        try:
            model.model_validate(_to_plain(data))
        except ValidationError as exc:
            problems = []
            for err in exc.errors(include_url=False, include_context=False, include_input=False):
                where = ".".join(str(p) for p in err["loc"])
                problems.append(f"{where}: {err['msg'].removeprefix('Value error, ')}")
            raise InventoryEditError(f"{label}: " + "; ".join(problems)) from None


def _slug(value: str, what: str) -> str:
    try:
        return require_slug(value)
    except TsunamiError:
        raise InventoryEditError(
            f"{what} name {value!r} must be lowercase letters, digits and dashes"
        ) from None


def _load(path: Path, kind: type) -> RoundTripDoc:
    try:
        doc = load_roundtrip(path)
    except YamlLoadError as exc:
        raise InventoryEditError(str(exc)) from None
    if doc.data is None:
        doc.data = kind() if kind is CommentedMap else _block_seq()
    if not isinstance(doc.data, kind):
        raise InventoryEditError(f"{path.name} has an unexpected layout; edit it by hand")
    return doc


def _new_doc(path: Path, data: Any) -> RoundTripDoc:
    return RoundTripDoc(
        path=path, data=data, mapping_indent=2, sequence_indent=2, sequence_offset=0
    )


def _block_seq() -> CommentedSeq:
    seq = CommentedSeq()
    seq.fa.set_block_style()
    return seq


def _set_key(mapping: CommentedMap, key: str, value: Any, order: tuple[str, ...]) -> None:
    """Set or remove (None) key, inserting a new key at its usual place in the file."""
    if value is None:
        mapping.pop(key, None)
        return
    if key in mapping:
        mapping[key] = value
        return
    rank = order.index(key) if key in order else len(order)
    position = len(mapping)
    for index, existing in enumerate(mapping):
        if existing in order and order.index(existing) > rank:
            position = index
            break
    mapping.insert(position, key, value)


def _plain(value: Any) -> Any:
    """GUI values to YAML values: enums to strings; nested dicts become block mappings."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        out = CommentedMap()
        for key, item in value.items():
            out[key] = _plain(item)
        return out
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


def _to_plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_plain(v) for v in value]
    return value


__all__ = ["InventoryEditError", "InventoryEditor", "send_to_recycle_bin"]
