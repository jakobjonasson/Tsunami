# Hardware catalog verification (work package 1.9)

Research date: 2026-10-10. Scope: every entry in `src/tsunami/core/data/hardware.yml`, checked against vendor sources where reachable. Method: web search plus fetching Cisco End-of-Sale/End-of-Life bulletins and Aruba EoS notice PDFs. No device was available.

**How to read the tables.** A value with a URL was read in (or reported by a search-result summary of) that source; "(read)" means I fetched the page, "(search summary)" means I only saw a search summary of it. **UNVERIFIED** means no source was found or read; the reason follows. `eos_year` is the year of the end-of-sale date (last day to order) of the newest member of the family; older members of the same entry went end-of-sale earlier. Cisco URLs are under `https://www.cisco.com/c/en/us/products/collateral/switches/`, abbreviated `.../`.

**Depth caveats.**
- Cisco `platform` (IOS vs IOS-XE) was not checked against a source for any family. Values are background knowledge (high confidence: 9xxx, 3850, 3650, 4500-X, IE3x00 are IOS-XE; 2960 family, 3560, 3750, 6500, IE4000 are IOS).
- Cisco lifecycle dates come from bulletin pages or search-result summaries of those bulletins.
- Aruba lifecycle for 6000, 2920 and 3800 comes from third-party trackers only.
- `model_pattern` was never tested against a real device. Patterns were checked only against the formats in `docs/research/aruba-cli.md` section 7 and the Cisco model strings that `show version` prints.

## Lifecycle, eos_year and platform

