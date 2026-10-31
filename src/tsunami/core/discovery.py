"""Discover a site's devices over CDP and LLDP, and find neighbors the inventory lacks.

Discovery starts at seed devices, logs in to each with one login, reads its facts and its
CDP and LLDP neighbors, adds it to the site, and follows every neighbor that is new,
inside the scope (a list of networks) and looks like a supported switch or router. It
goes breadth first until nothing new is left, the hop limit or the device limit.
Check-only mode visits the given devices and reports their neighbors that are not in the
inventory, without following or adding anything.

With `backup` set, each device's running config is read on the same session and
saved as a backup once the device is in the inventory: its first backup, or a new one
when it differs from the newest (backups.save_if_changed). A backup that fails is noted
on the device; it never fails the visit.

Every neighbor that was not learned is reported with the reason: outside the scope, no
management address, not a switch or router, a platform Tsunami does not support, a hop
or device limit, or a visit that failed (unreachable, login failed, host key changed).

Device output is untrusted: names a neighbor advertises become inventory names only
after slugging (lowercase letters, digits and dashes), and nothing from a device reaches
a config line. Error text passes through the redactor.

The IOS parsers are written against IOL 17.18 output from a CML lab
(tests/fixtures/neighbors/ios/). The AOS-CX and AOS-S parsers follow vendor
documentation and are UNVERIFIED on hardware, like the Aruba facts in facts.py.

See design.md, GUI: Inventory (Discover devices).
"""

from __future__ import annotations

import ipaddress
import re
import threading
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from tsunami.core.backups import DISCOVERY_TAG, BackupError, save_if_changed
from tsunami.core.catalog import load_catalog
from tsunami.core.facts import Facts, detect_platform, facts_commands, parse_facts
from tsunami.core.inventory_edit import InventoryEditError, InventoryEditor
from tsunami.core.inventory_import import load_snapshot
from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import PLATFORM_FAMILY, Platform, PlatformFamily, require_slug
from tsunami.core.models.play import ConnectionMethod
from tsunami.core.paths import AppPaths
from tsunami.core.redact import Redactor, redact_exception
from tsunami.core.secret_store import LoginCredential
from tsunami.core.transports.base import (
    AuthenticationError,
    ConnectCancelled,
    ConnectError,
    ConnectTarget,
    HostKeyMismatchError,
    JumpTarget,
    UnreachableError,
)

MAX_WORKERS = 10  # matches the jump host channel cap (OpenSSH MaxSessions defaults to 10)
MAX_DEVICES = 200  # the scale ceiling of one run (design.md, Hard constraints)

NEIGHBOR_COMMANDS: dict[PlatformFamily, tuple[str, ...]] = {
    PlatformFamily.IOS: ("show cdp neighbors detail", "show lldp neighbors detail"),
    # AOS-CX's CDP support is receive-only on many releases and its detail view is per
    # port, so CX discovery reads LLDP only.
    PlatformFamily.AOS_CX: ("show lldp neighbor-info detail",),
    PlatformFamily.AOS_S: ("show lldp info remote-device detail", "show cdp neighbors detail"),
}


# -- neighbors ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Neighbor:
    """One CDP or LLDP neighbor as a device reported it. All text is untrusted."""

    protocol: str  # "cdp" or "lldp"
    name: str  # advertised device id / system name; may be empty
    address: str | None  # management IPv4 or IPv6 address, if one was advertised
    description: str = ""  # platform and software text
    capabilities: frozenset[str] = frozenset()  # router, switch, bridge, phone, ap, host, ...
    local_port: str = ""
    remote_port: str = ""

    @property
    def slug(self) -> str | None:
        return device_slug(self.name)

    @property
    def platform(self) -> Platform | None:
        return guess_platform(self.description)

    @property
    def network_device(self) -> bool:
        """A switch or router (or no capabilities advertised, which is given the benefit
        of the doubt); phones, access points and hosts are not followed."""
        if not self.capabilities:
            return True
        return bool(self.capabilities & {"router", "switch", "bridge"})

    @property
    def router_only(self) -> bool:
        caps = self.capabilities
        return "router" in caps and not caps & {"switch", "bridge"}


_CAPABILITY_WORDS = (
    ("router", "router"),
    ("switch", "switch"),
    ("bridge", "bridge"),
    ("phone", "phone"),
    ("telephone", "phone"),
    ("wlan", "ap"),
    ("access-point", "ap"),
    ("host", "host"),
    ("station", "host"),
    ("repeater", "repeater"),
    ("docsis", "docsis"),
    ("cvta", "phone"),
)
_CAPABILITY_LETTERS = {
    "R": "router",
    "B": "bridge",
    "T": "phone",
    "W": "ap",
    "S": "host",  # LLDP: Station only
    "P": "repeater",
    "C": "docsis",
}


