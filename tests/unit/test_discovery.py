"""Site discovery over CDP and LLDP (core/discovery.py): parsers and the chain run.

The IOS fixtures in tests/fixtures/neighbors/ios/ are real output from an IOL 17.18 lab
(addresses rewritten to documentation ranges). The Aruba samples below follow vendor
documentation and are unverified on hardware.
"""

from __future__ import annotations

import ipaddress
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tsunami.core.discovery import (
    Discovery,
    DiscoveryRequest,
    Reason,
    Seed,
    VisitStatus,
    default_scope,
    device_slug,
    discover,
    guess_platform,
    parse_capabilities,
    parse_hostname,
    parse_neighbors,
    parse_scope,
)
from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import Platform
from tsunami.core.secret_store import LoginCredential
from tsunami.core.transports.base import (
    AuthenticationError,
    ExecResult,
    HostKeyMismatchError,
    UnreachableError,
)
from tsunami.core.workspace import load_workspace

FIXTURES = Path(__file__).parents[1] / "fixtures"
NEIGHBORS = FIXTURES / "neighbors" / "ios"
LOGIN = LoginCredential("netops", "Sup3r-Secret-Pw")
SCOPE = parse_scope("192.0.2.0/24")


def fixture(name: str) -> str:
    return (NEIGHBORS / f"{name}.txt").read_text(encoding="utf-8")


# -- parsers -----------------------------------------------------------------------------


def test_cdp_detail_from_iol():
    found = parse_neighbors("ios", "show cdp neighbors detail", fixture("cdp-detail-switch"))
    assert [(n.name, n.address) for n in found] == [
        ("DSC-SW3.lab.local", "192.0.2.53"),
        ("dsc-sw4.lab.local", "192.0.2.54"),
        ("dsc-sw1.lab.local", "192.0.2.51"),
    ]
    first = found[0]
    assert first.protocol == "cdp"
    assert first.slug == "dsc-sw3"
    assert first.capabilities == {"router", "switch"}
    assert (first.local_port, first.remote_port) == ("Ethernet0/1", "Ethernet0/0")
    assert first.platform == Platform.IOS_XE
    assert first.network_device and not first.router_only


def test_cdp_router_neighbor_is_router_only():
    found = parse_neighbors(
        "ios", "show cdp neighbors detail", fixture("cdp-detail-router-neighbor")
    )
    router = next(n for n in found if n.slug == "dsc-r1")
    assert router.address == "192.0.2.56"
    assert router.capabilities == {"router"}
    assert router.router_only


def test_cdp_out_of_scope_neighbor_keeps_its_address():
    found = parse_neighbors("ios", "show cdp neighbors detail", fixture("cdp-detail-out-of-scope"))
    assert ("dsc-sw6", "198.51.100.6") in {(n.slug, n.address) for n in found}


def test_lldp_detail_from_iol():
    found = parse_neighbors("ios", "show lldp neighbors detail", fixture("lldp-detail-switch"))
    assert [(n.slug, n.address, n.local_port, n.remote_port) for n in found] == [
        ("dsc-sw1", "192.0.2.51", "Et0/0", "Et0/1"),
        ("dsc-sw3", "192.0.2.53", "Et0/1", "Et0/0"),
        ("dsc-sw4", "192.0.2.54", "Et0/2", "Et0/1"),
    ]
    assert found[0].protocol == "lldp"
    assert found[0].capabilities == {"bridge", "router"}
    assert found[0].platform == Platform.IOS_XE
    assert "Version 17.18.2" in found[0].description


def test_a_link_with_cdp_off_shows_only_in_lldp():
    cdp = parse_neighbors("ios", "show cdp neighbors detail", fixture("cdp-detail-cdp-off-link"))
    lldp = parse_neighbors("ios", "show lldp neighbors detail", fixture("lldp-detail-lldp-only"))
    assert {n.slug for n in cdp} == {"dsc-sw2"}
    assert {n.slug for n in lldp} == {"dsc-sw2", "dsc-sw5"}


def test_cdp_or_lldp_turned_off_gives_no_neighbors():
    assert parse_neighbors("ios", "show cdp neighbors detail", "% CDP is not enabled") == []
    assert parse_neighbors("ios", "show lldp neighbors detail", "% LLDP is not enabled") == []


