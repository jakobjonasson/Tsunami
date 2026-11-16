<!-- Research notes gathered 2026-11-15 for structured operational-state checks (state tables).
     Sources are in the table at the end. Items marked UNVERIFIED must be confirmed on a device. -->

# State tables: show commands and sample output per platform

Research date: 2026-11-15. Families: `ios` (IOS 15.x Catalyst 2960-X/3560-CX, IOS-XE 16/17 Catalyst
9200/9300/9500/3850, ISR), `aos-cx` (AOS-CX 10.x), `aos-s` (ArubaOS-Switch 16.x, 2530/2930F).

## 0. How to read this report

**Evidence tags.** Every fact carries `[Kn]`/`[Cn]` (a key into the Sources table at the end) or is
marked **UNVERIFIED (confidence)**. Three kinds of sample output appear, and each block says which:

- **Vendor (verbatim)**: copied from a Cisco page. Cisco's own samples contain tabs and odd spacing
  that a device does not print (see 3.1); use them for tokens, not for column positions.
- **Vendor (layout reconstructed)**: HPE techdocs pages. `arubanetworking.hpe.com` returns HTTP 403 to
  plain clients and to WebFetch; the only fetcher that worked (a JS-rendering one) collapses all
  whitespace inside the example blocks. The tokens and their order are HPE's; **line breaks and column
  spacing were restored by me** and are not authoritative. Real spacing must come from a device.
- **Real device (non-vendor)**: test inputs of two open-source parser projects, ntc-templates [NTC]
  and Cisco's genieparser [GENIE]. These are captures from real devices contributed by users, so column
  spacing is real, but hostnames, descriptions and some addresses were edited by their contributors.
  I replaced remaining names that looked real with placeholders (noted per block). Trailing
  whitespace was kept where the source had it only when it matters, and is otherwise trimmed.

**Method caveats.**
- Cisco pages were readable through the JS-rendering fetcher (pre blocks intact) but pages are huge;
  WebFetch summaries cannot quote more than ~125 characters, so long Cisco blocks came from the fetcher.
- HPE PDFs could not be downloaded (403); everything HPE came from single HTML command pages.
- "Empty table" behavior (feature not configured) is almost never documented. Every such claim below
  is UNVERIFIED unless tagged. Capture these on the CML test bed (IOS-XE) and on real Aruba gear before
  writing parsers that rely on them.

### 0.1 What the repo fixtures already cover

| Fixture (under `tests/fixtures/`) | State table it covers |
|---|---|
| `show/ios/c9300_show_version.txt` | Stack member table (6): the `Switch Ports Model SW Version SW Image Mode` block, one member (`*    1 56 C9300-48P 17.06.04 CAT9K_IOSXE INSTALL`) |
| `show/ios/2960x_show_version.txt`, `3850_show_version.txt`, `c1000_show_version.txt` | None of the tables; standalone switches, no member block (the 2960-X/3850 captures are trimmed) |
| `show/aos-cx/6300_show_version.txt`, `6300_show_system.txt` | None (version/system facts only) |
| `show/aos-s/2930f_show_*.txt`, `5406r_show_modules.txt` | None (version/system/module facts only) |
| `neighbors/ios/*` (CDP/LLDP detail, show version) | None of the ten tables (neighbor discovery only) |

So **no table in this document has a repo fixture yet**, except a single-member IOS-XE stack table
inside `show version`. Every fixture has to be created (CML can produce IOS-XE ones for tables 1-5, 7-10).

### 0.2 Summary: recommended command per table

| Table | ios | aos-cx | aos-s |
|---|---|---|---|
| 1 interfaces | `show interfaces description` (all; status+protocol); `show interfaces status` (switches only) | `show interface brief` | `show interfaces brief` |
| 2 port_channels | `show etherchannel summary` | `show lacp interfaces` (+ `show lacp aggregates`) | `show lacp` (+ `show trunks`) |
| 3 ospf_neighbors | `show ip ospf neighbor` | `show ip ospf neighbors all-vrfs` | `show ip ospf neighbor` |
| 4 bgp_neighbors | `show bgp all summary` (IOS-XE); `show ip bgp summary` | `show bgp all summary` (or `show bgp all-vrfs all summary`) | `show ip bgp summary` |
| 5 eigrp_neighbors | `show ip eigrp neighbors` | n/a | n/a |
| 6 stack | `show switch` (+ `show version` member block) | `show vsf` | `show vsf` (2930F) / `show stacking` (2930M/3810M) |
| 7 vlans | `show vlan brief` | `show vlan` | `show vlans` |
| 8 route summary | `show ip route summary` | `show ip route summary all-vrfs` | `show ip route summary` |
| 9 FHRP | `show standby brief`, `show vrrp brief` | `show vrrp brief`, `show vsx brief` | `show vrrp summary` |
| 10 spanning tree | `show spanning-tree summary` + `show spanning-tree blockedports` | `show spanning-tree` (+ `show spanning-tree summary root`) | `show spanning-tree` |

---

## 1. interfaces

### 1.1 ios

Which command covers what:
- `show interfaces description`: every interface (physical, SVI, loopback, tunnel, sub-interface,
  port-channel) with `Status` (admin+link combined) and `Protocol`; exists on routers and switches.
  Best single command. Interface names are abbreviated (`Gi0/1`, `Vl99`). [NTC] UNVERIFIED that it is
  identical on all IOS-XE releases (high).
- `show ip interface brief`: every interface, full names, `Status`/`Protocol`, plus IP; Status column
  is wide enough for `administratively down`. Exists everywhere. [NTC]
- `show interfaces status`: switch ports only (plus Po), with switch-specific status words
  (`connected`, `notconnect`, `err-disabled`...). Not on ISR routers without a switch module
  (UNVERIFIED, high). No line-protocol column, no SVIs/loopbacks.

Real device (non-vendor) [NTC `cisco_ios/show_interfaces_description`]; one description replaced
with `UPLINK TO acme-sw01`:
```
Interface                      Status         Protocol Description
Vl1                            admin down     down
Vl99                           up             up       10.20.99.0_Switch_mgmt_VLAN
Gi0/1                          down           down     D3 USER
Gi0/8                          up             up       TEST AP
Gi0/9                          admin down     down
Gi0/10                         up             up       UPLINK TO acme-sw01
Gi0/10.10                      deleted        down
Gi0/10.20                      up             up       Carrier VLAN
Gi0/10.30                      reset          down     Garbage
```

Real device (non-vendor) [NTC `cisco_ios/show_ip_interface_brief`]:
```
Interface                  IP-Address      OK? Method Status                Protocol
Ethernet0/0                unassigned      YES NVRAM  up                    up
Ethernet0/0.11             10.0.1.38       YES NVRAM  up                    up
Ethernet0/0.100            unassigned      YES NVRAM  deleted               down
Ethernet0/1                1.1.1.1         YES NVRAM  up                    up
Ethernet0/2                unassigned      YES NVRAM  administratively down down
Ethernet0/3                unassigned      YES NVRAM  administratively down down
Loopback0                  10.0.1.2        YES NVRAM  up                    up
```

Real device (non-vendor) [NTC `cisco_ios/show_interfaces_status`, two captures merged by NTC into one
file; one description replaced with `Office 1`]:
```
Port      Name               Status       Vlan       Duplex  Speed Type
Gi1/0/1                      notconnect   1            auto   auto 10/100/1000BaseTX
Gi1/0/2   AccessPoint        connected    8          a-full a-1000 10/100/1000BaseTX
Gi1/0/3   Office 1           notconnect   1            auto   auto 10/100/1000BaseTX
Gi1/0/7   Trunk Example      suspended    trunk      a-full a-1000 1000BaseSX SFP
Gi1/0/8   SFP Not Present    notconnect   1            auto   auto Not Present
Gi1/0/10  Management         notconnect   routed       auto   auto 10/100BaseTX
Gi1/0/15  Multi Space to the notconnect   16         a-half   auto 10/100BaseTX
Gi1/0/16  Multi Space to the err-disabled 16         a-half   auto 10/100BaseTX
Gi1/0/17  Multi Space        disabled     16         a-half   auto 10/100BaseTX
Gi1/0/18  Monitoring Port    monitoring   routed     a-full   auto 10/100/1000BaseT
Gi1/0/19  No Description     notconnect:    1         a-full  a-100 10/100/1000BaseTX
Gi1/0/22  addl status text   notconnect: TD 1         a-full  a-100 10/100/1000BaseTX
Gi1/0/23  addl status text   connected: TDR 1         a-full  a-100 10/100/1000BaseTX
Fa1/6   test               notconnect   1            auto    auto 10/100BaseTX
Po1       ethchl             connected    trunk      a-full a-1000
```
Second real capture [NTC `cisco_ios/show_interfaces_status_pvlan`] (IOS-XE 25G ports, Vlan list):
```
Port         Name               Status       Vlan       Duplex  Speed Type
Twe1/0/1     Device 1           connected    123        a-full a-1000 10/100/1000BaseTX SFP
Twe1/0/2     Device 2           disabled     456          auto   auto 10/100/1000BaseTX SFP
Twe1/0/4     * Device 3 *       connected    123,1234   a-full a-1000 10/100/1000BaseTX SFP
Twe1/0/17    Device 5           connected    pvlan prom   full    10G SFP-10GBase-SR
Twe2/0/32                       monitoring   1            full    10G SFP-10GBase-SR
```
`show interfaces status err-disabled` has its own layout (`Port Name Status Reason`), with reasons
such as `psecure-violation`, `bpduguard`, `loopback` [NTC]; vendor docs name `gbic-invalid`,
`dtp-flap`, `link-flap` [C-IF].

Values:
- `show interfaces description` Status: `up`, `down`, `admin down`, `deleted`, `reset` [NTC];
  Protocol: `up`, `down` [NTC]; `down (monitoring)`, `up (connected)` style suffixes UNVERIFIED (low).
- `show ip interface brief` Status: `up`, `down`, `administratively down`, `deleted` [NTC]; `reset`
  UNVERIFIED (medium). Protocol: `up`, `down`; `up (looped)`, `down (disabled)` UNVERIFIED (medium).