def parse_capabilities(text: str) -> frozenset[str]:
    """Normalize CDP words ('Router Switch IGMP'), LLDP letters ('B,R') and Aruba words
    ('bridge, router', 'WLAN-access-point')."""
    found: set[str] = set()
    for token in re.split(r"[\s,]+", text.strip()):
        if not token:
            continue
        if len(token) == 1 and token in _CAPABILITY_LETTERS:
            found.add(_CAPABILITY_LETTERS[token])
            continue
        lowered = token.lower()
        for word, cap in _CAPABILITY_WORDS:
            if word in lowered:
                found.add(cap)
    return frozenset(found)


def device_slug(name: str) -> str | None:
    """An inventory name from an advertised name: the domain and a trailing serial in
    brackets are dropped, then lowercased with anything else turned into dashes."""
    text = re.sub(r"\(.*?\)\s*$", "", (name or "").strip())
    try:
        ipaddress.ip_address(text)
        return None  # an address is not a name
    except ValueError:
        pass
    text = text.split(".")[0].lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)[:63].strip("-")
    return text or None


_CX_VERSION = re.compile(r"\b[A-Z]{2}\.10\.\d{2}\.\d{4}")
_AOS_S_VERSION = re.compile(r"\b[A-Z]{1,2}\.1[56]\.\d{2}\.\d{4}")


def guess_platform(text: str) -> Platform | None:
    """A neighbor's platform from its CDP version or LLDP system description."""
    if not text:
        return None
    if re.search(r"AOS-CX|ArubaOS-CX", text, re.I) or _CX_VERSION.search(text):
        return Platform.AOS_CX
    if re.search(r"ProCurve|ArubaOS-Switch", text, re.I) or _AOS_S_VERSION.search(text):
        return Platform.AOS_S
    return detect_platform(text)


def _ip(text: str | None) -> str | None:
    if not text:
        return None
    try:
        return str(ipaddress.ip_address(text.strip()))
    except ValueError:
        return None


def _field(pattern: str, text: str) -> str:
    m = re.search(pattern, text, re.M | re.I)
    return m.group(1).strip() if m else ""


def _blocks(text: str) -> list[str]:
    return re.split(r"^\s*-{5,}\s*$", text, flags=re.M)


def _paragraph_after(label: str, block: str) -> str:
    """The lines after a label line up to the next blank line, joined with spaces."""
    m = re.search(rf"^{label}\s*:?\s*\n(.*?)(?:\n\s*\n|\Z)", block, re.M | re.S | re.I)
    if not m:
        return ""
    return " ".join(line.strip() for line in m.group(1).splitlines() if line.strip())


def parse_cdp_ios(text: str) -> list[Neighbor]:
    """`show cdp neighbors detail` on IOS and IOS-XE."""
    out: list[Neighbor] = []
    for block in _blocks(text):
        name = _field(r"^Device ID:\s*(.+?)\s*$", block)
        if not name:
            continue
        mgmt = re.search(
            r"Management address\(es\):\s*\n\s*IP(?:v4)? address:\s*(\S+)", block, re.I
        )
        entry = re.search(r"Entry address\(es\):\s*\n\s*IP(?:v4)? address:\s*(\S+)", block, re.I)
        ports = re.search(
            r"^Interface:\s*([^,]+),\s*Port ID \(outgoing port\):\s*(.+?)\s*$", block, re.M | re.I
        )
        platform = _field(r"^Platform:\s*(.+?),\s+Capabilities:", block)
        version = _paragraph_after(r"Version", block)
        out.append(
            Neighbor(
                protocol="cdp",
                name=name,
                address=_ip(mgmt.group(1) if mgmt else None)
                or _ip(entry.group(1) if entry else None),
                description=" ".join(p for p in (platform, version) if p),
                capabilities=parse_capabilities(_field(r"Capabilities:\s*(.*?)\s*$", block)),
                local_port=ports.group(1).strip() if ports else "",
                remote_port=ports.group(2).strip() if ports else "",
            )
        )
    return out


