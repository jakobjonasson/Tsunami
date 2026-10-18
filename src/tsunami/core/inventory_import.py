"""Import devices into a customer from a CSV file.

Three steps, so a GUI can show the middle one: read_csv() parses the file and refuses one
that carries secrets, preview() maps the columns and checks every row against the
inventory without writing anything, and apply_import() writes the importable rows in one
batch through InventoryEditor.add_devices. Nothing is written if no row is importable.

A CSV never carries secrets. A device names its login (a Credential Manager entry); the
login itself is added in Credentials. A file with a password-like column is refused
before any row is read.

See design.md, Inventory and variables; GUI: Inventory.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tsunami.core.catalog import load_catalog
from tsunami.core.inventory_edit import CUSTOMERS, ROLES, InventoryEditor
from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import SLUG_PATTERN, Platform, require_slug
from tsunami.core.models.inventory import DEVICE_TYPES, Device
from tsunami.core.yamlio import load_yaml

# Device fields a CSV column can fill, in the order a device file lists them.
FIELDS = ("name", "host", "site", "role", "platform", "model", "port", "credential")
FIELD_LABELS = {
    "name": "Name",
    "host": "Host",
    "site": "Site",
    "role": "Type",
    "platform": "Platform",
    "model": "Model",
    "port": "Port",
    "credential": "Login",
}
REQUIRED_FIELDS = ("name", "host", "site", "role", "platform")

# Header names (lowercase, spaces, dashes, dots and underscores removed) per field.
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "hostname", "device", "devicename"),
    "host": ("host", "ip", "ipaddress", "ipaddr", "mgmtip", "managementip", "address"),
    "site": ("site", "sitename"),
    "role": ("type", "devicetype", "role"),
    "platform": ("platform", "os", "operatingsystem"),
    "model": ("model", "hwmodel"),
    "port": ("port", "sshport"),
    "credential": ("login", "loginname", "credential", "credentialname"),
}

PLATFORM_ALIASES: dict[str, str] = {
    "ios": "ios",
    "ciscoios": "ios",
    "iosxe": "ios-xe",
    "ciscoiosxe": "ios-xe",
    "ciscoxe": "ios-xe",
    "aoscx": "aos-cx",
    "arubaoscx": "aos-cx",
    "arubacx": "aos-cx",
    "aoss": "aos-s",
    "arubaoss": "aos-s",
    "procurve": "aos-s",
    "hpprocurve": "aos-s",
    "hpeprocurve": "aos-s",
    "arubaprocurve": "aos-s",
}

TYPE_ALIASES: dict[str, str] = {
    "distribution": "distribution",
    "dist": "distribution",
    "wireless": "wlc",
    "wirelesscontroller": "wlc",
    "wap": "ap",
    "accesspoint": "ap",
}

MAX_FILE_BYTES = 5_000_000
DELIMITERS = (",", ";", "\t")
_SECRET_WORDS = frozenset(
    {"pass", "pwd", "passwd", "password", "secret", "enable", "key", "token", "psk", "community"}
)
_SECRET_PARTS = ("password", "passwd", "passphrase", "secret")
_IPV4_LIKE = re.compile(r"[0-9.]+")
_HOSTNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SLUG = re.compile(SLUG_PATTERN)

OK, WARNING, ERROR, SKIPPED = "ok", "warning", "error", "skipped"


class ImportRefused(TsunamiError):
    """The file or the request cannot be imported. Nothing was read further or written."""


def _key(text: str) -> str:
    return re.sub(r"[\s_.\-/]+", "", text.lower())


# -- reading the file ------------------------------------------------------------------


@dataclass(frozen=True)
class CsvTable:
    path: Path
    delimiter: str
    headers: list[str]
    rows: list[tuple[int, list[str]]]  # (row number as a spreadsheet shows it, cells)


def secret_headers(headers: Iterable[str]) -> list[str]:
    """Headers that look like they hold a password, secret, enable or key."""
    found = []
    for header in headers:
        words = set(re.sub(r"([a-z])([A-Z])", r"\1 \2", header).lower().replace("_", " ").split())
        words |= set(re.split(r"[^a-z0-9]+", header.lower()))
        squashed = _key(header)
        if words & _SECRET_WORDS or any(part in squashed for part in _SECRET_PARTS):
            found.append(header)
    return found


def read_csv(path: Path | str) -> CsvTable:
    """Parse a CSV (utf-8, with or without a BOM; comma, semicolon or tab separated).

    Raises ImportRefused for a file that cannot be read or has a secret-looking column;
    the rows of such a file are never read.
    """
    path = Path(path)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ImportRefused(f"{path.name} is too large for an import (over 5 MB).")
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise ImportRefused(f"{path.name} is not UTF-8 text. Save it as CSV UTF-8.") from None
    except OSError as exc:
        raise ImportRefused(f"Cannot read {path.name}: {exc.strerror or exc}") from None
    first = next((line for line in text.splitlines() if line.strip()), "")
    if not first:
        raise ImportRefused(f"{path.name} is empty.")
    delimiter = _sniff(first)
    records = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    headers: list[str] | None = None
    rows: list[tuple[int, list[str]]] = []
    try:
        for number, record in enumerate(records, start=1):
            if not any(cell.strip() for cell in record):
                continue
            if headers is None:
                headers = [cell.strip() for cell in record]
                bad = secret_headers(headers)
                if bad:
                    raise ImportRefused(
                        "This file has a column that looks like it holds a secret "
                        f"({', '.join(repr(h) for h in bad)}). A CSV must never carry "
                        "secrets. Add logins in Credentials, and let the CSV only name "
                        "the login a device uses."
                    )
                continue
            rows.append((number, [cell.strip() for cell in record]))
    except csv.Error as exc:
        raise ImportRefused(f"{path.name} is not valid CSV: {exc}") from None
    if headers is None:  # pragma: no cover - an all-blank file is caught above
        raise ImportRefused(f"{path.name} is empty.")
    return CsvTable(path, delimiter, headers, rows)


def _sniff(line: str) -> str:
    best, best_count = ",", 1
    for delimiter in DELIMITERS:
        count = len(next(csv.reader([line], delimiter=delimiter), []))
        if count > best_count:
            best, best_count = delimiter, count
    return best


# -- mapping ----------------------------------------------------------------------------


@dataclass
class ColumnMapping:
    """Where each device field comes from: a CSV column (header name), or one fixed value
    for every row. A fixed value wins over a column."""

    columns: dict[str, str] = field(default_factory=dict)
    fixed: dict[str, str] = field(default_factory=dict)


def auto_map(headers: Iterable[str]) -> ColumnMapping:
    """Map headers to fields by name: case, spaces, dashes and underscores are ignored."""
    headers = [h for h in headers if h.strip()]
    keyed = [(_key(h), h) for h in headers]
    mapping = ColumnMapping()
    used: set[str] = set()
    for name in FIELDS:
        for alias in HEADER_ALIASES[name]:
            match = next((h for k, h in keyed if k == alias and h not in used), None)
            if match is not None:
                mapping.columns[name] = match
                used.add(match)
                break
    return mapping


# -- checking rows ----------------------------------------------------------------------


@dataclass(frozen=True)
class RowResult:
    number: int
    fields: dict[str, Any]  # what the row says; the raw text where it could not be read
    status: str  # ok, warning, error or skipped
    reason: str = ""

    @property
    def importable(self) -> bool:
        return self.status in (OK, WARNING)


@dataclass(frozen=True)
class ImportPlan:
    customer: str
    rows: list[RowResult]
    new_sites: list[str]  # sites the importable rows need and the customer lacks

    @property
    def ready(self) -> int:
        return sum(1 for r in self.rows if r.importable)

    @property
    def warnings(self) -> int:
        return sum(1 for r in self.rows if r.status == WARNING)

    @property
    def errors(self) -> int:
        return sum(1 for r in self.rows if r.status == ERROR)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.rows if r.status == SKIPPED)

    def devices(self) -> list[dict[str, Any]]:
        return [dict(r.fields) for r in self.rows if r.importable]


@dataclass(frozen=True)
class ImportSummary:
    imported: int
    skipped: int
    errors: int
    sites_created: list[str]

    def message(self) -> str:
        parts = [f"Imported {self.imported} device{'' if self.imported == 1 else 's'}"]
        if self.skipped:
            parts.append(f"skipped {self.skipped} already present")
        if self.errors:
            parts.append(f"{self.errors} row{'' if self.errors == 1 else 's'} with errors left out")
        if self.sites_created:
            parts.append(
                f"created site{'' if len(self.sites_created) == 1 else 's'} "
                + ", ".join(self.sites_created)
            )
        return ", ".join(parts) + "."


@dataclass
class _Inventory:
    sites: set[str]
    roles: set[str]
    devices: dict[str, dict[str, Any]]


def _load_inventory(root: Path, customer: str) -> _Inventory:
    require_slug(customer)
    folder = root / CUSTOMERS / customer
    if not (folder / "customer.yml").is_file():
        raise ImportRefused(f"Unknown customer {customer!r}.")
    sites_dir = folder / "sites"
    sites = {p.stem for p in sites_dir.glob("*.yml")} if sites_dir.is_dir() else set()
    roles_dir = root / ROLES
    roles = {p.stem for p in roles_dir.glob("*.yml")} if roles_dir.is_dir() else set()
    devices: dict[str, dict[str, Any]] = {}
    path = folder / "devices.yml"
    if path.is_file():
        data = load_yaml(path).data  # YamlLoadError is a TsunamiError with file and line
        if data is None:
            data = []
        if not isinstance(data, list):
            raise ImportRefused("devices.yml has an unexpected layout; edit it by hand.")
        for item in data:
            if isinstance(item, Mapping) and isinstance(item.get("name"), str):
                devices[item["name"]] = dict(item)
    return _Inventory(sites, roles, devices)


def _resolve_type(text: str, roles: Collection[str]) -> str | None:
    key = _key(text)
    for slug, label in DEVICE_TYPES.items():
        if key in (_key(slug), _key(label)):
            return slug
    if key in TYPE_ALIASES:
        return TYPE_ALIASES[key]
    return text if text in roles else None


def _resolve_platform(text: str) -> str | None:
    key = _key(text)
    if key in PLATFORM_ALIASES:
        return PLATFORM_ALIASES[key]
    return next((p.value for p in Platform if _key(p.value) == key), None)


def _resolve_model(text: str) -> str | None:
    catalog = load_catalog()
    if text in catalog.ids():
        return text
    wanted = text.lower()
    for model_id in catalog.ids():
        if catalog.get(model_id).label.lower() == wanted:
            return model_id
    return None


def _bad_slug(what: str, text: str) -> str:
    hint = f" (try {text.strip().lower()!r})" if _SLUG.fullmatch(text.strip().lower()) else ""
    return f"{what} {text!r} must be lowercase letters, digits and dashes{hint}"


def _check_host(text: str) -> str | None:
    if _IPV4_LIKE.fullmatch(text):
        try:
            ipaddress.IPv4Address(text)
        except ValueError:
            return f"host {text!r} is not a valid IP address"
        return None
    if ":" in text:
        try:
            ipaddress.IPv6Address(text)
        except ValueError:
            return f"host {text!r} is not a valid IP address"
        return None
    if not _HOSTNAME.fullmatch(text) or len(text) > 253:
        return f"host {text!r} is not a valid IP address or DNS name"
    return None


@dataclass
class _Draft:
    number: int
    fields: dict[str, Any]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    same: bool = False


def preview(
    root: Path | str,
    customer: str,
    table: CsvTable,
    mapping: ColumnMapping,
    *,
    known_logins: Collection[str] | None = None,
    create_sites: bool = False,
) -> ImportPlan:
    """Check every row of the table against the customer's inventory. Writes nothing.

    known_logins are the Credential Manager login names; None skips that check. A login
    that is not known is a warning (add it in Credentials later), not an error.
    """
    inventory = _load_inventory(Path(root), customer)
    getters = _getters(table, mapping)
    drafts = [
        _check_row(number, cells, getters, inventory, known_logins, create_sites)
        for number, cells in table.rows
    ]
    _check_names(drafts, inventory)
    rows: list[RowResult] = []
    new_sites: list[str] = []
    for draft in drafts:
        if draft.errors:
            status, reason = ERROR, "; ".join(draft.errors)
        elif draft.same:
            status, reason = SKIPPED, "already in the inventory with the same fields"
        elif draft.warnings:
            status, reason = WARNING, "; ".join([*draft.warnings, *draft.notes])
        else:
            status, reason = OK, "; ".join(draft.notes)
        if status in (OK, WARNING):
            site = draft.fields["site"]
            if site not in inventory.sites and site not in new_sites:
                new_sites.append(site)
        rows.append(RowResult(draft.number, draft.fields, status, reason))
    return ImportPlan(customer, rows, new_sites)


def _getters(table: CsvTable, mapping: ColumnMapping) -> dict[str, Any]:
    getters: dict[str, Any] = {}
    for name in FIELDS:
        if name in mapping.fixed:
            value = mapping.fixed[name].strip()
            getters[name] = lambda cells, value=value: value
        elif name in mapping.columns:
            header = mapping.columns[name]
            if header not in table.headers:
                raise ImportRefused(f"The file has no column {header!r}.")
            index = table.headers.index(header)
            getters[name] = lambda cells, index=index: cells[index] if index < len(cells) else ""
    return getters


def _check_row(
    number: int,
    cells: list[str],
    getters: dict[str, Any],
    inventory: _Inventory,
    known_logins: Collection[str] | None,
    create_sites: bool,
) -> _Draft:
    raw = {name: (getters[name](cells).strip() if name in getters else "") for name in FIELDS}
    draft = _Draft(number, {name: raw[name] or None for name in FIELDS})
    fields, errors = draft.fields, draft.errors

    if raw["name"]:
        if not _SLUG.fullmatch(raw["name"]):
            errors.append(_bad_slug("name", raw["name"]))
    else:
        errors.append("missing name")

    if raw["host"]:
        problem = _check_host(raw["host"])
        if problem:
            errors.append(problem)
    else:
        errors.append("missing host")

    site = raw["site"]
    if not site:
        errors.append("missing site")
    elif not _SLUG.fullmatch(site):
        errors.append(_bad_slug("site", site))
    elif site not in inventory.sites:
        if create_sites:
            draft.notes.append(f"site {site!r} will be created")
        else:
            errors.append(f"unknown site {site!r} (tick Create missing sites to add it)")

    if raw["role"]:
        role = _resolve_type(raw["role"], inventory.roles)
        if role is None:
            errors.append(f"unknown type {raw['role']!r}")
        else:
            fields["role"] = role
    else:
        errors.append("missing type")

    if raw["platform"]:
        platform = _resolve_platform(raw["platform"])
        if platform is None:
            errors.append(f"unknown platform {raw['platform']!r}")
        else:
            fields["platform"] = platform
    else:
        errors.append("missing platform")

    if raw["model"]:
        model = _resolve_model(raw["model"])
        if model is None:
            errors.append(f"unknown model {raw['model']!r}")
        else:
            fields["model"] = model
            platform = fields.get("platform")
            if platform in {p.value for p in Platform}:
                wanted = load_catalog().get(model).platform
                if wanted != platform:
                    errors.append(f"model {model!r} runs {wanted}, not {platform}")

    if raw["port"]:
        if raw["port"].isascii() and raw["port"].isdigit() and 1 <= int(raw["port"]) <= 65535:
            fields["port"] = int(raw["port"])
        else:
            errors.append(f"bad port {raw['port']!r} (1 to 65535)")

    if raw["credential"]:
        if not _SLUG.fullmatch(raw["credential"]):
            errors.append(_bad_slug("login", raw["credential"]))
        elif known_logins is not None and raw["credential"] not in known_logins:
            draft.warnings.append(
                f"login {raw['credential']!r} is not in Credential Manager; add it in Credentials"
            )

    if not errors:
        try:
            Device.model_validate({k: v for k, v in fields.items() if v is not None})
        except Exception as exc:  # noqa: BLE001 - pydantic detail is shown as the reason
            errors.append(str(exc).splitlines()[0] if str(exc) else "invalid device")
    return draft


def _check_names(drafts: list[_Draft], inventory: _Inventory) -> None:
    by_name: dict[str, list[_Draft]] = {}
    for draft in drafts:
        name = draft.fields["name"]
        if name and _SLUG.fullmatch(name):
            by_name.setdefault(name, []).append(draft)
    for name, group in by_name.items():
        if len(group) > 1:
            for draft in group:
                others = ", ".join(str(d.number) for d in group if d is not draft)
                draft.errors.append(f"duplicate name in the file (also row {others})")
        existing = inventory.devices.get(name)
        if existing is None:
            continue
        for draft in group:
            if draft.errors:
                continue
            differs = _differences(draft.fields, existing)
            if differs:
                draft.errors.append(f"already in the inventory with a different {differs}")
            else:
                draft.same = True


def _differences(fields: dict[str, Any], existing: dict[str, Any]) -> str:
    """Fields that differ from an existing device. Port, model and login count only when
    the row gives a value, so a file imported again stays 'the same' after those were
    edited in the GUI."""
    differs = [
        FIELD_LABELS[k].lower()
        for k in ("host", "site", "role", "platform")
        if str(existing.get(k)) != str(fields[k])
    ]
    if fields.get("port") is not None and int(existing.get("port", 22)) != fields["port"]:
        differs.append("port")
    for key in ("model", "credential"):
        if fields.get(key) is not None and existing.get(key) != fields[key]:
            differs.append(FIELD_LABELS[key].lower())
    return ", ".join(differs)


# -- writing ----------------------------------------------------------------------------


def apply_import(
    root: Path | str,
    customer: str,
    plan: ImportPlan,
    *,
    editor: InventoryEditor | None = None,
) -> ImportSummary:
    """Write the plan's importable rows (and the sites they need) in one batch.

    Raises InventoryEditError if the inventory refuses the batch, for example because it
    changed since the preview; nothing is written then. With no importable row nothing
    is touched.
    """
    devices = plan.devices()
    if devices:
        editor = editor or InventoryEditor(Path(root))
        editor.add_devices(customer, devices, create_sites=plan.new_sites)
    created = list(plan.new_sites) if devices else []
    return ImportSummary(len(devices), plan.skipped, plan.errors, created)


__all__ = [
    "ColumnMapping",
    "CsvTable",
    "ImportPlan",
    "ImportRefused",
    "ImportSummary",
    "RowResult",
    "apply_import",
    "auto_map",
    "preview",
    "read_csv",
    "secret_headers",
]