def test_hostname_from_show_version():
    assert parse_hostname("ios", {"show version": fixture("show-version-l2")}) == "DSC-SW3"
    assert parse_hostname("ios", {"show version": fixture("show-version-router")}) == "dsc-r1"
    assert parse_hostname("ios", {"show version": ""}) is None


def test_hostname_on_aruba():
    cx = {"show system": "Hostname         : core-1\nSystem Description : x"}
    aos_s = {"show system information": "  System Name        : edge-2\n  System Contact :"}
    assert parse_hostname("aos-cx", cx) == "core-1"
    assert parse_hostname("aos-s", aos_s) == "edge-2"


@pytest.mark.parametrize(
    ("advertised", "slug"),
    [
        ("DSC-SW3.lab.local", "dsc-sw3"),
        ("core_sw 01", "core-sw-01"),
        ("n9k-1(FDO12345678)", "n9k-1"),
        ("SEP001122334455", "sep001122334455"),
        ("--edge--", "edge"),
        ("192.0.2.9", None),
        ("", None),
        ("...", None),
    ],
)
def test_device_slug(advertised, slug):
    assert device_slug(advertised) == slug


@pytest.mark.parametrize(
    ("text", "platform"),
    [
        ("Cisco IOS Software [IOSXE], Linux Software, Version 17.18.2", Platform.IOS_XE),
        ("Cisco IOS XE Software, Version 17.09.04a", Platform.IOS_XE),
        (
            "Cisco IOS Software, C2960X Software (C2960X-UNIVERSALK9-M), Version 15.2(7)E8",
            Platform.IOS,
        ),
        ("Aruba JL658A 6300M 24SFP+ 4SFP56 Swch FL.10.13.1010", Platform.AOS_CX),
        ("ArubaOS-CX 10.10", Platform.AOS_CX),
        (
            "Aruba JL256A 2930F-48G-PoE+-4SFP+ Switch, revision WC.16.10.0012, ROM WC.16.01.0008",
            Platform.AOS_S,
        ),
        ("HP J9727A 2920-24G-PoE+ Switch, revision WB.16.10.0009", Platform.AOS_S),
        ("ProCurve J9086A Switch 2610-24/12PWR", Platform.AOS_S),
        ("Cisco Nexus Operating System (NX-OS) Software 9.3(8)", None),
        ("Cisco IP Phone 8845", None),
        ("", None),
    ],
)
def test_guess_platform(text, platform):
    assert guess_platform(text) == platform


def test_capabilities_words_and_letters():
    assert parse_capabilities("Router Switch IGMP") == {"router", "switch"}
    assert parse_capabilities("B,R") == {"bridge", "router"}
    assert parse_capabilities("B,T") == {"bridge", "phone"}
    assert parse_capabilities("Host Phone Two-port Mac Relay") == {"host", "phone"}
    assert parse_capabilities("bridge, router") == {"bridge", "router"}
    assert parse_capabilities("WLAN-access-point") == {"ap"}
    assert parse_capabilities("") == frozenset()


AOS_S_LLDP = """
 LLDP Remote Device Information Detail

  Local Port   : 24
  ChassisType  : mac-address
  ChassisId    : 00 11 22 33 44 55
  PortType     : local
  PortId       : 49
  SysName      : core-sw1
  System Descr : Aruba JL256A 2930F-48G-PoE+-4SFP+ Switch, revision WC.16.10.0012, ROM
                 WC.16.01.0008 (/ws/swbuildm/rel_onyx_qaoff/code/build/anm(swbuildm_rel)
  PortDescr    : 49

  System Capabilities Supported  : bridge, router
  System Capabilities Enabled    : bridge, router

  Remote Management Address
     Type    : ipv6
     Address : fe80::211:22ff:fe33:4455
     Type    : ipv4
     Address : 192.0.2.30

------------------------------------------------------------------------------
  Local Port   : 3
  ChassisType  : network-address
  ChassisId    : 192.0.2.77
  PortType     : mac-address
  PortId       : 00 aa bb cc dd ee
  SysName      : SEP00AABBCCDDEE
  System Descr : Cisco IP Phone 8845
  PortDescr    : SW PORT

  System Capabilities Supported  : bridge, telephone
  System Capabilities Enabled    : bridge, telephone

  Remote Management Address
     Type    : ipv4
     Address : 192.0.2.77
"""