- `show interfaces status` Status: `connected`, `notconnect`, `disabled` (shutdown), `err-disabled`,
  `suspended` (LACP member), `monitoring` (SPAN destination) [NTC]; also `notconnect:`/`connected:`
  with trailing TDR text [NTC]. `inactive` (access VLAN missing) is a documented filter keyword [C-IF];
  as a status value UNVERIFIED (high). `sfpAbsent`, `xcvrAbsent`, `faulty`, `noOperMem`: UNVERIFIED
  (low; seen on other platforms). Vlan: number, `trunk`, `routed`, `unassigned`, `pvlan prom`,
  comma list [NTC].
- Empty table: not applicable (always prints interfaces). On a router, `show interfaces status`
  probably gives `% Invalid input detected at '^' marker.` UNVERIFIED (medium).

Hazards:
- `Name` is truncated to 18 characters and can contain spaces and words that look like status values
  (`*** connected to U`, `suspended customer` [NTC]). Parse `show interfaces status` by the header's
  column offsets, not by splitting on spaces.
- Status can carry a suffix (`notconnect: TD`) that shifts the following columns right [NTC].
- Duplex/Speed are right-aligned (`  auto`, `a-1000`); Type can be empty (Po rows) or contain spaces.
- Description column in `show interfaces description` is free text to end of line (may be empty).
- Status `admin down` is two words in `description` but `administratively down` in `ip int brief`.

### 1.2 aos-cx

Command: `show interface brief` (also `show interfaces brief`). Syntax
`show interface [<IFNAME>|<IFRANGE>] [brief | physical]`, "Shows brief info in tabular format";
`show interface [lag|loopback|tunnel|vlan] [<id>] [brief]` [K-CX-IF]. Exists on all CX models.
Columns (from the VSF guide, via search excerpt): `Port Native Mode Type Enabled Status Reason Speed`
with a second header line `VLAN (Mb/s)`. [K-CX-IF]

UNVERIFIED (medium) sample, written from background knowledge, header tokens confirmed above; not a
copy of any document:
```
--------------------------------------------------------------------------------------------------------
Port       Native  Mode     Type           Enabled Status  Reason                  Speed   Description
           VLAN                                                                    (Mb/s)
--------------------------------------------------------------------------------------------------------
1/1/1      1       access   1GbT           yes     up                              1000    --
1/1/2      1       access   1GbT           yes     down    Waiting for link        --      --
1/1/3      --      routed   --             no      down    Administratively down   --      --
```
Values: Enabled `yes`/`no`; Status `up`/`down`; Reason free text such as `Waiting for link`,
`Administratively down`, `No XCVR installed` (the last seen as `State information: No XCVR installed`
in full `show interface` output [NTC aruba_aoscx/show_interface]); UNVERIFIED (medium) for the brief
table. The IPsec tunnel brief example shows `--` placeholders for empty cells [K-CX-IF search excerpt].
Hazards: two-line header; Reason has spaces; `--` for empty values; on VSF stacks ports are
`member/slot/port`; split ports appear as `1/1/52:1` (Transceiver Guide excerpt). Full
`show interface` (not brief) prints `Interface 1/1/1 is up` / ` Admin state is up` blocks [NTC].

### 1.3 aos-s

Command: `show interfaces brief` (exists on all models). Real device (non-vendor)
[NTC `hp_procurve/show_interfaces_brief*`], four layout variants:
```
 Status and Counters - Port Status

                   | Intrusion                           MDI  Flow
  Port  Type       | Alert     Enabled Status Mode       Mode Ctrl
  ----- ---------- + --------- ------- ------ ---------- ---- ----
  1     10/100TX   | No        Yes     Up     100FDx     MDIX off
  2     10/100TX   | No        Yes     Down   10FDx      MDI  off
  5     10/100TX   | No        No      Down   10FDx      MDI  off
  49    100/1000T  | No        Yes     Down   1000FDx    MDIX off
  51               | No        Yes     Down   .               off
```
```
 Status and Counters - Port Status

                          | Intrusion                           MDI  Flow Bcast
  Port         Type       | Alert     Enabled Status Mode       Mode Ctrl Limit
  ------------ ---------- + --------- ------- ------ ---------- ---- ---- -----
  1/1          100/1000T  | No        Yes     Up     1000FDx    MDI  off  0
  1/24         100/1000T  | No        Yes     Down   1000FDx    Auto off  0
  1/25         1000SX     | No        Yes     Up     1000FDx    NA   off  0
  1/27                    | No        Yes     Down   .               off  0
  1/28-Trk1    SFP+SR     | No        Yes     Up     10GigFD    NA   off  0
  2/1          100/1000T  | No        Yes     Up     1000FDx    MDIX off  0
```
```

 Status and Counters - Port Status

 Note: * indicates values dynamically overridden by RADIUS.

                          | Intrusion                           MDI  Flow Bcast
  Port         Type       | Alert     Enabled Status Mode       Mode Ctrl Limit
  ------------ ---------- + --------- ------- ------ ---------- ---- ---- -----
  A1           1000SX     | No        Yes     Up     1000FDx    NA   off  0
  A8*          SFP+SR     | No        Yes     Up     10GigFD    NA   off  0
  D1-Trk1      100/1000T  | No        Yes     Up     1000FDx    MDIX off  0
  D5           100/1000T  | No        No      Down   1000FDx    NA   off  0
  E3                      | No        Yes     Down   .               off  0
  F1           10GbE-T    | No        Yes     Up     2500FDx*   MDIX off  0
 * third-party transceiver
```
Values: Enabled `Yes`/`No` (admin); Status `Up`/`Down` (link). No separate line protocol.
Alternative `show interfaces status` (adds Name, Tagged/Untagged) [NTC `hp_procurve/show_interfaces_status`]:
```
  Port     Name       Status  Config-mode   Speed    Type       Tagged Untagged
  -------- ---------- ------- ------------- -------- ---------- ------ --------
  A1                  Down    Auto          1000FDx  100/1000T  No     140
  A8       SW-01-0... Up      Auto          1000FDx  100/1000T  No     2
  A21                 Down                                      multi  No
  A24-Trk1 Srv-003... Up      Auto          10GigFD  10GbE-GEN  multi  No
```
```
  1/28-... Srv-001... Up      Auto          10GigFD  10GbE-GEN  multi  No
```
Hazards: Port carries trunk membership as a suffix (`1/28-Trk1`) and can be truncated (`1/28-...`)
in `status`; `*` suffixes on port (RADIUS override) and on Mode (`2500FDx*`); empty Type/Mode cells
and a lone `.` in Mode; optional `Bcast Limit` column and optional `Note:` line; a trailing
`* third-party transceiver` footnote line; port naming `1`, `A1`, `1/1` (stack/VSF). Some captures have
`\r\n` line ends [NTC hp_procurve/show_vlans4].

---

## 2. port_channels

### 2.1 ios: `show etherchannel summary`

Exists on all Catalyst switches and on ISR routers with port-channel support (UNVERIFIED for ISR,
medium). Flag legend per the Catalyst 9300 17.13 guide: D down, P bundled in port-channel,
I stand-alone, s suspended, H hot-standby (LACP only), R Layer3, S Layer2, U in use,
f failed to allocate aggregator, M not in use (minimum links not met), u unsuitable for bundling,
w waiting to be aggregated, d default port, A formed by Auto LAG [C-EC].

Real device (non-vendor) [NTC `cisco_ios/show_etherchannel_summary`] (older legend with `N`, `m`):
```
Flags:  D - down        P - bundled in port-channel
        I - stand-alone s - suspended
        H - Hot-standby (LACP only)
        R - Layer3      S - Layer2
        U - in use      N - not in use, no aggregation
        f - failed to allocate aggregator

        M - not in use, no aggregation due to minimum links not met
        m - not in use, port not aggregated due to minimum links not met
        u - unsuitable for bundling
        d - default port

        w - waiting to be aggregated
Number of channel-groups in use: 6
Number of aggregators:           6

Group  Port-channel  Protocol    Ports
------+-------------+-----------+-----------------------------------------------
1      Po1(SU)         LACP      Te6/4(P)       Te3/5(P)       Te9/4(P)
                                 Te5/5(P)
3      Po3(SU)         LACP      Te4/2(P)       Te2/2(P)
13     Po13(SU)        LACP      Te2/8(P)
```
Real device (non-vendor) [GENIE `iosxe/ShowEtherchannelSummary/golden_output_3`] (IOS-XE, Auto LAG
legend, down members, no-protocol groups, empty count):
```
Flags:  D - down        P - bundled in port-channel
        I - stand-alone s - suspended
        H - Hot-standby (LACP only)
        R - Layer3      S - Layer2
        U - in use      f - failed to allocate aggregator

        M - not in use, minimum links not met
        u - unsuitable for bundling
        w - waiting to be aggregated
        d - default port

        A - formed by Auto LAG


Number of channel-groups in use:
Number of aggregators:           5

Group  Port-channel  Protocol    Ports
------+-------------+-----------+-----------------------------------------------
1      Po1(SD)          -
2      Po2(SD)          -
3      Po3(SU)        LACP        Te2/0/1(P)      Te2/0/2(D)
                                  Te2/0/3(s)      Te3/0/1(s)
                                  Te3/0/2(D)      Te3/0/3(P)
4      Po4(RD)        LACP        Fi1/0/5(D)      Gi4/0/5(D)
```
Router variant (ISR/ASR style, tabs, `bndl` words) [NTC `show_etherchannel_summary2`]:
```
Flags:  D - down        P/bndl - bundled in port-channel
        I - stand-alone s/susp - suspended
        H - Hot-standby (LACP only)
        R - Layer3      S - Layer2
        U - in use      f - failed to allocate aggregator

        M - not in use, minimum links not met
        u - unsuitable for bundling
        w - waiting to be aggregated
        d - default port


Number of channel-groups in use: 1
Number of aggregators:           1

Group  Port-channel  Protocol    Ports
------+-------------+-----------+-----------------------------------------------
10	Po10(RU)		LACP	 Te0/0/2(bndl) Te0/0/3(bndl)

RU - L3 port-channel UP State
SU - L2 port-channel UP state
P/bndl -  Bundled
S/susp  - Suspended
```
State columns: Po flags are a pair `<layer><use>`: layer `S`/`R`, use `U` (in use), `D` (down),
`N`/`M` (not in use). Member flags: `P`, `D`, `I`, `s`, `H`, `w`, `u`, `f`, `d`, and router words
`bndl`, `susp`, `hot-sby`/`stand-alone` (the last two UNVERIFIED, low). Protocol: `LACP`, `PAgP`,
`-` (static `mode on`). Empty: legend + `Number of channel-groups in use: 0` + header only,
UNVERIFIED (high).
Hazards: member list wraps onto continuation lines with no group number; separators may be tabs;
`Number of channel-groups in use:` can have no number [GENIE]; Group number and Po number can
differ; legend text differs by release (do not anchor on legend lines); router footer lines after
the table (`RU - L3 port-channel UP State`) look like rows.