def parse_lldp_ios(text: str) -> list[Neighbor]:
    """`show lldp neighbors detail` on IOS and IOS-XE."""
    out: list[Neighbor] = []
    for block in _blocks(text):
        local = _field(r"^Local Intf:\s*(\S+)", block)
        if not local:
            continue
        name = _field(r"^System Name:\s*(.+?)\s*$", block)
        address = re.search(
            r"Management Addresses:\s*\n(?:\s*\S.*\n)*?\s*IP(?:V6)?:\s*(\S+)", block, re.I
        )
        out.append(
            Neighbor(
                protocol="lldp",
                name=name,
                address=_ip(address.group(1) if address else None),
                description=_paragraph_after(r"System Description", block),
                capabilities=parse_capabilities(
                    _field(r"^Enabled Capabilities:\s*(.+?)\s*$", block)
                ),
                local_port=local,
                remote_port=_field(r"^Port id:\s*(.+?)\s*$", block),
            )
        )
    return out


_KV = re.compile(r"^\s*([A-Za-z][\w /().-]*?)\s*:\s?(.*?)\s*$")


def _kv_records(text: str, start_keys: Iterable[str]) -> list[dict[str, list[str]]]:
    """Aruba detail output: `Key : value` lines; a start key opens a new record. Values
    are kept as lists, because a record can repeat a key (several addresses)."""
    starts = set(start_keys)
    records: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] | None = None
    last: str | None = None
    for line in text.splitlines():
        m = _KV.match(line)
        if m is None:
            stripped = line.strip()
            wrapped = line.startswith("    ") and stripped and set(stripped) - {"-", "="}
            if current is not None and last and wrapped:
                current[last][-1] = f"{current[last][-1]} {stripped}"
            continue
        key, value = m.group(1).strip().lower(), m.group(2)
        if key in starts:
            current = {}
            records.append(current)
        if current is not None:
            current.setdefault(key, []).append(value)
            last = key
    return records


def _first(record: dict[str, list[str]], *keys: str) -> str:
    for key in keys:
        for value in record.get(key, ()):
            if value:
                return value
    return ""


def _first_ip(record: dict[str, list[str]], *keys: str) -> str | None:
    found = [ip for key in keys for v in record.get(key, ()) if (ip := _ip(v))]
    v4 = [ip for ip in found if ":" not in ip]
    return (v4 or found or [None])[0]


def parse_lldp_aos_s(text: str) -> list[Neighbor]:
    """`show lldp info remote-device detail` on AOS-S. UNVERIFIED on hardware."""
    return [
        Neighbor(
            protocol="lldp",
            name=_first(r, "sysname", "system name"),
            address=_first_ip(r, "address"),
            description=_first(r, "system descr", "system description"),
            capabilities=parse_capabilities(_first(r, "system capabilities enabled")),
            local_port=_first(r, "local port"),
            remote_port=_first(r, "portid", "port id"),
        )
        for r in _kv_records(text, ("local port",))
    ]


def parse_cdp_aos_s(text: str) -> list[Neighbor]:
    """`show cdp neighbors detail` on AOS-S. UNVERIFIED on hardware."""
    return [
        Neighbor(
            protocol="cdp",
            name=_first(r, "device id"),
            address=_first_ip(r, "address"),
            description=" ".join(p for p in (_first(r, "platform"), _first(r, "version")) if p),
            capabilities=parse_capabilities(_first(r, "capability", "capabilities")),
            local_port=_first(r, "port"),
            remote_port=_first(r, "device port"),
        )
        for r in _kv_records(text, ("port",))
        if _first(r, "device id")
    ]


def parse_lldp_aos_cx(text: str) -> list[Neighbor]:
    """`show lldp neighbor-info detail` on AOS-CX. UNVERIFIED on hardware."""
    return [
        Neighbor(
            protocol="lldp",
            name=_first(r, "neighbor chassis-name", "neighbor system-name"),
            address=_first_ip(r, "neighbor management-address", "neighbor mgmt-address"),
            description=_first(r, "neighbor chassis-description", "neighbor system-description"),
            capabilities=parse_capabilities(_first(r, "chassis capabilities enabled")),
            local_port=_first(r, "port"),
            remote_port=_first(r, "neighbor port-id"),
        )
        for r in _kv_records(text, ("port",))
        if _first(r, "neighbor chassis-name", "neighbor system-name", "neighbor chassis-id")
    ]