AOS_S_CDP = """
 CDP neighbors information

  Port : 24
  Device ID : core-sw2.example.local
  Address Type : IP
  Address : 192.0.2.31
  Platform : cisco C9300-48P
  Capability : Router Switch
  Device Port : GigabitEthernet1/0/48
  Version : Cisco IOS Software [Cupertino], Catalyst L3 Switch Software, Version 17.9.4
------------------------------------------------------------------------------
"""

AOS_CX_LLDP = """
LLDP Neighbor Information
=========================

Port                           : 1/1/49
Neighbor Entries               : 1
Neighbor Chassis-Name          : agg-1
Neighbor Chassis-Description   : Aruba JL658A 6300M 24SFP+ 4SFP56 Swch FL.10.13.1010
Neighbor Chassis-ID            : 88:3a:30:00:00:01
Neighbor Management-Address    : 192.0.2.40
Chassis Capabilities Available : Bridge, Router
Chassis Capabilities Enabled   : Bridge, Router
Neighbor Port-ID               : 1/1/1
TTL                            : 120

Port                           : 1/1/2
Neighbor Entries               : 1
Neighbor Chassis-Name          : ap-lobby
Neighbor Chassis-Description   : ArubaOS (MODEL: 535)
Neighbor Management-Address    : 192.0.2.80
Chassis Capabilities Available : Bridge, WLAN
Chassis Capabilities Enabled   : WLAN
Neighbor Port-ID               : eth0
"""


def test_aos_s_lldp_detail():
    found = parse_neighbors("aos-s", "show lldp info remote-device detail", AOS_S_LLDP)
    switch, phone = found
    assert (switch.slug, switch.address, switch.local_port, switch.remote_port) == (
        "core-sw1",
        "192.0.2.30",  # IPv4 preferred over the link-local IPv6 listed first
        "24",
        "49",
    )
    assert switch.platform == Platform.AOS_S
    assert "WC.16.01.0008" in switch.description  # wrapped description joined
    assert switch.capabilities == {"bridge", "router"}
    assert phone.capabilities == {"bridge", "phone"}
    assert phone.network_device  # a bridge capability; its platform keeps it from being followed
    assert phone.platform is None


def test_aos_s_cdp_detail():
    (neighbor,) = parse_neighbors("aos-s", "show cdp neighbors detail", AOS_S_CDP)
    assert (neighbor.slug, neighbor.address) == ("core-sw2", "192.0.2.31")
    assert neighbor.remote_port == "GigabitEthernet1/0/48"
    assert neighbor.capabilities == {"router", "switch"}


def test_aos_cx_lldp_detail():
    agg, ap = parse_neighbors("aos-cx", "show lldp neighbor-info detail", AOS_CX_LLDP)
    assert (agg.slug, agg.address, agg.local_port, agg.remote_port) == (
        "agg-1",
        "192.0.2.40",
        "1/1/49",
        "1/1/1",
    )
    assert agg.platform == Platform.AOS_CX
    assert ap.capabilities == {"ap"}
    assert not ap.network_device


def test_scope_parsing():
    assert parse_scope("10.1.0.0/16, 192.0.2.7\n198.51.100.0/24") == (
        ipaddress.ip_network("10.1.0.0/16"),
        ipaddress.ip_network("192.0.2.7/32"),
        ipaddress.ip_network("198.51.100.0/24"),
    )
    assert parse_scope("10.1.1.5/24") == (ipaddress.ip_network("10.1.1.0/24"),)
    with pytest.raises(TsunamiError, match="not a network"):
        parse_scope("10.1.1.0/24 lab")
    assert default_scope(["192.0.2.5", "192.0.2.9", "dns.example", "198.51.100.1"]) == (
        ipaddress.ip_network("192.0.2.0/24"),
        ipaddress.ip_network("198.51.100.0/24"),
    )


# -- the chain ---------------------------------------------------------------------------

IOS_XE_BANNER = "Cisco IOS Software [IOSXE], Linux Software (X86_64BI_LINUX_L2), Version 17.18.2"


def cdp_block(name, address, *, caps="Router Switch IGMP", version=IOS_XE_BANNER, port="Et0/1"):
    lines = [
        "-------------------------",
        f"Device ID: {name}",
        "Entry address(es): ",
        *([f"  IP address: {address}"] if address else []),
        f"Platform: Linux Unix,  Capabilities: {caps} ",
        f"Interface: {port},  Port ID (outgoing port): Ethernet0/0",
        "Holdtime : 130 sec",
        "",
        "Version :",
        version,
        "",
        "advertisement version: 2",
        "Management address(es): ",
        *([f"  IP address: {address}"] if address else []),
        "",
    ]
    return "\n".join(lines)