### 2.2 aos-cx

`show lag brief`: not found in any HPE document; UNVERIFIED (low) that it exists. Documented:
`show lag [<LAG-ID>]` (detail blocks), `show lacp aggregates [<LAG-NAME>]`, `show lacp interfaces
[<IFNAME>]` (all platforms, operator or manager context) [K-CX-LAG][K-CX-LACPAGG][K-CX-LACPINT].
Recommended: `show lacp interfaces` (one row per member with state flags and forwarding state) plus
`show lag` for the LAG's own up/down. Static LAG members appear in `show lacp interfaces` with empty
LACP columns [K-CX-LACPINT].

Vendor (layout reconstructed) [K-CX-LACPINT] (10.13; one example with `lacp-block`):
```
switch# show lacp interfaces
State abbreviations :
A - Active        P - Passive      F - Aggregable I - Individual
S - Short-timeout L - Long-timeout N - InSync     O - OutofSync
C - Collecting    D - Distributing
X - State m/c expired              E - Default neighbor state

Actor details of all interfaces:
------------------------------------------------------------------------------------
Intf    Aggr     Port  Port  State   System-id         System Aggr Forwarding
        name     id    Pri                             Pri    Key  State
------------------------------------------------------------------------------------
1/1/1   lag10    17    1     ALFOE   70:72:cf:37:a3:5c 20     10   lacp-block
1/1/2   lag128   69    1     ALFNCD  70:72:cf:37:a3:5c 20     128  up
1/1/3   lag128   14    1     ALFNCD  70:72:cf:37:a3:5c 20     128  up
1/1/4   lag128                                                     down
1/1/5   lag20                                                      up

Partner details of all interfaces:
------------------------------------------------------------------------------
Intf    Aggr     Partner Port  State   System-id         System   Aggr
        name     Port-id Pri                             Priority Key
------------------------------------------------------------------------------
1/1/1   lag10    0       65534 PLFOEX  00:00:00:00:00:00 65534    0
1/1/2   lag128   69      1     PLFNCD  70:72:cf:8c:60:a7 65534    128
1/1/4   lag128
1/1/5   lag20
```
VSX multi-chassis rows show the LAG as `lag1(mc)` [K-CX-LACPINT]:
```
1/4/14  lag1(mc) 206   1     ALFNCD  f8:60:f0:06:49:00 65534  1    up
1/5/15  lag2(mc)                                                   down
```
LACP fallback example (10.11+) uses state `IE` and forwarding `lacp-block` [K-CX-LACPINT].
`show lacp aggregates` (key/value blocks) [K-CX-LACPAGG]:
```
switch# show lacp aggregates
Aggregate-name        : lag1
Aggregated-interfaces : 1/1/27 1/1/28 1/1/29
Heartbeat rate        : slow
Hash                  : l3-src-dst
Aggregate mode        : active

Aggregate-name        : lag2
Aggregated-interfaces : 1/1/48
Heartbeat rate        : slow
Hash                  : l2-src-dst
Aggregate mode        : passive
```
`show lag` (key/value blocks, paged in the sample) [K-CX-LAG]:
```
switch# show lag
System-ID       : 90:20:c2:24:60:00
System-priority : 65534

Aggregate lag1 is up
 Admin state is up
 Description :
 Type                  : normal
 Lacp Fallback         : Enabled
 MAC Address           : 90:20:c2:24:60:00
 Aggregated-interfaces : 1/1/1 1/1/2 1/1/3 1/1/46 1/1/47 1/1/48
 Aggregation-key       : 1
 Aggregate mode        : active
 Hash                  : l3-src-dst
 LACP rate             : slow
 Speed                 : 1000 Mb/s
 Mode                  : trunk
```
Values: State flag string = concatenation of the abbreviations above (`ALFNCD` = active, long
timeout, aggregable, in sync, collecting, distributing = healthy). Forwarding state `up`, `down`,
`lacp-block` [K-CX-LACPINT]. LAG line `Aggregate lagN is up|down`; aggregate mode `active`,
`passive`, `static` [K-CX-LAG][K-CX-LACPAGG]. Empty: UNVERIFIED.
Hazards: rows with empty middle columns (static members, down members) leave only Intf, Aggr and
Forwarding; must parse by header offsets or by "first two tokens + last token". Two-line headers.
`(mc)` suffix. `show lag` output shows `-- MORE --` if paging is not disabled [K-CX-LAG].

### 2.3 aos-s

`show trunks` lists **static** trunk membership (static LACP and non-protocol `Trunk`), not link
state; dynamic LACP trunks appear only in `show lacp` [K-S-TRK][K-S-LACPOP]. `show lacp` gives per-port
LACP state. Both exist on 2530 and 2930F (UNVERIFIED for 2530, high). For link state of a member, use
the `Status` column of `show interfaces brief` row `D1-Trk1` (section 1.3).

Vendor (layout reconstructed) [K-S-TRK]:
```
switch# show trunks

 Load Balancing

  Port | Name                    Type      | Group Type
  ---- + ----------------------- --------- + ----- -----
  4    | Print-Server-Trunk      10/100TX  | Trk1  Trunk
  5    | Print-Server-Trunk      10/100TX  | Trk1  Trunk
  7    |                         10/100TX  | Trk2  Trunk
  8    |                         10/100TX  | Trk2  Trunk
```
Real device (non-vendor) [NTC `hp_procurve/show_trunks`] (name replaced with `Uplink acme-sw001`):
```

 Load Balancing Method:  L3-based (default)

  Port   | Name                             Type       | Group Type
  ------ + -------------------------------- ---------- + ----- --------
  49     | Uplink acme-sw001                1000LX     | Trk2  LACP
  50     | Uplink acme-sw001                1000LX     | Trk2  LACP

```
`show lacp`: vendor field meanings [K-S-LACP]: Port Numb; LACP Enabled `Active`/`Passive`; Trunk
Group `TrkX` (static LACP), `DynX` (dynamic, [K-S-LACPOP]) or the port number itself (LACP on but not
trunked); Port Status `Up`, `Down`, `Disabled`, `Blocked`, `Standby`; LACP Partner `Yes`/`No`;
LACP Status `Success`/`Failure`. The vendor sample is an image; layout below is UNVERIFIED (medium),
from background knowledge:
```
                           LACP

           LACP      Trunk     Port                LACP      Admin   Oper
   Port    Enabled   Group     Status    Partner   Status    Key     Key
   ----    -------   -------   -------   -------   -------   ------  ------
   A1      Active    Trk1      Up        Yes       Success   0       485
   A2      Active    Trk1      Up        Yes       Success   0       485
```
Type column of `show trunks`: `Trunk` (static non-protocol), `LACP` [K-S-TRK][NTC]. Empty: header
only, UNVERIFIED (medium). Hazards: Name column free text with spaces, may be empty; `|` and `+`
separators; blank line containing a single space at the end [NTC].

---

## 3. ospf_neighbors

### 3.1 ios: `show ip ospf neighbor`

Exists on all L3-capable IOS/IOS-XE; on L2-only images (2960-X LAN Base) the command is absent,
UNVERIFIED (medium). Covers all OSPF processes and VRFs in one table (no per-process header),
UNVERIFIED (medium).