| Entry id | Field | Value | Source |
|---|---|---|---|
| cisco-c9300, c9200, c1000, c9500, c9400, c9600, c9350, c9610, ie3x00 | lifecycle | current | UNVERIFIED. No EoS notice for the whole family found; only partial notices, e.g. C9500-12Q/24Q/40X (`.../catalyst-9500-series-switches/c9500-12q-c9500-24q-c9500-40x-eol.html`, search summary). Absence of a notice in a web search is weak evidence. |
| cisco-c9300, c9200, c9500, c9400, c9600, c9350, c9610, ie3x00 | platform | ios-xe | UNVERIFIED (background knowledge). |
| cisco-c1000 | platform | ios | UNVERIFIED. Search results say only "Cisco IOS Software"; the 15.2(7)E train was not confirmed. |
| cisco-c9350, cisco-c9610 | existence | both exist | Cisco IOS XE 26.1 release-note index lists both (search result, <https://www.cisco.com/c/en/us/support/ios-nx-os-software/ios-xe-26/products-release-notes-list.html>). |
| cisco-c2960x | eos_year | 2022 (2022-10-31) | `.../catalyst-2960-x-series-switches/eos-eol-notice-c51-744432.html` (search summary). The 2960-XR has its own earlier notice, 2020-10-31 (`.../catalyst-2960-xr-series-switches/eos-eol-notice-c51-744422.html`, search summary). |
| cisco-c2960x, c2960l, c2960plus, c2960cx, c2960s, c3750x, c3560x, c4500-ios, c6500, ie4000 | platform | ios | UNVERIFIED. |
| cisco-c2960l | eos_year | 2021 (2021-10-31, EOL13611) | `.../catalyst-2960-l-series-switches/eos-eol-notice-c51-744433.html` (read). Was 2024; 2023 is the date of the separate IOS-software notice. |
| cisco-c3850 | eos_year | 2020 (2020-10-30, EOL13188) | `.../catalyst-3850-series-switches/eos-eol-notice-c51-743072.html` (read). Was 2022. Later notices cover software and licenses (2023-09-05), accessories (2023-05-31) and fiber SKUs (2022-04-30). |
| cisco-c3850, c3650, c4500e | platform | ios-xe | UNVERIFIED. |
| cisco-c3650 | eos_year | 2021 (2021-10-31, EOL13617) | `.../catalyst-3650-series-switches/eos-eol-notice-c51-744426-b.html` (read). Was 2022. The Mini and the licenses ended in 2023. |
| cisco-c2960plus | eos_year | 2021 (2021-10-31) | `.../catalyst-2960-plus-series-switches/eos-eol-notice-c51-744423.html` (search summary). Was 2020. |
| cisco-c2960cx | eos_year | 2024 (2024-04-30; 2960-CX EOL15071, 3560-CX selected models EOL15072) | `.../catalyst-2960-cx-series-switches/catalyst-2960-cx-series-switch-eol.html` (read); `.../catalyst-3560-cx-series-switches/catalyst-3560-cx-serie-switche-eol.html` (search summary). 2960-C went 2020-10-30, EOL13189 (`https://www.cisco.com/c/en/us/products/switches/catalyst-2960-c-series-switches/eos-eol-notice-c51-743071.html`, read). The 3560-CX mGig went 2025-04-30 (`.../catalyst-3560-cx-series-switches/catalyst-3560-cx-series-mgig-switch-eol.html`, search summary) and is later than the stored year. Was 2021. |
| cisco-c3750x | eos_year | 2016 (2016-10-30, EOL10623, 3750-X) | `.../catalyst-3560-x-series-switches/eos-eol-notice-c51-736139.html` (read). 3750-E and 3750-G went 2013-01-30, EOL8044 (`.../catalyst-3750-series-switches/eol_c51-696372.html`, search summary). Was 2015. |
| cisco-c3560x | eos_year | 2016 (2016-10-30, EOL10623) | Same notice as 3750-X (read). 3560-E and 3560-G: 2013 (EOL8044). Was 2015. |
| cisco-c2960s | eos_year | 2015 (2015-11-06, EOL10043) | `.../catalyst-2960-series-switches/eos-eol-notice-c51-733348.html` (search summary; covers 2960-S and 2960-SF). The original 2960 and 2960-G were not checked: UNVERIFIED. Was 2016. |
| cisco-c4500e | eos_year | 2020 (4500-X and Sup 8-E 2020-10-30; Sup 7-E 2017-10-31) | 4500-X, EOL13216: `.../catalyst-4500-x-series-switches/eos-eol-notice-c51-743098.html` (search summary). Sup 8-E, EOL13217: `.../catalyst-4500-series-switches/eos-eol-notice-c51-743088.html` (search summary; the URL-to-notice mapping is inferred from the result list). Was 2021. |
| cisco-c4500e | platform note | | The chassis model (WS-C4507R+E) does not identify the supervisor, so the model string cannot tell Sup 6-E (IOS) from Sup 7/8-E (IOS-XE). |
| cisco-c4500-ios | eos_year | 2017 (4948E, 2017-10-31, EOL11273) | `.../catalyst-4900-series-switches/eos-eol-notice-c51-738116.html` (search summary). Sup 6-E and older 4500 chassis: UNVERIFIED, no bulletin found. Was 2016. |
| cisco-c6500 | eos_year | 2022 (6807-XL 2022-04-30, EOL13872); 6500-E chassis and VS-S2T 2020-10-30 (EOL13211) | `.../catalyst-6800-series-switches/eos-eol-notice-c51-2438805.html`; `.../catalyst-6500-series-switches/eos-eol-notice-c51-743096.html` (both search summaries). Was 2017. Whether 6807-XL runs IOS only was not checked. |
| cisco-ie4000 | eos_year | 2026 (IE4010 2026-07-31); IE4000 and IE5000 2024-07-26 | `.../industrial-ethernet-4010-series-switches/industrial-ethernet-4010-series-eol.html`; `https://www.cisco.com/c/en/us/products/switches/industrial-ethernet-4000-series-switches/industrial-ethernet-4000-series-switches-eol.html`; `.../industrial-ethernet-5000-series-switches/indus-ethernet-5000-series-switch-eol.html` (all search summaries). Was 2023. |
| aruba-cx6300, 6200, 6100, 6400, 8325, 4100i | lifecycle | current | UNVERIFIED. No EoS notice found. The 8360 v1 SKUs went EoS 2023-02-28 and v2 replaces them (<https://asp-documents.arubanetworks.com/portals/0/Aruba%208360%20Switch.pdf>, read), so 8325 / 8360 stays `current`. |
| aruba-cx6000 | lifecycle, eos_year | end-of-sale, 2026 (2026-03-31) | UNVERIFIED against HPE: trackers only (<https://www.router-switch.com/eol-eosl-checker/aruba/r8n85a_62140.html>, <https://www.layer23-switch.com/eol-eosl-tool/r8n85a/>); successor R8N85B. Was 2024. |
| aruba-cx8320 | lifecycle, eos_year | end-of-sale, 2023 | UNVERIFIED. No notice found for 8320 or 8400; left as is. |
| aruba-cx6300, 6200, 6100, 6400, 8325, 4100i, 6000, 8320 | platform | aos-cx | 6000 is called an AOS-CX switch in the 2530 notice (<https://asp-documents.arubanetworks.com/portals/0/el/EOS_2530-Gigabit-24-48-port-Switches.pdf>, read). The rest UNVERIFIED (background knowledge, high confidence). |
| aruba-2930f | lifecycle | current | Negative evidence only: a Servnet page checked 2026-10-08 reports none of the 208 HPE EoS notices covers a 2930F SKU, and a 2025 Airheads thread says no EoS was announced (<https://airheads.hpe.com/discussion/end-of-sale-date-for-hpe-aruba-2930f-switches>). UNVERIFIED against HPE. |
| aruba-2930m | lifecycle | current | Same negative evidence (<https://www.servnetuk.com/server-end-of-life/aruba-2930m>). UNVERIFIED against HPE. |
| aruba-5400r | lifecycle | current | UNVERIFIED. No notice found; a 2017 HPE community reply said still in production (<https://community.hpe.com/t5/hpe-aruba-networking-provision/switch-information/m-p/6970307>). |
| aruba-2540 | lifecycle, eos_year | end-of-sale, 2021 (2021-10-31, announced 2021-04-30) | <https://asp-documents.arubanetworks.com/portals/0/el/EOS_2540-Switches.pdf> (search summary). Was `current`. Replacement: CX 6100. |
| aruba-3810m | lifecycle, eos_year | end-of-sale, 2023 (2023-06-30, announced 2022-11-09) | <https://asp-documents.arubanetworks.com/portals/0/el/EOS_Notice_3810-Switches.pdf> (read). A reseller reports a revised date of 2025-01-31; not confirmed. Was `current`. |
| aruba-2530 | eos_year | 2021 (2021-10-31, announced 2021-05-31), 24/48-port gigabit | <https://asp-documents.arubanetworks.com/portals/0/el/EOS_2530-Gigabit-24-48-port-Switches.pdf> (read). 8-port models: `EOS_2530-Gigabit-8-port-Switches.pdf` in the same folder (search summary). The 10/100 models were not checked. Was 2019. |
| aruba-2920 | eos_year | 2018 | UNVERIFIED: trackers only (<https://www.router-switch.com/eol-eosl-checker/aruba/j9726a_34321.html>, 2018-05-31 to 2018-07-02). Was 2017. 2620 not checked. |
| aruba-3800 | eos_year | 2016 | UNVERIFIED: trackers only (<https://www.router-switch.com/eol-eosl-checker/hpe/j9575a_34318.html>, 2016-10-31). Was 2018. |
| aruba-legacy-procurve | eos_year | 2014 | UNVERIFIED. Not researched. |
| aruba-2930f, 2930m, 2540, 3810m, 5400r, 2530, 2920, 3800, legacy-procurve | platform | aos-s | UNVERIFIED (background knowledge; consistent with `docs/research/aruba-cli.md`). |
| aruba-2930f .. aruba-legacy-procurve | console_baud | 9600 | `docs/research/aruba-cli.md` section 8.2: boot output at 9600, then speed-sense. Not re-checked. |

## Examples

| Entry id | Examples | Status |
|---|---|---|
| cisco-* | all | UNVERIFIED against data sheets. The EoS bulletins confirm these names: WS-C2960L-24PS-LL, WS-C2960L-48TS-LL, WS-C2960C-8PC-L, WS-C2960CX-8PC-L, and the WS-C3650-* family. The rest are from background knowledge. `test_examples_match_their_own_entry` checks each Cisco example against its entry's `model_pattern`. |
| aruba-cx6300 | JL658A (6300M 24SFP+ 4SFP56) | Verified in the 3810 EoS notice (read). JL659A (48-port Smart Rate PoE) and JL762A (48x1G non-PoE bundle) come from reseller listings only: UNVERIFIED against HPE. |
| aruba-cx6200 | JL725A (24G PoE), JL726A (48G) | Reseller listings only. UNVERIFIED against HPE. |
| aruba-cx6100 | JL679A, JL678A | UNVERIFIED: no description found. |
| aruba-cx6400 | R0X26C (6405 v2), R0X27C (6410 v2) | Reseller listings (CDW, e-nitiative). Replaced R0X26A and R0X31A: R0X26A is the v1 6405 bundle and R0X31A is a management module, not a chassis. |
| aruba-cx8325 | JL624A, JL700C | JL700C is the 8360-32Y4C v2 bundle (8360 notice, read). Replaced JL700A, which went end-of-sale 2023-02-28. JL624A is an 8325-32C per one reseller page: UNVERIFIED. |
| aruba-cx4100i | JL817A | `buy.hpe.com` listing: 12-port PoE, 2x SFP+ (<https://buy.hpe.com/my/en/networking/switches/fixed-port-l3-managed-ethernet-switches/networking-cx-switch-series/hpe-aruba-networking-cx-4100i-switch-series/p/1013625614>). |
| aruba-cx6000 | R8N85A (48G CL4 4SFP), R8N86A (48G 4SFP) | Verified in the 2530 notice (read). |
| aruba-cx8320 | JL479A, JL375A | JL375A is an 8400 chassis bundle (HPE listing). JL479A: UNVERIFIED. |
| aruba-2930f | JL253A (24G 4SFP+), JL258A (8G PoE+ 2SFP+), JL261A (24G PoE+ 4SFP) | Reseller listings. Replaced JL263A: its description could not be confirmed (one listing says 24G PoE+ TAA, another 48-port). |
| aruba-2930m | JL319A (24G 1-slot), JL322A (48G PoE+ 1-slot) | Reseller listings only. |
| aruba-2540 | JL354A (24G 4SFP+), JL356A (24G PoE+ 4SFP+) | Reseller listings only. |
| aruba-3810m | JL073A (24G PoE+ 1-slot), JL074A (48G PoE+ 1-slot) | Verified in the 3810 EoS notice (read). |
| aruba-5400r | J9850A, J9821A | Reseller listings: both a 5406R zl2 chassis. UNVERIFIED against HPE. |
| aruba-2530 | J9772A (48G PoE+), J9773A (24G PoE+) | Verified in the 2530 notice (read). Replaced J9779A, a 10/100 model not in the notice. |
| aruba-2920, 3800, legacy-procurve | all | UNVERIFIED beyond tracker pages (J9726A is a 2920-24G; J9575A has conflicting descriptions on 3800 tracker pages). |

## model_pattern

All UNVERIFIED on devices. Reasoning against the known formats:

- AOS-CX `show system` Product Name starts with the part number, then a description holding the series (`JL665A 6300F 48G CL4 PoE 4SFP56 Sw`, `aruba-cli.md` section 7.1). The numeric series patterns (`6300`, `6200`, ...) match that description. 6400-series chassis probably report `6405` or `6410` rather than `6400`, so the pattern became `64(00|05|10)`; UNVERIFIED.
- AOS-S `show modules` Chassis line: `3800-24G-PoE+-2SFP+ J9573A`, `5406Rzl2 J9850A` (`aruba-cli.md` section 7.2). The 5400R chassis prints `5406Rzl2`, not `5400R`, so the pattern became `54(06|12)R` and the legacy zl pattern gained `54(06|12)zl`. Whether the 2530, 2540 and 2930 lines use the same `Chassis:` format is still UNVERIFIED in `aruba-cli.md`.
- Cisco: patterns are anchored on the model string. The Catalyst 9400 and 9600 chassis models are `C9404R`, `C9407R`, `C9410R` and `C9606R`, which `^C9400` and `^C9600` did not match; fixed. 2960-SF models (`WS-C2960SF-`) did not match the 2960-S pattern; fixed. `WS-C4500X-` matched both 4500 patterns; the IOS 4500 pattern now excludes it.
- Remaining overlap: `WS-C4507R+E` and similar chassis match both `cisco-c4500e` and `cisco-c4500-ios`, because the chassis does not reveal the supervisor. A model-mismatch warning cannot tell them apart; the device pipeline's platform check does.

## Changes made to hardware.yml

- Renamed keys: `family` to `label`, `show_version_model` to `model_pattern` (all entries). Rewrote the header.
- `eos_year`: cisco-c2960l 2024 to 2021; cisco-c3850 2022 to 2020; cisco-c3650 2022 to 2021; cisco-c2960plus 2020 to 2021; cisco-c2960cx 2021 to 2024; cisco-c3750x 2015 to 2016; cisco-c3560x 2015 to 2016; cisco-c2960s 2016 to 2015; cisco-c4500e 2021 to 2020; cisco-c4500-ios 2016 to 2017; cisco-c6500 2017 to 2022; cisco-ie4000 2023 to 2026; aruba-cx6000 2024 to 2026 (tracker only); aruba-2530 2019 to 2021; aruba-2920 2017 to 2018 (tracker only); aruba-3800 2018 to 2016 (tracker only).
- `lifecycle` from current to end-of-sale, with `eos_year`: aruba-2540 (2021), aruba-3810m (2023).
- `model_pattern`: cisco-c9400 `^C94(00|04|07|10)`; cisco-c9600 `^C96(00|06)`; cisco-c2960s `^WS-C2960(S|SF|G)?-`; cisco-c4500-ios excludes `4500X`; aruba-cx6400 `64(00|05|10)`; aruba-5400r `54(06|12)R`; aruba-legacy-procurve adds `54(06|12)zl`.
- `examples`: aruba-cx6400 to [R0X26C, R0X27C]; aruba-cx8325 JL700A to JL700C; aruba-2930f JL263A to JL261A; aruba-2530 J9779A to J9773A.

## Possibly missing families (not added)

- Cisco: Catalyst 3560 and 3750 originals and V2 (older than the listed -E/-G/-X lines; `^WS-C3750` already matches the 3750 ones), Catalyst 2960-G/original 2960 (matched by the 2960-S pattern), IE1000/IE2000/IE3000, Catalyst 3560-CX mGig, Catalyst Digital Building series.
- Aruba: CX 10000, CX 9300, CX 8100 (AOS-CX); 2530 10/100 and 2615/2915 (AOS-S, partly covered by the legacy entry).