@dataclass
class FakeDevice:
    hostname: str
    neighbors: list[str] = field(default_factory=list)  # cdp_block() texts
    banner: str = IOS_XE_BANNER
    error: Exception | None = None
    model: str | None = None


class FakeSession:
    def __init__(self, device: FakeDevice) -> None:
        self.device = device

    def send_exec(self, command: str, **_: object) -> ExecResult:
        d = self.device
        if command == "show version":
            model = f"\nModel Number : {d.model}" if d.model else ""
            text = f"{d.banner}\n{d.hostname} uptime is 1 minute{model}\n"
        elif command == "show cdp neighbors detail":
            text = "\n".join(d.neighbors) + "\n\nTotal cdp entries displayed : 1"
        elif command == "show lldp neighbors detail":
            text = "% LLDP is not enabled"
        else:
            text = "% Invalid input detected at '^' marker."
        return ExecResult(command, text)

    def close(self) -> None:
        pass


class FakeTransport:
    """Devices by address. Records every target it was asked to open."""

    def __init__(self, devices: dict[str, FakeDevice]) -> None:
        self.devices = devices
        self.opened: list[tuple[str, Platform]] = []
        self.lock = threading.Lock()

    def open(self, target):
        with self.lock:
            self.opened.append((target.host, target.platform))
        device = self.devices.get(target.host)
        if device is None:
            raise UnreachableError(f"{target.device}: connection timed out")
        if device.error is not None:
            raise device.error
        return FakeSession(device)


class FakeHistory:
    def __init__(self) -> None:
        self.facts: dict[str, dict] = {}

    def record_device_facts(self, customer, device, **values):
        self.facts[f"{customer}/{device}"] = values