Vendor (verbatim) [C-OSPF] (note the doc's own tabs and a malformed last line):
```
Device# show ip ospf neighbor
Neighbor ID 		Pri   State        Dead Time     Address         Interface
10.199.199.137  1    FULL/DR       0:00:31    192.168.80.37      Ethernet0
172.16.48.1     1    FULL/DROTHER  0:00:33    172.16.48.1        Fddi0
172.16.48.200   1    FULL/DROTHER  0:00:33    172.16.48.200      Fddi0
10.199.199.137  5    FULL/DR       0:00:33    172.16.48.189      Fddi0
172.16.1.201		1	INIT/DROTHER			00.00.35		10.1.1.201			Ethernet0/0
```
Real device (non-vendor) [NTC `cisco_ios/show_ip_ospf_neighbor`, `four_neighbors`]:
```
Neighbor ID     Pri   State           Dead Time   Address         Interface
10.0.0.1          0   FULL/  -        00:00:32    10.0.1.1        Port-channel1
10.0.0.2          0   FULL/  -        00:00:33    10.0.2.1        Port-channel2
10.0.0.3          0   FULL/  -        00:00:39    10.0.3.1        Vlan1000
```
```
Neighbor ID     Pri   State           Dead Time   Address         Interface
10.190.30.2        1   FULL/BDR        00:00:04    10.190.16.11     GigabitEthernet0/2.101
10.190.30.3        1   FULL/DR         00:00:04    10.190.16.12     GigabitEthernet0/2.101
```
State column = `<adjacency>/<role>`. Adjacency (vendor list) [C-OSPF]: `DOWN`, `ATTEMPT` (NBMA only),
`INIT`, `2WAY`, `EXSTART`, `EXCHANGE`, `LOADING`, `FULL`. Role: `DR`, `BDR`, `DROTHER`, or `  -`
(point-to-point / no DR, printed as `FULL/  -` with spaces) [NTC]. Normal steady states: `FULL/*`
and, on broadcast segments between two DROTHERs, `2WAY/DROTHER` [C-OSPF]. INIT also appears when the
peer enables TTL security [C-OSPF]. Dead Time `hh:mm:ss` (old docs `0:00:31`).
Empty (OSPF configured, no neighbors, or OSPF not configured): prints nothing, UNVERIFIED (medium).
Hazards: `FULL/  -` contains spaces, so splitting on whitespace yields an extra `-` token; Neighbor ID
and Pri can run together when the ID is 15 characters (`10.190.30.2        1` shows variable gap);
interface names are full (`GigabitEthernet0/2.101`) and may be long; the same Neighbor ID can appear
on several interfaces (key on ID+interface); `show ip ospf neighbor summary` and `detail` have other
layouts [C-OSPF].

### 3.2 aos-cx: `show ip ospf neighbors [all-vrfs | vrf <name>]`

Syntax `show ip ospf [<PROCESS-ID>] neighbors [<NEIGHBOR-ID>] [interface <IF>] [detail | summary]
[all-vrfs | vrf <VRF-NAME>]`, default VRF `default`; 10.14 widened process ID to 1-65535 [K-CX-OSPF].
Prints a header per process and VRF. Vendor (layout reconstructed) [K-CX-OSPF]:
```
switch# show ip ospf neighbors
OSPF Process ID 1 VRF default
==============================

Total Number of Neighbors: 1

Neighbor ID      Priority  State             Nbr Address       Interface
-------------------------------------------------------------------------
2.2.2.2          1         FULL/DR           10.1.1.2          1/1/1
```
`summary` form (counts per state, useful as a cross-check) [K-CX-OSPF]:
```
switch# show ip ospf neighbors summary
OSPF Process ID 1 VRF default, Neighbor Summary
================================================

Interface  Down  Attempt  Init  TwoWay  ExStart  Exchange  Loading  Full  Total
-------------------------------------------------------------------------
1/1/1      0     0        0     0       0        0         0        1     1
1/1/2      0     0        0     0       0        0         0        0     0
Total      0     0        0     0       0        0         0        1     1
```
State: `FULL/DR` shown; full value set UNVERIFIED (medium): likely same `<STATE>/<role>` scheme with
`DR`, `BDR`, `DROTHER`; states named in the summary header: Down, Attempt, Init, TwoWay, ExStart,
Exchange, Loading, Full [K-CX-OSPF]. Nbr Address shows the borrowed address on IP-unnumbered links
(10.14.1000+) [K-CX-OSPF]. Empty/not configured: UNVERIFIED. Hazards: repeated
`OSPF Process ID n VRF x` headers with `=` underline (record process/VRF as row context); `Total Number
of Neighbors` line; with `all-vrfs` one block per VRF (UNVERIFIED, high).

### 3.3 aos-s: `show ip ospf neighbor`

Exists on routing-capable models (2930F: yes, documented in the 2930M/F guide [K-S-OSPF]); 2530 has
no OSPF, UNVERIFIED (high) that the command is rejected (`Invalid input: ospf`, medium).
Vendor (layout reconstructed) [K-S-OSPF]:
```
switch# show ip ospf neighbor

 OSPF Neighbor Information

                                                         Rxmt          Helper
  Router ID       Pri IP Address     NbIfState State     QLen  Events  Status
  --------------- --- -------------- --------- --------- ----- ------  ------
  10.0.8.34       1   10.3.18.34     DR        FULL      0     6       none
  10.3.53.38      1   10.3.53.38     DR        FULL      0     6       none
```
NbIfState: `DR`, `BDR`, or **blank** (neither). State: `INIT`, `2WAY`, `EXSTART`, `EXCHANGE`,
`LOADING`, `FULL` [K-S-OSPF] (no DOWN/ATTEMPT listed). Helper Status `Helper`/`None` per field
table, `none` in sample [K-S-OSPF]. 16.11 KB page has a changed layout (adds Helper Age) per search
excerpt; not read. Empty: UNVERIFIED. Hazards: blank NbIfState shifts token positions; two-line header.

---

## 4. bgp_neighbors

### 4.1 ios

`show ip bgp summary` covers IPv4 unicast of the global table only. On IOS-XE `show bgp all summary`
prints one block per address family (`For address family: ...`) including VPNv4/IPv6 [GENIE][NTC];
prefer it when VRFs or IPv6 are used, with the AF as row context. Per-VRF neighbors appear under
`For address family: VPNv4 Unicast` only as VPNv4 peers; CE-side VRF peers need
`show bgp vpnv4 unicast all summary` or `show ip bgp vpnv4 all summary` (UNVERIFIED, medium).
Not on L2-only images.

Vendor (verbatim, short form via fetcher) [C-BGP]:
```
Neighbor        V    AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down State/PfxRcd
10.100.1.1      4   200      26      22      199    0    0 00:14:23 23
10.200.1.1      4   300      21      51      199    0    0 00:13:40 0
```
Real device (non-vendor) [NTC `cisco_ios/show_ip_bgp_summary`] (header trimmed to 3 lines):
```
BGP router identifier 10.0.0.0, local AS number 65000
BGP table version is 512185206, main routing table version 512185206
566019 network entries using 140372712 bytes of memory

Neighbor        V           AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down  State/PfxRcd
10.0.0.1        4        65000 2746767 2396274 512185206    0    0 3w0d       558720
10.0.0.3        4        65002  695143  689871 512185203    0    0 1y10w           0
10.0.0.5        4        65004 26552304 14931352 512185206    0    0 19w5d      558720
10.0.0.10       4        65009 26930508  942614 512185203    0    0 1y10w     Idle (Admin)
```
Real device (non-vendor) [GENIE `iosxe/ShowBgpAllSummary/golden_output1`] (test file indented every
line by 8 spaces; de-indented here, memory lines trimmed):
```
For address family: IPv4 Unicast
BGP router identifier 192.168.111.1, local AS number 100
BGP table version is 28, main routing table version 28

Neighbor        V           AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down  State/PfxRcd
192.168.111.1       4          100       0       0        1    0    0 01:07:38 Idle
192.168.4.1       4          100       0       0        1    0    0 never    Idle

For address family: IPv6 Unicast
BGP router identifier 192.168.111.1, local AS number 100
BGP table version is 1, main routing table version 1

Neighbor        V           AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down  State/PfxRcd
2001:db8:400::1:1       4          100       0       0        1    0    0 01:07:38 Idle
2001::14:4      4          200       0       0        1    0    0 never    Idle
```
asdot AS numbers appear as `65000.1000` [NTC `..._with_dot_peer_as`].

State/PfxRcd semantics [C-BGP]: "Current state of the BGP session, and the number of prefixes that
have been received". **A number means Established**; any word means not established. Words:
`Idle`, `Connect`, `Active`, `OpenSent`, `OpenConfirm` (BGP FSM; UNVERIFIED that all print, high),
`Idle (Admin)` = `neighbor shutdown` [C-BGP][NTC], `Idle (PfxCt)` after maximum-prefix shutdown
(doc text says the string `PfxRcd` appears; device form UNVERIFIED, medium). Up/Down: time in
Established, or since the session left it ("current status if not in the Established state")
[C-BGP]; forms `00:14:23`, `3w0d`, `1y10w`, `19w5d`, `never` [NTC][GENIE].
Empty / not configured: `% BGP not active` UNVERIFIED (high). Hazards: long counters and IPv6
neighbors push columns right or wrap the neighbor onto its own line (IPv6 > 15 chars wraps on real
devices, UNVERIFIED high; GENIE shows them unwrapped); `Idle (Admin)` has a space; header block length
varies; dynamic neighbors prefixed `*` (UNVERIFIED, medium); parse rows by "address + 8 numeric
fields + rest".

### 4.2 aos-cx: `show bgp all summary`

Syntax `show bgp [vrf <VRF-NAME>] {ipv4 unicast | ipv6 unicast | all} summary [vsx-peer]`; `all` =
"all VRFs and address-families" [K-CX-BGP]. **No prefix-count column**: State is a word and AdminStatus
is separate. Vendor (layout reconstructed) [K-CX-BGP]:
```
switch# show bgp all summary
Codes: * Dynamic Neighbor
VRF : default
BGP Summary
-----------
 Local AS               : 100          BGP Router Identifier  : 9.0.0.1
 Peers                  : 1            Log Neighbor Changes   : No
 Cfg. Hold Time         : 180          Cfg. Keep Alive        : 60
 Confederation Id       : 0

Address-family : IPv4 Unicast
-----------------------------
 Neighbor        Remote-AS MsgRcvd MsgSent   Up/Down Time State        AdminStatus
 9.0.0.2         200       25      23        00h:17m:50s  Established  Up
 *10.1.1.5       11        26      24        00h:20m:26s  Established  Up

Address-family : IPv6 Unicast
-----------------------------
 Neighbor        Remote-AS MsgRcvd MsgSent   Up/Down Time State        AdminStatus
 *2001::2002     11        3       3         00h:00m:14s  Established  Up

VRF : v1
BGP Summary
...
```
Real device (non-vendor) [NTC `aruba_aoscx/show_bgp_all-vrfs_all_summary`] (command
`show bgp all-vrfs all summary`):
```
VRF : default
BGP Summary
-----------
 Local AS               : 4290020685   BGP Router Identifier  : 192.168.100.1
 Peers                  : 4            Log Neighbor Changes   : No
 Cfg. Hold Time         : 180          Cfg. Keep Alive        : 60

Address-family : IPv4 Unicast
-----------------------------
 Neighbor        Remote-AS MsgRcvd MsgSent   Up/Down Time State        AdminStatus
 10.252.13.2     4290020685  0       0       00h:00m:00s  Idle          Up
 172.25.0.1      4290000000  116314  133774  02m:02w:06d  Established   Up

Address-family : IPv6 Unicast
-----------------------------

Address-family : L2VPN EVPN
-----------------------------
 Neighbor        Remote-AS MsgRcvd MsgSent   Up/Down Time State        AdminStatus
 172.18.174.17   4290000000  116310  133770  02m:02w:06d  Established   Up
```
Values: State BGP FSM words (`Idle`, `Established` seen; `Active`, `Connect`, `OpenSent`,
`OpenConfirm` UNVERIFIED high); AdminStatus `Up` (`Down` when shut, UNVERIFIED high).
Hazards: per-VRF and per-AF repeated headers (row context = VRF + AF); empty AF sections (header,
no rows); `*` prefix glued to the address; Up/Down Time format changes for long uptimes
(`02m:02w:06d` = months:weeks:days, [NTC]); whether `show bgp all summary` without `all-vrfs` lists
non-default VRFs: the vendor sample does list VRF v1 [K-CX-BGP], but the parameter name suggests
otherwise, so UNVERIFIED (medium). Not configured: UNVERIFIED.

### 4.3 aos-s: `show ip bgp summary`

BGP is documented in the 3810M/5400R (KB) routing guide [K-S-BGP]; on 2930F (WC) and 2530 BGP is
absent, UNVERIFIED (medium for 2930F, high for 2530). Vendor (layout reconstructed) [K-S-BGP]:
```
switch(config)# show ip bgp summary

 Peer Information

  Remote Address  Remote-AS Local-AS State        Admin Status
  --------------  --------- -------- ------------ ------------
  2.2.2.2         65009     65009    Established  Start
  3.1.1.2         65008     65009    Established  Start
```
No prefix count; Admin Status `Start` shown for running peers (`Stop` for shut peers, UNVERIFIED
medium). A global header (AS, router ID) may precede it on other releases, UNVERIFIED (medium).

---

## 5. eigrp_neighbors (ios only): `show ip eigrp neighbors`

L3 images only. Real device (non-vendor) [NTC `cisco_ios/show_ip_eigrp_neighbors*`], three header forms:
```
IP-EIGRP neighbors for process 1
H   Address                 Interface       Hold Uptime   SRTT   RTO  Q  Seq
                                            (sec)         (ms)       Cnt Num
0   192.168.1.2            Gi0/0             10 00:12:45   19   100  0  11
1   192.168.2.2            Gi0/1             12 00:11:24   14   100  0  10
```
```
EIGRP-IPv4 Neighbors for AS(10)
H   Address                 Interface              Hold Uptime   SRTT   RTO  Q  Seq
                                                   (sec)         (ms)       Cnt Num
11  10.1.1.122              Tu15                     44 1d01h     114   684  0  21930
9   10.1.1.34               Tu117                    38 1w6d      157   942  0  7449
5   192.168.1.239           Gi0/0/2                  11 1y17w       1   100  0  1915033
```
```
EIGRP-IPv4:(535) neighbors for process 535
H   Address                 Interface       Hold Uptime   SRTT   RTO  Q  Seq
                                            (sec)         (ms)       Cnt Num
0   10.120.250.1            Vl250             10 1w0d        3   450  0  17091
```
(one NTC capture also starts with `Load for five secs:` / `Time source is NTP` lines from
`terminal exec prompt timestamp`). No state column: presence of the row is the state; Q Cnt > 0
persistently means a stuck neighbor. Uptime forms `00:12:45`, `1d01h`, `1w6d`, `1y17w`. Named-mode
and VRF output print one `EIGRP-IPv4 VR(name) Address-Family Neighbors for AS(n)` / `VRF(x)` header
per AS/VRF, UNVERIFIED (medium). Empty: header only when EIGRP runs with no neighbors, nothing when
EIGRP is absent, UNVERIFIED (medium). Hazards: two-line header; process/AS headers repeat; H numbers
are not stable keys (key on address+interface); interface names abbreviated.

---

## 6. stack members

### 6.1 ios: `show switch`

Exists on stackable Catalysts (2960-X FlexStack, 3850, 9200/9300); not on 3560-CX or ISR (expected
`% Invalid input detected at '^' marker.`, UNVERIFIED medium). On a standalone stackable switch it
prints one row. States from the 9300 command reference usage guidelines [C-STK]: `Initializing`,
`HA Sync in Progress`, `Syncing`, `Ready`, `V-Mismatch`, `Provisioned` (MAC and priority shown as 0),
`Unprovisioned`, `Removed`, `Sync not started`, `Lic-Mismatch`. Roles: IOS-XE `Active`, `Standby`,
`Member`; IOS 15 / 3750-era `Master`, `Member` [NTC]. `*` marks the switch you are logged into.

Real device (non-vendor), IOS-XE 4-member [NTC `cisco_ios/show_switch_detail3`]:
```
Switch/Stack Mac Address : cc98.9114.3580 - Local Mac Address
Mac persistency wait time: Indefinite
                                             H/W   Current
Switch#   Role    Mac Address     Priority Version  State
-------------------------------------------------------------------------------------
*1       Active   cc98.9114.3580     15     V01     Ready
 2       Member   706b.b9c8.2380     2      V01     Ready
 3       Member   a0f8.4911.c080     3      V01     Ready
 4       Standby  cc98.911a.0800     8      V01     Ready
```
Provisioned members, empty H/W column [GENIE `iosxe/ShowSwitch/golden_output_1`]:
```
Switch/Stack Mac Address : 0c75.bd6e.db00 - Local Mac Address
Mac persistency wait time: Indefinite
                                             H/W   Current
Switch#   Role    Mac Address     Priority Version  State
-------------------------------------------------------------------------------------
*1       Active   0c75.bd6e.db00     15     V02     Ready
 2       Member   0000.0000.0000     0              Provisioned
 3       Member   0000.0000.0000     0              Provisioned
```
IOS 15 layout with stack-port table (`show switch detail`) [NTC `show_switch_detail2`]:
```
Switch/Stack Mac Address : 4c4e.3573.ac00
                                           H/W   Current
Switch#  Role   Mac Address     Priority Version  State
----------------------------------------------------------
*1       Master 4c4e.3573.ac00     15     3       Ready
 2       Member 4c4e.3573.7b00     8      3       Ready



         Stack Port Status             Neighbors
Switch#  Port 1     Port 2           Port 1   Port 2
--------------------------------------------------------
  1        Ok         Ok                3        2
  2        Ok         Ok                1        3
```
Software version per member: the member table in `show version` (repo fixture
`show/ios/c9300_show_version.txt`):
```
Switch Ports Model              SW Version        SW Image              Mode
------ ----- -----              ----------        ----------            ----
*    1 56    C9300-48P          17.06.04          CAT9K_IOSXE           INSTALL
```
StackWise Virtual (9500): `show switch` lists the two chassis (layout UNVERIFIED, medium);
`show stackwise-virtual` gives domain and links, not states [GENIE `iosxe/ShowStackwiseVirtual`]:
```
Stackwise Virtual Configuration:
--------------------------------
Stackwise Virtual : Enabled
Domain Number : 100

Switch  Stackwise Virtual Link  Ports
------  ----------------------  ------
1       1                       HundredGigE1/0/3
                                HundredGigE1/0/4
2       1                       HundredGigE2/0/3
                                HundredGigE2/0/4
```
Hazards: `*` glued to the number; empty H/W Version for provisioned members; state can be two or
more words (`HA Sync in Progress`, `V-Mismatch`); trailing spaces on rows [NTC]; header line wording
differs between IOS 15 and IOS-XE; stack-port table follows after blank lines in `detail`.

### 6.2 aos-cx: `show vsf`

VSF exists on 6200/6300 (and 4100i/6100 from 10.16) [K-CX-VSF]; not on 8xxx/6400 (VSX instead), where
the command is invalid, UNVERIFIED (high). Standalone 6300 shows `Topology : Standalone` and one row.
Vendor (layout reconstructed) [K-CX-VSF] (10.15, 6300):
```
switch# show vsf
Force Autojoin              : Disabled
Autojoin Eligibility Status : Not Eligible
MAC Address                 : 38:21:c7:5c:62:40
Egress Shape                : Enabled
Egress Shape Rate           : None
Secondary                   : 2
Topology                    : Ring
Status                      : No Split
Split Detection Method      : None

Mbr  Mac Address         type            Status
ID
---  ------------------- --------------  ---------------
1    38:21:c7:5c:62:40   JL668A          Conductor
2    18:7a:3b:1b:68:c0   R8S90A          Standby
3    38:21:c7:5c:57:c0   JL668A          Member
4    18:7a:3b:1b:66:40   R8S89A          Member Booting
```
and a missing member: `4                        JL662A          Not Present` [K-CX-VSF].
Status values: `Conductor`, `Standby`, `Member`, `Member Booting`, `Not Present` [K-CX-VSF];
`In Other Fragment` after a split (search excerpt of the same guide); older releases say `Master`
instead of `Conductor` [NTC `aruba_aoscx/show_vsf_detail`, FL.10.05]. Stack Status: `No Split`
(others such as `Fragment Active` UNVERIFIED). Software version: one value for the stack in
`show vsf detail` (`Software Version : FL.10.10.1030`) [NTC]; per-member version is not listed.
Hazards: blank MAC cell for `Not Present`; two-word statuses; header column `type` lower-case in one
release, `Type` in another [K-CX-VSF]; 10.13+/10.15+ add header lines.

### 6.3 aos-s: `show vsf` (2930F) and `show stacking` (2930M, 3810M)

2930F stacks with VSF (front-port); 2930M and 3810M use backplane stacking and `show stacking`;
2530 has neither [K-S-VSF][K-S-STK]; model mapping for 2930M/3810M per search excerpt of the ATMG
pages; 2530 UNVERIFIED (high). `show vsf` "can be run only after VSF is enabled" [K-S-VSF]; the error
text otherwise is UNVERIFIED.
Vendor (layout reconstructed) [K-S-VSF]:
```
switch# show vsf

 VSF Domain ID    : 123
 MAC Address      : 941882-435589
 VSF Topology     : Ring
 VSF Status       : Active
 Uptime           : 26d 20h 36m
 VSF MAD          : None
 VSF Port Speed   : 10G
 Software Version : WC.16.06.0000x

 Mbr
 ID  MAC Address       Model                                 Pri Status
 --- ----------------- ------------------------------------- --- ---------------
 1   f40343-0796b0     Aruba JL258A 2930F-8G-PoE+-2SFP+ S... 128 Member
 *2  941882-435580     Aruba JL254A 2930F-48G-4SFP+ Switch   128 Member
 3   b05ada-9771c0     Aruba JL256A 2930F-48G-PoE+-4SFP+ ... 128 Standby
 7   b05ada-9631e0     Aruba JL255A 2930F-24G-PoE+-4SFP+ ... 128 Commander
```
Vendor (layout reconstructed) [K-S-STK] (3810, split stack):
```
Switch# show stacking

 Stack ID         : 00011cc1-de4d4740
 MAC Address      : 1cc1de-4d474b
 Stack Topology   : Unknown
 Stack Status     : Fragment Active
 Split Policy     : All-Fragments-Up
 Uptime           : 0d 8h 6m
 Software Version : KA.15.11.0000x

 Mbr
 ID  Mac Address   Model                                  Pri Status
 --- ------------- -------------------------------------- --- ---------------
 1   1cc1de-4d4740 HPE J9574A 3810-48G-PoE+-4SFP+ Switch  128 Missing
 2   1cc1de-4d7400 HPE J9576A 3810-48G-4SFP+ Switch       128 Commander
 3   1cc1de-4ddc00 HPE J9575A 3810-24G-2SFP+ Switch       128 Missing
```
Status values: `Commander`, `Standby`, `Member`, `Missing`, `Provisioned` [K-S-VSF][K-S-STK];
`Not Joined` per search excerpt of [K-S-STK]. VSF Status `Active` (others UNVERIFIED). Software
version: a single stack-wide `Software Version`; `show vsf detail` / `show stacking detail` give
per-member ROM Version only [NTC `hp_procurve/show_vsf_detail`][K-S-STK].
Hazards: `*` marks the local member; Model has spaces and is truncated with `...`; `Mbr ID` header on
two lines (UNVERIFIED layout); MAC format `xxxxxx-xxxxxx`.

---

## 7. vlans

### 7.1 ios: `show vlan brief`

Switches only (absent on routers without switch modules, UNVERIFIED high). Real device (non-vendor)
[NTC `cisco_ios/show_vlan`] (first section of `show vlan`, same as `brief`):
```
VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Gi0/1
10   Management                       active
50   VLan50                           active    Fa0/1, Fa0/2, Fa0/3, Fa0/4, Fa0/5, Fa0/6, Fa0/7, Fa0/8, Fa0/9
                                                Fa0/10, Fa0/11, Fa0/12
1002 fddi-default                     act/unsup
1003 token-ring-default               act/unsup
1004 fddinet-default                  act/unsup
1005 trnet-default                    act/unsup
```
Status values: `active`, `act/unsup` (1002-1005); `suspended`, `act/lshut`, `sus/lshut` UNVERIFIED
(medium). Hazards: Ports wraps onto continuation lines; trunk ports are not listed; names truncated
to 32 characters; empty Ports for VLANs with no access ports; one GENIE capture shows a single very
long unwrapped Ports line [GENIE `iosxe/ShowVlanBrief`], so accept both.

### 7.2 aos-cx: `show vlan`

Vendor (layout reconstructed) [K-CX-VLAN] (10.16):
```
switch# show vlan
----------------------------------------------------------------------------------
VLAN  Name             Status  Reason      Type      Interfaces
----------------------------------------------------------------------------------
1     DEFAULT_VLAN_1   up      ok          static    1/1/3-1/1/4
12    VLAN12           up      ok          static    1/1/3,1/1/6,lag1-lag2
20    ManagementVLAN   down    admin_down  static    1/1/3,1/1/10
23    VLAN23           up      ok          port-access 1/1/1
```
Real device (non-vendor) [NTC `aruba_aoscx/show_vlan`]:
```
------------------------------------------------------------------------------------------------------------------
VLAN  Name                              Status  Reason                  Type        Interfaces
------------------------------------------------------------------------------------------------------------------
1     DEFAULT_VLAN_1                    up      ok                      default     1/1/34-1/1/52
3072  mgmt                              up      ok                      static      1/1/2-1/1/4,1/1/6-1/1/8,
                                                                                    1/1/17-1/1/19,1/1/47-1/1/52
```
Values: Status `up`/`down`; Reason `ok`, `admin_down` [K-CX-VLAN] (others such as `no_member_port`
UNVERIFIED medium); Type `static`, `default`, `port-access` [K-CX-VLAN][NTC] (`dynamic` UNVERIFIED).
Hazards: Interfaces wraps with a trailing comma; ranges `1/1/2-1/1/4` and `lag1-lag2`.

### 7.3 aos-s: `show vlans`

Real device (non-vendor) [NTC `hp_procurve/show_vlans*`], three layouts:
```
 Status and Counters - VLAN Information

  Maximum VLANs to support : 8
  Primary VLAN : DEFAULT_VLAN
  Management VLAN :

  802.1Q VLAN ID Name         Status       Voice Jumbo
  -------------- ------------ ------------ ----- -----
  1              DEFAULT_VLAN Port-based   No    No
  100            DEVOPS       Port-based   No    No
```
```
  VLAN ID Name                             | Status     Voice Jumbo
  ------- -------------------------------- + ---------- ----- -----
  1       DEFAULT_VLAN                     | Port-based No    No
  10      Router Vlan                      | Port-based No    No
  50      Voice                            | Port-based Yes   No
```
```
  VLAN ID Name                             Status       Voice
  ------- -------------------------------- ------------ -----
  1       DEFAULT_VLAN                     Port-based   No
  2000    dmz                              Port-based   No
```
Status: `Port-based` (`Protocol`, `Dynamic` for GVRP UNVERIFIED medium). No member ports in this
table (use `show vlans <id>` or `show vlans ports`). Hazards: header word `802.1Q VLAN ID` vs `VLAN ID`;
optional `|`; optional Jumbo column; names with spaces; `\r` line ends in one capture.

---

## 8. route summary

### 8.1 ios: `show ip route summary`

Real device (non-vendor) [NTC `cisco_ios/show_ip_route_summary`]:
```
IP routing table name is default (0x0)
IP routing table maximum-paths is 32
Route Source    Networks    Subnets     Replicates  Overhead    Memory (bytes)
connected       0           41          0           3936        11808
static          0           3           0           288         864
application     0           0           0           0           0
nhrp            0           0           0           0           0
eigrp 65329     3           3013        0           481920      868608
ospf 20         1           0           0           384         292
  Intra-area: 0 Inter-area: 0 External-1: 0 External-2: 1
  NSSA External-1: 0 NSSA External-2: 0
internal        24                                              240672
Total           28          3057        0           486528      1122244
```
Older IOS without Replicates [NTC `..._summary3`]:
```
IP routing table name is Default-IP-Routing-Table(0)
IP routing table maximum-paths is 16
Route Source    Networks    Subnets     Overhead    Memory (bytes)
connected       0           4           288         544
static          1           2           216         408
internal        3                                   3468
Total           4           6           504         4420
```
BGP/IS-IS sub-lines `  External: 0 Internal: 0 Local: 0`, `  Level 1: 0 Level 2: 0 Inter-area: 0`
[NTC][GENIE]. Count = Networks + Subnets. Hazards: Route Source has a process number/tag as a second
token (`ospf 20`, `isis test1`); indented detail lines; column set differs by release; `internal`
row has blanks; per-VRF needs `show ip route vrf <x> summary`. On L2 2960-X LAN Base the command
works for the static/connected table, UNVERIFIED (medium).

### 8.2 aos-cx: `show ip route summary [all-vrfs | vrf <name>]`

"Display the aggregate count of routes per routing protocol" [K-CX-ROUTE]. Vendor (layout
reconstructed) [K-CX-ROUTE]:
```
core1# show ip route summary all-vrfs
IP Route Table Summary

VRF name : red
Protocol        Active Routes   Protected Routes
connected       3               -
local           3               -
bgp             12              5
```
Protected Routes is 9300-only [K-CX-ROUTE search excerpt]; whether other platforms omit the column
or show `-` is UNVERIFIED. Protocol names: `connected`, `local`, `bgp` (`static`, `ospf` UNVERIFIED
high). Hazards: per-VRF blocks.

### 8.3 aos-s: `show ip route summary`

Vendor (layout reconstructed) [K-S-RSUM]:
```
switch(config)# show ip route summary

 IPv4 Route Table Summary

  Protocol  Active Routes
  --------- -------------
  Connected 1
  Static    1
```
Exists on 2530 (static routing) UNVERIFIED (medium). Protocol words `Connected`, `Static` (`OSPF`,
`RIP`, `BGP` UNVERIFIED high).

---

## 9. first-hop redundancy

### 9.1 ios: `show standby brief` (HSRP), `show vrrp brief`

Real device (non-vendor) [NTC `cisco_ios/show_standby_brief`] (shows interface-name wrap):
```
                     P indicates configured to preempt.
                     |
Interface   Grp  Pri P State   Active          Standby         Virtual IP
Vl1         1    120 P Active  local           10.1.1.3      10.1.1.1
Vl1         6    120 P Active  local           FE80::B         FE80::DEF
Vl51        1    120 P Init    unknown         unknown         10.1.51.1
Vl4010
     1    120 P Active  local           10.1.254.3    10.1.254.1
```
Non-preempt row (blank P column) [GENIE `iosxe/ShowStandbyBrief/golden_output1`]:
```
                     P indicates configured to preempt.
                     |
Interface   Grp  Pri P State   Active          Standby         Virtual IP
Vl1309      1    110   Init    unknown         unknown         40.1.31.100
```
HSRP State: `Init`, `Learn`, `Listen`, `Speak`, `Standby`, `Active` (Init/Active seen [NTC]; the rest
UNVERIFIED, high, from the HSRP state machine). Active/Standby columns: `local`, `unknown` or an
address. Empty: nothing printed, UNVERIFIED (medium).

`show vrrp brief`, VRRPv2 [NTC `cisco_ios/show_vrrp_brief`]:
```
Interface          Grp Pri Time  Own Pre State   Master addr     Group addr
Fa0/1              1   110 3570       Y  Master  192.168.88.2    192.168.88.1
Fa0/1.3            3   110 3570       Y  Backup  10.0.3.3        10.0.3.1
Fa0/1.4            4   100 3609       Y  Init    10.0.4.3        10.0.4.1
```
VRRPv3 (`fhrp version vrrp v3`, IOS-XE) [NTC `..._vrrpv3_brief`]:
```
  Interface          Grp  A-F Pri  Time Own Pre State   Master addr/Group addr
  Gi0/0                1 IPv4 110     0  N   Y  MASTER  10.0.1.2(local) 10.0.1.1
  Gi0/0                6 IPv6 110     0  N   Y  MASTER  FE80::1:A(local) FE80::1:1
  Gi0/0.4010           1 IPv4 110  3570  N   Y  BACKUP  10.254.1.3 10.254.1.1
```
VRRP State: v2 `Init`, `Backup`, `Master`; v3 upper-case `INIT`, `BACKUP`, `MASTER` [NTC][GENIE].
Hazards: long interface names wrap the rest of the HSRP row onto the next line; blank `P`/`Own`
columns; v3 combines two addresses in one column with `(local)` glued on; IPv6 groups mixed in.

### 9.2 aos-cx: `show vrrp brief`, `show vsx brief`

Vendor (layout reconstructed) [K-CX-VRRP] (10.14):
```
switch# show vrrp brief
VRRP is enabled

Interface  Grp  A-F   Pri  Time  Owner  Pre  State   Active addr/Group addr
1/1/1      1    IPv4  100  0     N      Y    ACTIVE  10.0.0.2(local) 10.0.0.1
1/1/2      1    IPv4  100  0     N      Y    INIT    AF-UNDEFINED no address
1/1/2      1    IPv6  100  0     N      Y    INIT    AF-UNDEFINED no address
```
State: `ACTIVE`, `INIT` seen; `STANDBY` implied by the state-transition counters ("Init to standby",
"Standby to active") [K-CX-VRRP]; older releases `MASTER`/`BACKUP`, UNVERIFIED (medium). Full output
shows INIT reasons `(Interface Down)`, `(Group Disabled)`, `(No Primary Group Address)` [K-CX-VRRP].
Hazards: `VRRP is enabled` preamble; address column can be `AF-UNDEFINED no address` (three tokens).

`show vsx brief` (key/value, VSX platforms: 6300 except S3L75A/76A/77A, 5420, 6400, 8100, 83xx,
8400, 9300, 10000) [K-CX-VSX]:
```
vsx-primary# show vsx brief
ISL State                              : In-Sync
Device State                           : Peer-Established
Keepalive State                        : Keepalive-Established
Device Role                            : primary
Number of Multi-chassis LAG interfaces : 2
```
Device State values [K-CX-VSX]: `Peer-Established` (steady), `Sync-Primary`, `Sync-Secondary`,
`Sync-Secondary-Linkup-Delay`, `Split-System-Primary`, `Split-System-Secondary`, `Waiting-For-Peer`.
ISL State `In-Sync` (others UNVERIFIED); Keepalive `Keepalive-Established` (others UNVERIFIED);
Role `primary`/`secondary`. Not configured: UNVERIFIED.

### 9.3 aos-s: `show vrrp summary`

`show vrrp` (per-VR blocks, `State : Main Router` per 16.11 search excerpt) and `show vrrp summary`
exist on 2930M/F [K-S-VRRP]; 2530 has no VRRP, UNVERIFIED (high). Vendor (layout reconstructed)
[K-S-VRRP]:
```
switch# show vrrp summary

 Vlan VRID Pri Up Time  Owner  Pre   State   Master Addr Virtual Addr
 ---- ---- --- -------- ------ ----- ------- ----------- ---------------
 10   11   11  2 mins   Backup True  Master  11.1.1.2    11.1.1.9
 10   123  202 2 mins   Backup True  Master  10.1.1.2    10.1.1.100
 110  124  202 2 mins   Backup True  Backup  110.1.1.1   110.1.1.110
```
State `Master`, `Backup` (`Init` UNVERIFIED high). Hazards: Up Time contains a space; IPv4 only
("all VRRP IPv4 instances").

---

## 10. spanning tree

### 10.1 ios

One screen of counts: `show spanning-tree summary`; blocked ports: `show spanning-tree blockedports`;
root per VLAN: `show spanning-tree root`. Recommended pair: `summary` (counts per VLAN/MST, root
bridge list) + `blockedports` (names). `show spanning-tree` (full) lists every port per VLAN and is
long. Real device (non-vendor) [GENIE `iosxe/ShowSpanningTreeSummary/golden_output_rpvst`]:
```
Switch is in rapid-pvst mode
Root bridge for: VLAN0001
Extended system ID                      is enabled
Portfast Default                        is disabled
Portfast Edge BPDU Guard Default        is disabled
Portfast Edge BPDU Filter Default       is disabled
Loopguard Default                       is disabled
PVST Simulation Default                 is enabled but inactive in rapid-pvst mode
Bridge Assurance                        is enabled
EtherChannel misconfig guard            is enabled
UplinkFast                              is disabled
BackboneFast                            is disabled
Configured Pathcost method used is short

Name                   Blocking Listening Learning Forwarding STP Active
---------------------- -------- --------- -------- ---------- ----------
VLAN0001                     0         0        0          1          1
---------------------- -------- --------- -------- ---------- ----------
1 vlan                       0         0        0          1          1
```
MST variant [GENIE `..._mstp`] (tail):
```
Switch is in mst mode (IEEE Standard)
Root bridge for: MST0, MST100
...
Name                   Blocking Listening Learning Forwarding STP Active
---------------------- -------- --------- -------- ---------- ----------
MST0                         3         0        0          9         12
MST100                       3         0        0          1          4
---------------------- -------- --------- -------- ---------- ----------
2 msts                       6         0        0         10         16
```
`show spanning-tree root` [NTC]:
```
                                        Root    Hello Max Fwd
Vlan                   Root ID          Cost    Time  Age Dly  Root Port
---------------- -------------------- --------- ----- --- ---  ------------
VLAN0001         11175 5c6e.f0a7.a0b0         0    2   20  15
VLAN0003         11195 5c6e.f0a7.a0b0         0    2   20  15  Po34
```
Per-port detail from `show spanning-tree` [NTC]: `Gi1/0               Altn BLK 4         128.5    Shr`;
Role `Root`, `Desg`, `Altn`, `Back`, `Mstr` (Mstr UNVERIFIED medium); Sts `FWD`, `BLK`, `LRN`, `LIS`,
`BKN*` (BKN* = broken, e.g. inconsistent; UNVERIFIED medium). `show spanning-tree blockedports`
layout (`Name  Blocked Interfaces List` + `Number of blocked ports (segments) in the system : N`)
UNVERIFIED (medium). "Root bridge for:" line is absent when the switch is root for nothing,
UNVERIFIED (medium). Empty Root Port means this switch is root [NTC]. Hazards: "Root bridge for:"
list wraps for many VLANs; the summary's option lines vary by release and mode (skip them, anchor on
the `Name  Blocking` header); totals row (`1 vlan`, `2 msts`) is not a VLAN.