def parse_neighbors(family: PlatformFamily | str, command: str, text: str) -> list[Neighbor]:
    """Neighbors from one of NEIGHBOR_COMMANDS. Error text (CDP or LLDP turned off) gives
    an empty list."""
    fam = PlatformFamily(family)
    cdp = "cdp" in command
    if fam is PlatformFamily.AOS_S:
        return parse_cdp_aos_s(text) if cdp else parse_lldp_aos_s(text)
    if fam is PlatformFamily.AOS_CX:
        return [] if cdp else parse_lldp_aos_cx(text)
    return parse_cdp_ios(text) if cdp else parse_lldp_ios(text)


def parse_hostname(family: PlatformFamily | str, outputs: dict[str, str]) -> str | None:
    """The device's hostname from its facts commands."""
    fam = PlatformFamily(family)
    if fam is PlatformFamily.AOS_CX:
        return _field(r"^Hostname\s*:\s*(\S+)", outputs.get("show system", "")) or None
    if fam is PlatformFamily.AOS_S:
        info = outputs.get("show system information", "")
        return _field(r"System Name\s*:\s*(\S+)", info) or None
    return _field(r"^(\S+) uptime is", outputs.get("show version", "")) or None


# -- the run -------------------------------------------------------------------------


class VisitStatus(StrEnum):
    ADDED = "added"
    KNOWN = "in inventory"
    CONFLICT = "name clash"
    UNREACHABLE = "unreachable"
    LOGIN_FAILED = "login failed"
    HOSTKEY_CHANGED = "host key changed"
    FAILED = "failed"
    CANCELLED = "cancelled"


LEARNED = frozenset({VisitStatus.ADDED, VisitStatus.KNOWN})


class Reason(StrEnum):
    VISIT_FAILED = "could not be learned"
    OUT_OF_SCOPE = "outside the scope"
    NO_ADDRESS = "no management address"
    NOT_NETWORK = "not a switch or router"
    UNSUPPORTED = "platform not supported"
    HOP_LIMIT = "beyond the hop limit"
    DEVICE_LIMIT = "device limit reached"
    NOT_IN_INVENTORY = "not in the inventory"  # check-only mode
    CANCELLED = "not visited (cancelled)"


@dataclass(frozen=True)
class Seed:
    host: str
    platform: Platform
    name: str | None = None  # the inventory name, when the seed is an inventory device


@dataclass(frozen=True)
class DiscoveryRequest:
    customer: str
    site: str
    seeds: tuple[Seed, ...]
    scope: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    role: str = "access"  # type of added devices; a router-only neighbor becomes `router`
    add: bool = True  # False: check only (visit the seeds, follow and add nothing)
    max_hops: int = 5
    max_devices: int = MAX_DEVICES
    device_credential: str | None = None  # written on added devices when set
    backup: bool = False  # save each inventory device's running config


@dataclass(frozen=True)
class DeviceVisit:
    host: str
    status: VisitStatus
    name: str | None = None  # the inventory name (added or existing)
    platform: Platform | None = None
    model: str | None = None  # catalog id
    version: str | None = None
    hop: int = 0
    neighbors: int = 0
    detail: str = ""
    backup: str = ""  # what happened to its config backup ("" when none was taken)


@dataclass(frozen=True)
class Unlearned:
    """A neighbor that is not in the inventory and was not added."""

    name: str  # as advertised (untrusted)
    address: str | None
    seen_from: str  # the device that reported it (inventory name or host)
    reason: Reason
    local_port: str = ""
    remote_port: str = ""
    description: str = ""
    detail: str = ""


@dataclass(frozen=True)
class DiscoveryReport:
    visits: list[DeviceVisit]
    unlearned: list[Unlearned]
    cancelled: bool = False

    @property
    def added(self) -> list[str]:
        return [v.name for v in self.visits if v.status == VisitStatus.ADDED and v.name]

    def summary(self) -> str:
        known = sum(1 for v in self.visits if v.status == VisitStatus.KNOWN)
        # Inventory devices that could not be read; a failed new device is in unlearned.
        unread = sum(1 for v in self.visits if v.status not in LEARNED and v.name)
        count = len(self.unlearned)
        parts = [f"{len(self.added)} added", f"{known} already in inventory"]
        if unread:
            parts.append(f"{unread} inventory device{'' if unread == 1 else 's'} not read")
        parts.append(f"{count} unlearned neighbor{'' if count == 1 else 's'}")
        return ", ".join(parts) + (" (cancelled)" if self.cancelled else "") + "."


@dataclass
class _Item:
    host: str
    platform: Platform
    hop: int
    seen_from: str = "seed"
    advertised: str = ""  # the name a neighbor advertised for it
    known_name: str | None = None  # an inventory device
    router_only: bool = False
    neighbor: Neighbor | None = None