@pytest.fixture
def root(tmp_path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(FIXTURES / "workspace", root)
    return root


def lab() -> dict[str, FakeDevice]:
    """seed .1 - .2 - .3 - .4, a loop .3/.4 back to .2, a router .5 behind .3, plus
    neighbors that must not be followed."""
    return {
        "192.0.2.1": FakeDevice(
            "site-sw1",
            [
                cdp_block("site-sw2.lab.local", "192.0.2.2"),
                cdp_block(
                    "SEP0011AABBCCDD",
                    "192.0.2.90",
                    caps="Host Phone",
                    version="Cisco IP Phone 8845",
                ),
                cdp_block("far-sw.lab.local", "198.51.100.9"),
                cdp_block("nexus-1(FDO1234)", "192.0.2.91", version="Cisco Nexus Operating System"),
                cdp_block("silent-sw", None),
            ],
        ),
        "192.0.2.2": FakeDevice(
            "site-sw2",
            [
                cdp_block("site-sw1.lab.local", "192.0.2.1"),
                cdp_block("SITE-SW3.lab.local", "192.0.2.3"),
                cdp_block("site-sw4.lab.local", "192.0.2.4"),
            ],
        ),
        "192.0.2.3": FakeDevice(
            "SITE-SW3",
            [
                cdp_block("site-sw2.lab.local", "192.0.2.2"),
                cdp_block("site-sw4.lab.local", "192.0.2.4"),
                cdp_block("edge-r1.lab.local", "192.0.2.5", caps="Router"),
            ],
        ),
        "192.0.2.4": FakeDevice(
            "site-sw4",
            [cdp_block("site-sw2.lab.local", "192.0.2.2"), cdp_block("SITE-SW3", "192.0.2.3")],
        ),
        "192.0.2.5": FakeDevice("edge-r1", [cdp_block("SITE-SW3.lab.local", "192.0.2.3")]),
    }


def request(**overrides) -> DiscoveryRequest:
    values = dict(
        customer="acme",
        site="site-b",
        seeds=(Seed("192.0.2.1", Platform.IOS_XE),),
        scope=SCOPE,
    )
    values.update(overrides)
    return DiscoveryRequest(**values)


def run(root, devices=None, *, events=None, **overrides):
    transport = FakeTransport(devices if devices is not None else lab())
    history = FakeHistory()
    report = discover(
        root,
        request(**overrides),
        login=LOGIN,
        transport=transport,
        history=history,
        on_event=events.append if events is not None else None,
    )
    return report, transport, history


def devices_yml(root: Path) -> dict[str, dict]:
    ws = load_workspace(root)
    assert ws.errors == [], ws.errors
    return {d.name: d for d in ws.customers["acme"].devices}


def by_name(report) -> dict[str, Reason]:
    return {u.name: u.reason for u in report.unlearned}


def test_chain_learns_every_device_and_writes_a_clean_inventory(root):
    events: list = []
    report, transport, history = run(root, events=events)

    assert report.added == ["site-sw1", "site-sw2", "site-sw3", "site-sw4", "edge-r1"]
    assert [v.hop for v in report.visits] == [0, 1, 2, 2, 3]
    devices = devices_yml(root)
    assert devices["site-sw3"].host == "192.0.2.3"  # upper-case hostname slugged
    assert devices["site-sw1"].site == "site-b"
    assert (root / "inventory/customers/acme/sites/site-b.yml").is_file()  # site created
    assert devices["site-sw2"].role == "access"
    assert devices["edge-r1"].role == "router"  # advertised Router only
    assert str(devices["site-sw1"].platform) == "ios-xe"
    # Each device is visited once, despite the loop and CDP reports from both ends.
    assert sorted(h for h, _ in transport.opened) == [f"192.0.2.{i}" for i in range(1, 6)]
    assert history.facts["acme/site-sw2"]["version"] == "17.18.2"
    assert events == [*report.visits[:1], *events[1:]]  # events stream as they happen
    assert len(events) == len(report.visits) + len(report.unlearned)


def test_neighbors_that_are_not_followed_are_reported_once_with_a_reason(root):
    report, _, _ = run(root)
    assert by_name(report) == {
        "SEP0011AABBCCDD": Reason.NOT_NETWORK,
        "far-sw.lab.local": Reason.OUT_OF_SCOPE,
        "nexus-1(FDO1234)": Reason.UNSUPPORTED,
        "silent-sw": Reason.NO_ADDRESS,
    }
    far = next(u for u in report.unlearned if u.name == "far-sw.lab.local")
    assert (far.address, far.seen_from, far.local_port) == ("198.51.100.9", "site-sw1", "Et0/1")
    assert report.summary() == "5 added, 0 already in inventory, 4 unlearned neighbors."


def test_failed_visits_are_unlearned_and_stop_that_branch(root):
    devices = lab()
    devices["192.0.2.2"].error = AuthenticationError(f"site-sw2: login failed for {LOGIN.password}")
    report, transport, _ = run(root, devices)
    assert report.added == ["site-sw1"]
    visit = next(v for v in report.visits if v.host == "192.0.2.2")
    assert visit.status == VisitStatus.LOGIN_FAILED
    assert LOGIN.password not in visit.detail  # error text is redacted
    unlearned = next(u for u in report.unlearned if u.address == "192.0.2.2")
    assert unlearned.reason == Reason.VISIT_FAILED
    assert unlearned.name == "site-sw2.lab.local"
    assert unlearned.detail.startswith("login failed")
    assert "192.0.2.3" not in {h for h, _ in transport.opened}


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (UnreachableError("x: connection timed out"), VisitStatus.UNREACHABLE),
        (HostKeyMismatchError("x: host key changed"), VisitStatus.HOSTKEY_CHANGED),
        (AuthenticationError("x: login failed"), VisitStatus.LOGIN_FAILED),
    ],
)
def test_connect_errors_map_to_statuses(root, error, status):
    devices = lab()
    devices["192.0.2.5"].error = error
    report, _, _ = run(root, devices)
    assert next(v for v in report.visits if v.host == "192.0.2.5").status == status
    assert "edge-r1" not in devices_yml(root)


def test_a_device_already_in_the_inventory_is_read_not_added(root):
    devices = lab()
    devices["203.0.113.11"] = FakeDevice("sa-as01", [cdp_block("site-sw1", "192.0.2.1")])
    report, _, _ = run(
        root,
        devices,
        seeds=(Seed("203.0.113.11", Platform.IOS_XE, name="sa-as01"),),
    )
    first = report.visits[0]
    assert (first.status, first.name) == (VisitStatus.KNOWN, "sa-as01")
    assert "site-sw1" in report.added  # followed from the inventory device


def test_a_neighbor_at_a_known_address_is_not_visited(root):
    devices = lab()
    devices["192.0.2.1"].neighbors.append(cdp_block("other-name", "203.0.113.12"))
    report, transport, _ = run(root, devices)
    assert "203.0.113.12" not in {h for h, _ in transport.opened}
    assert "other-name" not in by_name(report)