### 10.2 aos-cx: `show spanning-tree` (+ `show spanning-tree summary root`)

Vendor (layout reconstructed) [K-CX-STP] (10.16, MSTP):
```
switch# show spanning-tree
Spanning tree status : Enabled Protocol: MSTP

MST0
  Root ID    Priority   : 0
             MAC-Address: 02:12:12:00:01:00
             This bridge is the root
             Hello time(in seconds):2 Max Age(in seconds):20 Forward Delay(in seconds):15

  Bridge ID  Priority   : 0
             MAC-Address: 02:12:12:00:01:00
             Hello time(in seconds):2 Max Age(in seconds):20 Forward Delay(in seconds):15

PORT     ROLE        STATE      COST       PRIORITY  TYPE      BPDU-Tx    BPDU-Rx    TCN-Tx     TCN-Rx
-------- ----------- ---------- ---------- --------- --------- ---------- ---------- ---------- ----------
1/1/1    Designated  Forwarding 20000      128       P2P Edge  100        60         20         10
1/1/3    Designated  Forwarding 20000      128       Shr       100        60         20         10
1/1/5    Alternate   Loop-Inc   20000      128       Shr Edge  100        60         20         10
1/1/6    Alternate   Root-Inc   20000      128       Shr Edge  100        60         20         10
1/1/7    Root        Forwarding 2000       128       P2P       100        60         20         10
1/1/8    Alternate   Blocking   20000      128       P2P       100        60         20         10
1/1/9    Disabled    Down       20000      128       P2P       100        60         20         10

Number of topology changes    : 4
Last topology change occurred : 516 seconds ago
```
`show spanning-tree summary root` (RPVST) [K-CX-STPROOT]:
```
switch# show spanning-tree summary root
STP status            : Enabled
Protocol              : RPVST
System ID             : f8:60:f0:c9:70:40
Root bridge for VLANs : 1-10

                                          Root  Hello Max Fwd
VLAN     Priority Root ID           cost      Time  Age Dly Root Port
-------- -------- ----------------- --------- ----- --- --- ------------
VLAN1    32768    f8:60:f0:c9:70:40 0         2     20  15  0
VLAN2    32768    f8:60:f0:c9:70:40 0         2     20  15  0
```
ROLE: `Designated`, `Root`, `Alternate`, `Disabled` (`Backup` UNVERIFIED high); STATE: `Forwarding`,
`Blocking`, `Loop-Inc`, `Root-Inc`, `Down` (10.09+) [K-CX-STP]; `Learning`/`Listening` per
`summary port` counts (search excerpt). TYPE `P2P`, `Shr`, optionally followed by `Edge` (two tokens).
A port-security-held port shows role Disabled / state Blocking while hardware learns (search excerpt).
`show spanning-tree summary port` gives Blocking/Listening/Learning/Forwarding/Down counts per VLAN
(exists [search excerpt]; layout not read). Hazards: TYPE has an optional second word; one table per
MST instance / VLAN; root port `0` when root.