@dataclass
class _Outcome:
    item: _Item
    status: VisitStatus | None = None  # None: logged in and read
    detail: str = ""
    facts: Facts = field(default_factory=Facts)
    platform: Platform | None = None
    hostname: str | None = None
    neighbors: list[Neighbor] = field(default_factory=list)
    running: str | None = field(default=None, repr=False)  # unredacted; only for the backup
    backup_error: str = ""


def parse_scope(text: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Networks from comma, space or newline separated CIDRs (a bare address is a /32)."""
    nets = []
    for token in re.split(r"[\s,;]+", text.strip()):
        if token:
            try:
                nets.append(ipaddress.ip_network(token, strict=False))
            except ValueError:
                raise TsunamiError(
                    f"{token!r} is not a network (write it as 10.1.1.0/24)"
                ) from None
    return tuple(nets)


def default_scope(hosts: Iterable[str]) -> tuple[ipaddress.IPv4Network, ...]:
    """The /24 of every IPv4 seed: a starting point for the scope field."""
    nets: list[ipaddress.IPv4Network] = []
    for host in hosts:
        try:
            net = ipaddress.ip_network(f"{ipaddress.IPv4Address(host)}/24", strict=False)
        except ValueError:
            continue
        if net not in nets:
            nets.append(net)
    return tuple(nets)


class Discovery:
    """One discovery run. Call run() once (it blocks); cancel() from another thread."""

    def __init__(
        self,
        root: Path | str,
        request: DiscoveryRequest,
        *,
        login: LoginCredential,
        transport: Any,
        jump: JumpTarget | None = None,
        editor: InventoryEditor | None = None,
        history: Any = None,  # core.history.History: facts of each learned device
        on_event: Callable[[DeviceVisit | Unlearned], None] | None = None,
        cancel: threading.Event | None = None,
        workers: int = MAX_WORKERS,
        connect_timeout: float = 15,
        command_timeout: float = 60,
        allow_new_host_key: bool = True,
        paths: AppPaths | None = None,  # where backups go, when request.backup
        keep: int = 30,  # backups kept per device
        redactor: Redactor | None = None,  # every stored secret; the login is added
    ) -> None:
        require_slug(request.customer)
        require_slug(request.site)
        if not request.seeds:
            raise TsunamiError("discovery needs at least one seed device")
        if request.add and not request.scope:
            raise TsunamiError("discovery needs a scope: the networks it may follow neighbors into")
        if request.backup and paths is None:
            raise TsunamiError("discovery needs the backups folder to save configs")
        self.root = Path(root)
        self.request = request
        self.login = login
        self.transport = transport
        self.jump = jump
        self.editor = editor or InventoryEditor(self.root)
        self.history = history
        self.on_event = on_event
        self.cancel_event = cancel or threading.Event()
        self.workers = max(1, min(workers, MAX_WORKERS))
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self.allow_new_host_key = allow_new_host_key
        login_values = [login.password, login.enable or ""]
        self.redactor = redactor.with_values(login_values) if redactor else Redactor(login_values)
        self.paths = paths
        self.keep = keep
        self.visits: list[DeviceVisit] = []
        self.unlearned: list[Unlearned] = []
        self._names: dict[str, str] = {}  # inventory name -> host
        self._hosts: dict[str, str] = {}  # host -> inventory name
        self._sites: set[str] = set()
        self._claimed: set[str] = set()  # names and hosts queued or visited in this run
        self._reported: set[str] = set()  # unlearned neighbors already listed
        self._count = 0  # devices queued, for the device limit

    def cancel(self) -> None:
        self.cancel_event.set()

    # -- driving ----------------------------------------------------------------------

    def run(self) -> DiscoveryReport:
        snapshot = load_snapshot(self.root, self.request.customer)
        self._sites = set(snapshot.sites)
        for name, device in snapshot.devices.items():
            host = str(device.get("host", ""))
            self._names[name] = host
            if host:
                self._hosts[host] = name
        level: list[_Item] = []
        for seed in self.request.seeds:
            if seed.host in self._claimed:
                continue
            known = seed.name or self._hosts.get(seed.host)
            level.append(_Item(seed.host, seed.platform, 0, known_name=known))
            self._claim(seed.host, known)
        self._count = len(level)
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            while level:
                if self.cancel_event.is_set():
                    for item in level:
                        self._unvisited(item)
                    break
                outcomes = list(pool.map(self._visit, level))
                next_level: list[_Item] = []
                for outcome in outcomes:
                    next_level.extend(self._process(outcome))
                level = next_level
        return DiscoveryReport(list(self.visits), list(self.unlearned), self.cancel_event.is_set())

    def _claim(self, host: str | None, name: str | None) -> None:
        for key in (host, name):
            if key:
                self._claimed.add(key)

    def _emit(self, event: DeviceVisit | Unlearned) -> None:
        if isinstance(event, DeviceVisit):
            self.visits.append(event)
        else:
            self.unlearned.append(event)
        if self.on_event is not None:
            self.on_event(event)

    # -- one device (worker threads) --------------------------------------------------

    def _target(self, item: _Item, platform: Platform) -> ConnectTarget:
        return ConnectTarget(
            customer=self.request.customer,
            device=item.known_name or item.host,
            host=item.host,
            port=22,
            platform=platform,
            method=ConnectionMethod.SSH,
            credential=self.login,
            jump=self.jump,
            connect_timeout=self.connect_timeout,
            command_timeout=self.command_timeout,
            allow_new_host_key=self.allow_new_host_key,
            cancel=self.cancel_event,
        )

    def _visit(self, item: _Item) -> _Outcome:
        if self.cancel_event.is_set():
            return _Outcome(item, VisitStatus.CANCELLED)
        outcome = self._read(item, item.platform)
        if (
            outcome.status is None
            and outcome.platform is not None
            and PLATFORM_FAMILY[outcome.platform] != PLATFORM_FAMILY[item.platform]
        ):
            # The neighbor's description pointed at the wrong CLI: read it again with the
            # platform the device itself reports.
            outcome = self._read(item, outcome.platform)
        return outcome

    def _read(self, item: _Item, platform: Platform) -> _Outcome:
        try:
            session = self.transport.open(self._target(item, platform))
        except ConnectCancelled:
            return _Outcome(item, VisitStatus.CANCELLED)
        except HostKeyMismatchError as exc:
            return _Outcome(item, VisitStatus.HOSTKEY_CHANGED, self._text(exc))
        except AuthenticationError as exc:
            return _Outcome(item, VisitStatus.LOGIN_FAILED, self._text(exc))
        except UnreachableError as exc:
            return _Outcome(item, VisitStatus.UNREACHABLE, self._text(exc))
        except ConnectError as exc:
            return _Outcome(item, VisitStatus.FAILED, self._text(exc))
        try:
            family = PLATFORM_FAMILY[platform]
            outputs = {cmd: session.send_exec(cmd).output for cmd in facts_commands(family)}
            facts = parse_facts(family, outputs)
            detected = facts.platform or platform
            outcome = _Outcome(
                item, facts=facts, platform=detected, hostname=parse_hostname(family, outputs)
            )
            if PLATFORM_FAMILY[detected] != family:
                return outcome  # read again with the right driver (see _visit)
            for command in NEIGHBOR_COMMANDS[family]:
                if self.cancel_event.is_set():
                    break
                outcome.neighbors += parse_neighbors(
                    family, command, session.send_exec(command).output
                )
            if self.request.backup and not self.cancel_event.is_set():
                try:
                    outcome.running = session.running_config()
                except Exception as exc:  # noqa: BLE001 - noted on the device, never fatal
                    outcome.backup_error = redact_exception(exc, self.redactor)
            return outcome
        except Exception as exc:  # noqa: BLE001 - one device failing never stops the run
            return _Outcome(item, VisitStatus.FAILED, redact_exception(exc, self.redactor))
        finally:
            try:
                session.close()
            except Exception:  # noqa: BLE001, S110 - closing is best effort
                pass

    def _text(self, exc: BaseException) -> str:
        return self.redactor.text(str(exc))

    # -- results (the run's thread, one at a time) -----------------------------------

    def _process(self, outcome: _Outcome) -> list[_Item]:
        item = outcome.item
        if outcome.status is not None:
            self._failed(outcome)
            return []
        platform = outcome.platform or item.platform
        name = item.known_name or self._name_for(outcome)
        status, detail = self._place(item, name)
        self._claim(item.host, name)
        model = self._model(outcome.facts, platform)
        if status is None:
            status, detail = self._add(item, name, platform, model)
        if status in LEARNED and self.history is not None:
            self._record_facts(name, platform, outcome.facts)
        backup = self._backup(item, name, platform, outcome) if status in LEARNED else ""
        self._emit(
            DeviceVisit(
                host=item.host,
                status=status,
                name=name if status in LEARNED else None,
                platform=platform,
                model=model,
                version=outcome.facts.version,
                hop=item.hop,
                neighbors=len(outcome.neighbors),
                detail=detail,
                backup=backup,
            )
        )
        if status not in LEARNED:
            self._report(
                item, Reason.VISIT_FAILED, f"{status}: {detail}" if detail else str(status)
            )
            return []
        return self._follow(item, name, outcome.neighbors)

    def _name_for(self, outcome: _Outcome) -> str:
        for candidate in (outcome.hostname, outcome.item.advertised):
            slug = device_slug(candidate or "")
            if slug:
                return slug
        return "device-" + re.sub(r"[^0-9a-f]+", "-", outcome.item.host.lower()).strip("-")

    def _place(self, item: _Item, name: str) -> tuple[VisitStatus | None, str]:
        """KNOWN or CONFLICT if the inventory already has this device or name; None if
        it is new."""
        if item.known_name:
            return VisitStatus.KNOWN, ""
        by_host = self._hosts.get(item.host)
        if by_host is not None:
            detail = "" if by_host == name else f"listed as {by_host}"
            return VisitStatus.KNOWN, detail
        if name in self._names:
            if self._names[name] == item.host:
                return VisitStatus.KNOWN, ""
            return (
                VisitStatus.CONFLICT,
                f"the inventory already has {name} at {self._names[name]}; not added",
            )
        return None, ""

    def _add(
        self, item: _Item, name: str, platform: Platform, model: str | None
    ) -> tuple[VisitStatus, str]:
        if not self.request.add:
            # Check-only mode visits only the seeds; a seed missing from the inventory is
            # reported, never added.
            return VisitStatus.KNOWN, "not in the inventory (check only)"
        role = "router" if item.router_only else self.request.role
        fields: dict[str, Any] = {
            "name": name,
            "host": item.host,
            "site": self.request.site,
            "role": role,
            "platform": platform.value,
        }
        if model:
            fields["model"] = model
        if self.request.device_credential:
            fields["credential"] = self.request.device_credential
        create = [] if self.request.site in self._sites else [self.request.site]
        try:
            self.editor.add_devices(self.request.customer, [fields], create_sites=create)
        except (InventoryEditError, TsunamiError, OSError) as exc:
            return VisitStatus.FAILED, f"not added: {self._text(exc)}"
        self._sites.add(self.request.site)
        self._names[name] = item.host
        self._hosts[item.host] = name
        return VisitStatus.ADDED, f"added as {role}"

    def _model(self, facts: Facts, platform: Platform) -> str | None:
        if not facts.model:
            return None
        matches = [
            e.id for e in load_catalog().match_reported_model(facts.model) if e.platform == platform
        ]
        return matches[0] if len(matches) == 1 else None

    def _backup(self, item: _Item, name: str, platform: Platform, outcome: _Outcome) -> str:
        """Save the config read on the visit under the device's inventory name."""
        if not self.request.backup:
            return ""
        if outcome.backup_error:
            return f"failed: {outcome.backup_error}"
        if outcome.running is None:
            return ""
        inventory_name = item.known_name or self._hosts.get(item.host)
        if inventory_name is None:
            return ""  # check only, and the device is not in the inventory
        assert self.paths is not None
        try:
            saved = save_if_changed(
                self.paths,
                self.request.customer,
                inventory_name,
                outcome.running,
                PLATFORM_FAMILY[platform],
                self.redactor,
                tag=DISCOVERY_TAG,
                keep=self.keep,
            )
        except (BackupError, TsunamiError, OSError) as exc:
            return f"failed: {self._text(exc)}"
        finally:
            outcome.running = None
        return saved.summary

    def _record_facts(self, name: str, platform: Platform, facts: Facts) -> None:
        redact = self.redactor.text
        try:
            self.history.record_device_facts(
                self.request.customer,
                name,
                platform=platform.value,
                model=redact(facts.model) if facts.model else None,
                version=redact(facts.version) if facts.version else None,
                serial=redact(facts.serial) if facts.serial else None,
            )
        except Exception:  # noqa: BLE001, S110 - facts are a convenience, never a failure
            pass

    def _failed(self, outcome: _Outcome) -> None:
        item = outcome.item
        status = outcome.status
        assert status is not None
        self._emit(
            DeviceVisit(
                host=item.host,
                status=status,
                name=item.known_name,
                platform=item.platform,
                hop=item.hop,
                detail=outcome.detail,
            )
        )
        if status == VisitStatus.CANCELLED:
            self._unvisited(item)
        else:
            detail = f"{status}: {outcome.detail}" if outcome.detail else str(status)
            self._report(item, Reason.VISIT_FAILED, detail)

    def _unvisited(self, item: _Item) -> None:
        if item.known_name:
            return  # an inventory device is never "unlearned"
        self._report(item, Reason.CANCELLED)

    def _report(self, item: _Item, reason: Reason, detail: str = "") -> None:
        if item.known_name:
            return
        n = item.neighbor
        self._emit(
            Unlearned(
                name=n.name if n else item.advertised,
                address=item.host,
                seen_from=item.seen_from,
                reason=reason,
                local_port=n.local_port if n else "",
                remote_port=n.remote_port if n else "",
                description=_short(n.description) if n else "",
                detail=detail,
            )
        )

    # -- neighbors --------------------------------------------------------------------

    def _known(self, neighbor: Neighbor) -> bool:
        slug, address = neighbor.slug, neighbor.address
        return bool(
            (slug and (slug in self._names or slug in self._claimed))
            or (address and (address in self._hosts or address in self._claimed))
        )

    def _follow(self, item: _Item, seen_from: str, neighbors: Sequence[Neighbor]) -> list[_Item]:
        queued: list[_Item] = []
        for neighbor in _merge(neighbors):
            if self._known(neighbor):
                continue
            reason = self._why_not(item, neighbor)
            if reason is not None:
                key = neighbor.slug or neighbor.address or neighbor.name
                if key and key in self._reported:
                    continue
                if key:
                    self._reported.add(key)
                self._emit(
                    Unlearned(
                        name=neighbor.name,
                        address=neighbor.address,
                        seen_from=seen_from,
                        reason=reason,
                        local_port=neighbor.local_port,
                        remote_port=neighbor.remote_port,
                        description=_short(neighbor.description),
                    )
                )
                continue
            assert neighbor.address is not None and neighbor.platform is not None
            self._claim(neighbor.address, neighbor.slug)
            self._count += 1
            queued.append(
                _Item(
                    neighbor.address,
                    neighbor.platform,
                    item.hop + 1,
                    seen_from=seen_from,
                    advertised=neighbor.name,
                    router_only=neighbor.router_only,
                    neighbor=neighbor,
                )
            )
        return queued

    def _why_not(self, item: _Item, neighbor: Neighbor) -> Reason | None:
        """Why a new neighbor is not followed, or None to follow it. Check-only mode
        gives the same reasons, then NOT_IN_INVENTORY for one that would be followed."""
        if not neighbor.network_device:
            return Reason.NOT_NETWORK
        if neighbor.address is None:
            return Reason.NO_ADDRESS
        address = ipaddress.ip_address(neighbor.address)
        if self.request.scope and not any(address in net for net in self.request.scope):
            return Reason.OUT_OF_SCOPE
        if neighbor.platform is None:
            return Reason.UNSUPPORTED
        if not self.request.add:
            return Reason.NOT_IN_INVENTORY
        if item.hop + 1 > self.request.max_hops:
            return Reason.HOP_LIMIT
        if self._count >= self.request.max_devices:
            return Reason.DEVICE_LIMIT
        return None


def _merge(neighbors: Sequence[Neighbor]) -> list[Neighbor]:
    """One entry per neighbor device when CDP and LLDP both report it (and per device
    when it is seen on several ports). The first report wins, filled in from later ones."""
    merged: dict[str, Neighbor] = {}
    order: list[str] = []
    for n in neighbors:
        key = n.slug or n.address or f"{n.protocol}:{n.local_port}"
        if key not in merged:
            merged[key] = n
            order.append(key)
            continue
        first = merged[key]
        merged[key] = replace(
            first,
            address=first.address or n.address,
            description=first.description or n.description,
            capabilities=first.capabilities | n.capabilities,
        )
    return [merged[k] for k in order]


def _short(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def discover(root: Path | str, request: DiscoveryRequest, **kwargs: Any) -> DiscoveryReport:
    """Run a discovery to the end. kwargs as for Discovery."""
    return Discovery(root, request, **kwargs).run()


__all__ = [
    "NEIGHBOR_COMMANDS",
    "DeviceVisit",
    "Discovery",
    "DiscoveryReport",
    "DiscoveryRequest",
    "Neighbor",
    "Reason",
    "Seed",
    "Unlearned",
    "VisitStatus",
    "default_scope",
    "device_slug",
    "discover",
    "guess_platform",
    "parse_capabilities",
    "parse_hostname",
    "parse_neighbors",
    "parse_scope",
]