def test_a_hostname_clash_is_reported_not_added(root):
    devices = lab()
    devices["192.0.2.5"].hostname = "sa-as01"  # the inventory has sa-as01 at 203.0.113.11
    report, _, _ = run(root, devices)
    visit = next(v for v in report.visits if v.host == "192.0.2.5")
    assert visit.status == VisitStatus.CONFLICT
    assert "203.0.113.11" in visit.detail
    assert devices_yml(root)["sa-as01"].host == "203.0.113.11"


def test_check_only_reads_the_seeds_and_reports_unknown_neighbors(root):
    devices = lab()
    devices["203.0.113.11"] = FakeDevice(
        "sa-as01",
        [
            cdp_block("sa-as02.acme", "203.0.113.12"),  # in the inventory
            cdp_block("new-sw.acme", "192.0.2.60"),
            cdp_block("SEP99", "192.0.2.61", caps="Host Phone", version="Cisco IP Phone"),
        ],
    )
    before = (root / "inventory/customers/acme/devices.yml").read_text()
    report, transport, _ = run(
        root,
        devices,
        add=False,
        scope=(),
        seeds=(Seed("203.0.113.11", Platform.IOS_XE, name="sa-as01"),),
    )
    assert transport.opened == [("203.0.113.11", Platform.IOS_XE)]
    assert by_name(report) == {"new-sw.acme": Reason.NOT_IN_INVENTORY, "SEP99": Reason.NOT_NETWORK}
    assert report.added == []
    assert (root / "inventory/customers/acme/devices.yml").read_text() == before


def test_hop_and_device_limits(root):
    report, _, _ = run(root, max_hops=1)
    assert report.added == ["site-sw1", "site-sw2"]
    assert by_name(report)["SITE-SW3.lab.local"] == Reason.HOP_LIMIT

    root2 = root.parent / "ws2"
    shutil.copytree(FIXTURES / "workspace", root2)
    report, _, _ = run(root2, max_devices=3)
    assert len(report.added) == 3
    assert Reason.DEVICE_LIMIT in by_name(report).values()


def test_cancel_stops_after_the_current_level(root):
    events: list = []
    transport = FakeTransport(lab())
    run_ = Discovery(root, request(), login=LOGIN, transport=transport, on_event=events.append)
    real_emit = run_._emit

    def emit(event):
        real_emit(event)
        run_.cancel()

    run_._emit = emit  # cancel as soon as the seed is reported
    report = run_.run()
    assert report.cancelled
    assert report.added == ["site-sw1"]
    cancelled = [u for u in report.unlearned if u.reason == Reason.CANCELLED]
    assert [u.address for u in cancelled] == ["192.0.2.2"]
    assert report.summary().endswith("(cancelled).")


def test_a_wrong_platform_guess_is_read_again_with_the_reported_one(root):
    devices = lab()
    # .2 advertises an Aruba CX description, but it is an IOS-XE switch.
    devices["192.0.2.1"].neighbors[0] = cdp_block(
        "site-sw2", "192.0.2.2", version="Aruba JL658A 6300M FL.10.13.1010"
    )
    report, transport, _ = run(root, devices)
    assert ("192.0.2.2", Platform.AOS_CX) in transport.opened
    assert ("192.0.2.2", Platform.IOS_XE) in transport.opened
    assert next(v for v in report.visits if v.host == "192.0.2.2").platform == Platform.IOS_XE
    assert "site-sw2" in report.added


def test_model_and_login_are_written_when_known(root):
    devices = lab()
    devices["192.0.2.1"].model = "C9300-48P"
    devices["192.0.2.1"].neighbors = []
    report, _, _ = run(root, devices, device_credential="acme-netadmin", role="distribution")
    device = devices_yml(root)["site-sw1"]
    assert device.model == "cisco-c9300"
    assert device.credential == "acme-netadmin"
    assert device.role == "distribution"


def test_requests_that_cannot_run(root):
    with pytest.raises(TsunamiError, match="scope"):
        Discovery(root, request(scope=()), login=LOGIN, transport=FakeTransport({}))
    with pytest.raises(TsunamiError, match="seed"):
        Discovery(root, request(seeds=()), login=LOGIN, transport=FakeTransport({}))
    with pytest.raises(TsunamiError):
        Discovery(root, request(site="Bad Site"), login=LOGIN, transport=FakeTransport({}))