### 10.3 aos-s: `show spanning-tree`

"Displays the global and regional spanning-tree status for the switch, and displays the per-port
spanning-tree operation at the regional level" (MSTP is the default) [K-S-STP]; the vendor example is
an image. UNVERIFIED (medium) layout from background knowledge:
```
 Multiple Spanning Tree (MST) Information

  STP Enabled   : Yes
  Force Version : MSTP-operation
  ...
  CST Root MAC Address : 0024a8-cd3d00
  CST Root Priority    : 32768
  CST Root Path Cost   : 0
  CST Root Port        : This switch is root
  ...
  Port  Type      | Cost      Priority Role       State      Designated Bridge Hello Time PtP Edge
  ----- --------- + --------- -------- ---------- ---------- ----------------- ---------- --- ----
  1     100/1000T | 20000     128      Designated Forwarding 0024a8-cd3d00     2          Yes No
  2     100/1000T | Auto      128      Disabled   Disabled
```
Designated Bridge, Hello Time, PtP and Edge appear only for ports connected to active devices
[K-S-STP] (so disconnected ports have short rows). Role/State value sets UNVERIFIED (Role
`Root`, `Designated`, `Alternate`, `Backup`, `Disabled`; State `Forwarding`, `Blocking`, `Learning`,
`Disabled`). If STP is disabled the output is short (`STP Enabled : No`), UNVERIFIED (medium).

---

## 11. Cross-cutting parsing hazards (ranked)

1. **Fixed-width columns with spaces inside values**: IOS `show interfaces status` Name, CX STP
   TYPE `P2P Edge`, CX VSF `Member Booting`, AOS-S VSF Model, IOS stack state `HA Sync in Progress`,
   AOS-S VRRP `2 mins`. Use header-derived column offsets, or anchor on known first/last tokens.
2. **Empty cells**: CX `show lacp interfaces` down/static rows, AOS-S OSPF blank NbIfState, IOS
   HSRP blank `P`, IOS provisioned members' blank H/W Version, AOS-S brief empty Type/Mode.
3. **Wrapped rows**: etherchannel members, `show vlan brief` Ports, CX `show vlan` Interfaces,
   HSRP long interface names (row continues on next line), IPv6 BGP neighbors (UNVERIFIED).
4. **Repeated block headers that are row context**: BGP `For address family:` (IOS) and
   `VRF :`/`Address-family :` (CX); CX OSPF `OSPF Process ID n VRF x`; EIGRP per-AS header; STP per
   VLAN/MST tables.
5. **Glued markers**: `*1` (stack local), `*2` (AOS-S VSF local), `*10.1.1.5` (CX dynamic BGP
   neighbor), `lag1(mc)`, `D1-Trk1`, `A8*`, `2500FDx*`, `10.0.1.2(local)`, `Po1(SU)`, `Te2/0/1(P)`.
6. **Two-line headers** (IOS EIGRP, CX lacp/brief, AOS-S OSPF/VSF): don't treat line 2 as data.
7. **Words in numeric columns**: IOS BGP State/PfxRcd (`Idle (Admin)` vs a count); Up/Down `never`.
8. **Release-dependent vocabulary**: `Master`/`Active` (IOS stack), `Master`/`Conductor` (CX VSF),
   `Master`/`MASTER`/`ACTIVE` (VRRP), legend lines in etherchannel, column sets in route summary.
9. **Line noise**: `\r\n`, tabs (router etherchannel), trailing spaces, timestamp preambles
   (`Load for five secs`), `-- MORE --` if paging is on, a bare `.` in AOS-S Mode.
10. **Truncation**: IOS status Name 18 chars, VLAN names 32 chars, AOS-S port `1/28-...` and model `...`.

---

## 12. Not covered / open items

- AOS-CX `show interface brief` and AOS-S `show lacp` and `show spanning-tree`: no vendor text sample
  retrieved (image or page not found); layouts above are UNVERIFIED.
- AOS-CX `show lag brief`: existence not confirmed.
- AOS-S `show ip bgp summary` and `show vrrp` on 2930F: availability not confirmed per model.
- Empty-table / feature-not-configured output for every table on every family: not documented; all
  UNVERIFIED. Capture on CML (IOS-XE) and on Aruba hardware.
- IOS: `show spanning-tree blockedports` sample, `show stackwise-virtual` member states / 9500 SVL
  `show switch` layout, named-mode EIGRP headers, per-VRF OSPF output, HSRP version 2 layout, ISR
  support for `show etherchannel summary`.
- AOS-CX: OSPF/BGP state vocabulary beyond the sampled values; `show vsf` on non-VSF platforms;
  `show vsx brief` when VSX is not configured.
- Verbatim HPE column spacing: the HPE site blocks automated download; a person with a browser (or
  the PDFs) should replace the "layout reconstructed" blocks with exact copies.

## Sources

Cisco (read through a JS-rendering fetcher or search excerpts):
- [C-OSPF] Cisco IOS IP Routing: OSPF Command Reference, "show ip ospf through T":
  https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/iproute_ospf/command/iro-cr-book/ospf-s1.html
- [C-BGP] IP Routing: BGP Command Reference, IOS XE 3SE (Catalyst 3850), "show ip through Z":
  https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/iproute_bgp/command/iproute_bgp-xe-3se-3850-cr-book/iproute_bgp-xe-3se-3850-cr-book_chapter_0100.html
- [C-EC] Layer 2 Configuration Guide, IOS XE 17.13 (Catalyst 9300), Configuring EtherChannels (flag
  legend via search excerpt):
  https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst9300/software/release/17-13/configuration_guide/lyr2/b_1713_lyr2_9300_cg/configuring_etherchannels.html
- [C-STK] Command Reference, IOS XE 16.11 (Catalyst 9300), Stack Manager and HA Commands:
  https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst9300/software/release/16-11/command_reference/b_1611_9300_cr/stack_manager_and_high_availability_commands.html
- [C-IF] Command Reference IOS XE 16.9 and 17.15 (Catalyst 9300), Interface and Hardware Commands
  (search excerpts): https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst9300/software/release/16-9/command_reference/b_169_9300_cr/interface_and_hardware_commands.html
  and .../release/17-15/command_reference/b_1715_9300_cr/interface_and_hardware_commands.html

HPE Aruba AOS-CX (all under https://arubanetworking.hpe.com/techdocs/AOS-CX/):
- [K-CX-OSPF] 10.15/HTML/ip_route_4100i-5420-6000-6100-6200/Content/Chp_OSPFv2/OSPFv2_cmds/sho-ip-osp-nei-10.htm
- [K-CX-LACPAGG] 10.13/HTML/link_aggregation/Content/Chp_LAG/LACP_LAG_cmds/sho-lac-agg-10.htm
- [K-CX-LACPINT] 10.13/HTML/link_aggregation/Content/Chp_LAG/LACP_LAG_cmds/sho-lac-int-10.htm
- [K-CX-LAG] 10.13/HTML/link_aggregation/Content/Chp_LAG/LACP_LAG_cmds/sho-lag.htm
- [K-CX-IF] 10.14/HTML/fundamentals_6200/Content/Chp_IfaceCfg/Iface_cmds/sho-int-10.htm (syntax);
  brief column header from search excerpts of 10.15/10.17 PDF/vsf.pdf and the AOS-S/AOS-CX
  Transceiver Guide
- [K-CX-BGP] 10.15/HTML/ip_route_8400/Content/Chp_BGP/BGP_cmds/sho-bgp-sum-10.htm
- [K-CX-VSF] 10.15/HTML/vsf/Content/VSF_cmds/sho-vsf.htm
- [K-CX-VSX] 10.16/HTML/vsx/Content/VSX_cmds/sho-vsx-bri-10.htm
- [K-CX-VLAN] 10.16/HTML/l2_bridging_6300-6400/Content/Chp_vlans/vlan_cmds/sho-vla.htm
- [K-CX-VRRP] 10.14/HTML/ip_route_4100i-6000-6100-6200/Content/Chp_VRRP/VRRP_cmds/sho-vrr-10.htm
- [K-CX-STP] 10.16/HTML/l2_bridging_6300-6400/Content/Chp_stp/mstp_cmds/sho-spa-tre-mst-10.6.htm
- [K-CX-STPROOT] 10.13/HTML/l2_bridging_8400/Content/Chp_stp/rpvst_cmds/sho-spa-tre-sum-roo-rpv-10.htm
- [K-CX-ROUTE] 10.15/HTML/ip_route_8400/Content/Chp_VRF/VRF_cmds/sho-ip-rou.htm

HPE Aruba AOS-S (all under https://arubanetworking.hpe.com/techdocs/AOS-S/):
- [K-S-OSPF] 16.10/MRG/WC/content/common%20files/vie-osp-nei-inf.htm (2930M/F)
- [K-S-BGP] 16.10/MRG/KB/content/kb/cnf-pro3.htm (3810M/5400R); command list 16.11/MRG/KB/content/kb/bgp-sho-rou.htm
- [K-S-TRK] 16.11/MCG/KB/content/kb/sho-tru.htm
- [K-S-LACP] 16.10/MCG/KB/content/kb/def-por-ope.htm
- [K-S-LACPOP] 16.10/MCG/WC/content/common%20files/tru-gro-ope-usi-lac.htm (2930M/F)
- [K-S-VSF] 16.11/MCG/WC/content/wc/sho-vsf.htm
- [K-S-STK] 16.10/ATMG/KB/content/kb/mon-sta.htm
- [K-S-RSUM] 16.11/MRG/KB/content/kb/adm-dis.htm
- [K-S-VRRP] 16.10/MRG/WC/content/wc/sho-vrr-sum.htm (2930M/F)
- [K-S-STP] 16.10/MRG/WB/content/common%20files/sho-spa-tre.htm (example is an image)

Non-vendor real-device captures (open-source parser test inputs):
- [NTC] networktocode/ntc-templates, `tests/cisco_ios/`, `tests/aruba_aoscx/`, `tests/hp_procurve/`
  (files named in each block): https://github.com/networktocode/ntc-templates/tree/master/tests
- [GENIE] CiscoTestAutomation/genieparser, `src/genie/libs/parser/iosxe/tests/<Parser>/cli/equal/`:
  https://github.com/CiscoTestAutomation/genieparser/tree/main/src/genie/libs/parser/iosxe/tests
- [REPO] this repo, `tests/fixtures/show/` and `tests/fixtures/neighbors/`.
