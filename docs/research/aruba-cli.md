<!-- Research notes gathered 2026-10-10 for the Aruba platform drivers (design.md, Platform drivers).
     Sources are in the table at the end. Items marked UNVERIFIED must be confirmed on a device. -->

# Aruba AOS-CX and AOS-S driver research (for a Netmiko 4.8.0 tool)

Research date: 2026-10-10. Target: Netmiko 4.8.0 (tag `v4.8.0` confirmed: `netmiko/__init__.py` has `__version__ = "4.8.0"`).

## 0. How to read this report

**Evidence tags.** Every fact carries `[Kn]` (a key into the Sources table at the bottom, which holds the full URL) or is marked **UNVERIFIED (confidence: high/medium/low)**. "UNVERIFIED (high)" means I am fairly sure from background knowledge, but I did not find a primary source that I read.

**Method caveats (important for trust).**
- `arubanetworking.hpe.com/techdocs` returns HTTP 403 to plain HTTP clients (Akamai). I read the HPE docs through a JS-rendering fetcher and through PDF-to-text conversion. The text is the published HPE text; the URLs in the Sources table are the canonical doc URLs.
- The older "CLI Bank" pages (`.../AOSCX-CLI-Bank/...`) returned 404 through the fetcher. For those commands I used the 10.13 and 10.14 Fundamentals/Security/CLI guides instead.
- AOS-CX docs list most commands as "Command History: 10.07 or earlier". That is the earliest version HPE documents, not proof of when the command first appeared.
- Netmiko facts come from reading the actual v4.8.0 source files (quoted below).
- All sample configs/outputs below use made-up values. Where an excerpt is a composite (assembled from real captures plus documented syntax), it says so.
- Things I could not verify are collected in the final section **"Unverified / open items"**.

---

## 1. Session basics

### 1.1 Netmiko 4.8.0 source (both classes)

**Dispatcher** [N5]:
```python
"aruba_aoscx": ArubaCxSSH,
"aruba_osswitch": HPProcurveSSH,
"aruba_procurve": HPProcurveSSH,
"hp_procurve": HPProcurveSSH,
...
CLASS_MAPPER["aruba_procurve_telnet"] = HPProcurveTelnet
CLASS_MAPPER["hp_procurve_telnet"] = HPProcurveTelnet
# Add serial drivers
CLASS_MAPPER["cisco_ios_serial"] = CiscoIosSerial
CLASS_MAPPER["furukawa_fitelnet_serial"] = FurukawaFitelnetSerial
```
Only those two `_serial` entries exist. There is no `aruba_aoscx_telnet`/`aruba_aoscx_serial` entry.

**`ArubaCxSSH`** (whole file is 43 lines) [N1]:
```python
class ArubaCxSSH(CiscoSSHConnection):
    def __init__(self, **kwargs):
        if kwargs.get("default_enter") is None:
            kwargs["default_enter"] = "\r"
        return super().__init__(**kwargs)

    def session_preparation(self) -> None:
        self.ansi_escape_codes = True
        self._test_channel_read(pattern=r"[>#]")
        self.set_base_prompt()
        self.disable_paging(command="no page")

    def check_config_mode(self, check_string: str = "(config)#",
                          pattern: str = r"[>#]", force_regex: bool = False) -> bool:
        return super().check_config_mode(check_string=check_string, pattern=pattern)

    def config_mode(self, config_command: str = "configure term",
                    pattern: str = "", re_flags: int = 0) -> str:
        """Aruba auto completes on space so 'configure' needs fully spelled-out."""
        return super().config_mode(config_command=config_command, pattern=pattern, re_flags=re_flags)
```
Inherited from `CiscoBaseConnection` [N3]:
- `exit_config_mode(exit_config="end", pattern=r"#.*")`
- `save_config(cmd="copy running-config startup-config", confirm=False, confirm_response="")`. It calls `self.enable()` first, then `_send_command_str(..., read_timeout=100.0)`.
- `enable(cmd="enable", pattern="ssword")` and `check_enable_mode(check_string="#")`.
- `cleanup(command="exit")` (calls `exit_config_mode()` if `check_config_mode()` is true, then writes `exit`).
- `serial_login`/`telnet_login` (see section 8).

Consequences for CX:
- `session_preparation` does **not** call `enable()` and does **not** set terminal width. `no page` is the only preparation command.
- **`check_config_mode` only matches the literal string `(config)#`.** Sub-context prompts such as `sw(config-if)#`, `sw(config-vlan-10)#`, `sw(config-lag-if)#` do not contain `(config)#`. `send_config_set()` ends with `exit_config_mode()`, which only sends `end` `if self.check_config_mode()`. If the last command leaves you in a sub-context, Netmiko will not send `end` and the session stays in the sub-context. Likewise `config_mode()` called from a sub-context would send `configure term` and then raise `ValueError("Failed to enter configuration mode.")` if the check still fails. [N1][N2][N3] The base `config_mode` code is in [N2].
  - Workaround: subclass and use `check_string="(config"` (or regex with `force_regex=True`), or always append `end`/`exit` yourself. Whether the device accepts `configure term` from inside a sub-context is **UNVERIFIED (medium)**.
- The default config command is `configure term` (abbreviated). The class docstring says CX auto-completes on space, so abbreviations typed with spaces are risky. [N1]

**`HPProcurveBase` / `HPProcurveSSH`** (203 lines) [N4]:
```python
class HPProcurveBase(CiscoSSHConnection):
    def __init__(self, *args, **kwargs):
        conn_timeout = kwargs.get("conn_timeout")
        kwargs["conn_timeout"] = 20 if conn_timeout is None else conn_timeout
        disabled_algorithms = kwargs.get("disabled_algorithms")
        if disabled_algorithms is None:
            kwargs["disabled_algorithms"] = {"pubkeys": ["rsa-sha2-256", "rsa-sha2-512"]}
        super().__init__(*args, **kwargs)

    def session_preparation(self):
        self.ansi_escape_codes = True
        # ProCurve has an odd behavior where the router prompt can show up
        # before the 'Press any key to continue' message. Read up until the
        # Copyright banner to get past this.
        try: self.read_until_pattern(pattern=r".*opyright", read_timeout=1.3)
        except ReadTimeout: pass
        # Procurve uses 'Press any key to continue'
        try:
            data = self.read_until_pattern(pattern=r"(any key to continue|[>#])", read_timeout=3.0)
            if "any key to continue" in data:
                self.write_channel(self.RETURN)
                self.read_until_pattern(pattern=r"[>#]", read_timeout=3.0)
        except ReadTimeout: pass
        self.set_base_prompt()
        if len(self.base_prompt) >= 25: self.set_base_prompt()
        # ProCurve requires elevated privileges to disable output paging :-(
        self.enable()
        self.set_terminal_width(command="terminal width 511", pattern="terminal")
        command = "no page"
        self.disable_paging(command=command)

    def check_config_mode(self, check_string=")#", pattern=r"[>#]", force_regex=False): ...
    def enable(self, cmd="enable", pattern="password", ...):   # custom: handles a Username: prompt, then Password:
    def cleanup(self, command="logout"):                        # answers "Do you want to log out" with y, "Do you want to save the current" with n
    def save_config(self, cmd="write memory", confirm=False, confirm_response=""):
        return super().save_config(cmd=cmd, confirm=confirm, confirm_response=confirm_response)

class HPProcurveSSH(HPProcurveBase):
    def _get_ssh_client_instance(self):
        """If no SSH keys, no agent, and no password, use noauth."""
        if not self.use_keys and not self.allow_agent and not self.password:
            return SSHClient_noauth()
        return SSHClient()

class HPProcurveTelnet(HPProcurveBase):
    def telnet_login(..., username_pattern=r"(Login Name:|sername:)", pwd_pattern=r"assword", ...)
```
Consequences for AOS-S:
- `config_mode` is inherited (`configure terminal`). The AOS-S 2930 guide shows `configure terminal` followed by `switch(config)# vlan 1` and `switch(vlan-1)#` prompts [S14].
- `check_string=")#"` matches `(config)#`, `(vlan-10)#`, `(eth-1)#`, so sub-contexts are recognized (unlike CX). The manager prompt `switch#` has no `)#`.
- `exit_config_mode` sends `end` (inherited).
- `save_config` calls `enable()` first, then sends `write memory` and reads to the prompt (no confirmation handling).
- The `terminal width 511` line only reads until the echo `terminal`. Whether AOS-S accepts that command is **UNVERIFIED (medium)**; Netmiko does not check the reply.
- `cleanup()` sends `logout`. If AOS-S asks `Do you want to save the current configuration`, Netmiko answers `n`. [N4]
- Netmiko's own comment states paging cannot be disabled without manager privilege, hence `enable()` before `no page`.

### 1.2 AOS-CX login and privilege model

- Three built-in local groups with fixed privileges [K-CX-Roles]:
  - `administrators` (privilege level 15): full CLI and REST; sees sensitive config "such as passwords which are displayed as ciphertext".
  - `operators` (level 1): display-only CLI, "Viewing of nonsensitive switch configuration information", REST GET only.
  - `auditors` (level 19): auditor context (`auditor>`) only.
- The factory `admin` user is in `administrators`, and after login you land in the manager context with prompt `switch#`. The `enable` command "Exits the operator context (>) and enters the manager context (#)" and the listed authority is "Administrators or local user group members with execution rights". It has no password prompt in the command description. [K-CXC][K-CX-Login]
  - Whether an operator-group user can `enable` is **UNVERIFIED (medium)**; the authority column suggests not. A driver should log in as an administrator-group user and treat a `>` prompt as an error.
- `show running-config` as an operator shows "No user information" (users are hidden). [K-CXG]
- Paging: `no page` (session-only, not persistent): "This setting is not persistent and applies to the current session only"; `page <2-1000>`. [K-CXC] Aruba's own Ansible terminal plugin sends `no page`, then (best effort) `terminal width 512` / `terminal width 0`, and warns if width cannot be set. [K-AnsT] Netmiko's CX class does not send a width command.
- Auto-confirm: `auto-confirm` makes the CLI answer `y` to all `(y/n)` prompts; HPE says it resets to disabled on reboot, and recommends REST for automation. [K-CXC] Whether it is per session or global is **UNVERIFIED (medium)**.
- Config mode: `configure terminal` (or `configure`/`config`); `end` returns to manager context; `exit` goes up one level. [K-CXC]
- Prompt format `hostname(config)#`, sub-contexts `hostname(config-if)#` etc. [K-CXG][K-CXC examples]
- Scrapli's community platform for CX uses prompt regex `^[\w.\-@/:]{1,63}#\s?$` and `no page` on open, and `exit` on close. [K-Scrapli]

### 1.3 AOS-S login and privilege model

- Two CLI privilege levels plus config: **Operator** prompt `switch>`; **Manager** prompt `switch#`; Global config `switch(config)#`; context config e.g. `switch(vlan-10)#`, `switch(eth-1)#`. [K-S13b]
- "If no passwords are set when you log on to the CLI, you will enter at the Manager level." With passwords, the level equals the password used. From operator: `switch> enable`, then `Password:`, then `switch#`. [K-S13b]
- Operator can examine config but not change it; manager can configure. [K-S13b]
- RADIUS/TACACS users normally land as operator and must `enable` again; `aaa authentication login privilege-mode` lets RADIUS Service-Type Administrative-user (value 6) land directly in manager. [K-S19]
- Paging: `[no] page` "Toggles the paging mode for show commands between continuous listing and per-page listing". [K-S15] Netmiko comments that elevated privilege is needed to disable paging. [N4]
- Config entry: `configure terminal` [S14], confirmed by Netmiko default and the 2930 guide; `configure`/`config` also appears in [K-S13b].
- `erase startup-config` "operates at any level except the Operator level" [K-S15] (illustrates the operator limit).
- With RADIUS/TACACS "hp-privilege-level" 1 = Operator, 15 = Manager. [K-S19b]

---

## 2. Config-mode errors

### 2.1 AOS-CX
- `% Command incomplete.` is real and begins with `%`. Example from a CX user (copying with an incomplete URL form):
  `SwBrsl021# copy startup-config tftp://192.168.10.22/file1` -> `% Command incomplete.` [K-AH-Incomplete]. Also seen for `erase all` (without `zeroize`) on a 6300. [K-AH-Internal]
  - Practical gotcha from that same thread: CX `copy startup-config tftp://...` requires a trailing `cli` or `json` keyword (see the 10.13 `copy {running-config | startup-config} <REMOTE-URL> {cli | json} [vrf ...]` syntax). [K-CX-copy-remote]
- `Invalid input: <token>` is real and does **not** start with `%`: `6300# systemctl daemon-reload` -> `Invalid input: systemctl`. [K-AH-Internal] The CX Fundamentals guide also says "If the user tries to execute any command that is not allowed, an Invalid input: error message is displayed." [K-CXG]
- Scrapli's community AOS-CX platform lists failure strings `["% Command incomplete.", "Invalid input:", "doesn't exist"]`. [K-Scrapli]
- `Cannot execute command. Internal error.` appears in the forum thread. [K-AH-Internal] `Error: Checkpoint auto timer is already ON` appears for `checkpoint auto` re-entry. [K-AH-ckpt]
- Loading a CLI file with `copy <url> running-config` does not stop on errors: "If a particular CLI command fails, the switch logs the failure and it continues to the next line"; the console prints `Some of the configuration lines from the file were NOT applied. Use 'show events -d hpe-config' for more info.` [K-CXG]
- `% Unknown command.` and `% Ambiguous command.`: **UNVERIFIED (medium)**. I did not find either string in the CX 10.13 guides, the airheads threads I read, or the Scrapli platform file.
- Value out of range: **UNVERIFIED (low)**. Messages are feature-specific; I did not find a documented generic one.
- Do CX errors start with `%` like IOS? Only some do (`% Command incomplete.`). `Invalid input:` and `Error:` do not. A pattern like `^%` alone will miss `Invalid input:`.
- Suggested starting `error_pattern` for CX (must be validated on a device): `r"^(% |Invalid input:|Error:|Cannot execute command|.*doesn't exist)"`.

### 2.2 AOS-S
- `Invalid input: <token>`: verified, no leading `%`. Examples: `Invalid input: 255.255.255.255` when a mask follows `host` in an ACL [K-S15, 16.05 guide ch. "Error (Invalid input) when entering an IP address"]; field misspelled in `show interfaces custom` gives `Invalid input: <input>` [K-S15].
- `Incomplete input: <command>`: verified (`Incomplete input: custom` when the port list is missing in `show interfaces custom`). [K-S15]
- `Module not present for port or invalid port: <input>`: verified. [K-S15]
- Value out of range: in the user-role context a too-large number is reported as `Invalid input: 100000000000000000` (documented example with `reauth-period`). [K-S15] Other contexts may use different text; **UNVERIFIED (medium)**.
- Ambiguous command: **UNVERIFIED (low)**; I found no AOS-S source for an "Ambiguous" message.
- Some commands print warnings and ask `[y/n]`/`(y/n)` mid-config (examples in this report: `include-credentials`, `encrypt-credentials`, `erase startup-config`, `cfg-restore`, `reload after`). These will stall `send_config_set` unless handled. [K-S9][K-S10][K-S15]
- Suggested starting `error_pattern` for AOS-S: `r"^(Invalid input:|Incomplete input:|Module not present for port or invalid port:)"`. Validate on a device.

### 2.3 Netmiko `send_config_set(error_pattern=...)`
Works with both classes. It is implemented in `BaseConnection.send_config_set` and is platform-neutral [N2]:
```python
error_match = re.search(error_pattern, output, flags=re.M)
if error_match:
    msg = f"Invalid input detected at command: {cmd}, matched error: {error_msg}"
    raise ConfigInvalidException(msg)
```
Notes:
- The regex runs over the cumulative `output` (including any earlier commands), with `re.M`, so anchor with `^`.
- With `cmd_verify=True` (default) it waits for the command echo and then the prompt (`base_prompt` or `terminator`). A command that raises a `(y/n)` prompt will hang until `read_timeout`.
- `bypass_commands` default `r"^banner .*$"` disables cmd_verify when a banner line is sent. [N2]
- Netmiko stops at the first match, but the device has already applied earlier lines (no atomic transaction on either platform).

---

## 3. Running-config format

### 3.1 AOS-CX
Facts:
- `show running-config` prints `Current configuration:` then `!Version AOS-CX <ver>` and `!export-password: default`, comment/separator lines starting with `!`, then global commands and contexts. [K-CXG] A device-generated sample from a 10.04 switch (HPE sample config) shows the structure below. [K-CX-DCN]
- **Indentation: 4 spaces per level, no `exit` lines** (context ends when the next top-level line starts). Observed in the HPE DCN sample [K-CX-DCN]. Newer releases: **UNVERIFIED (medium)**.
- Names with spaces are **not quoted** in that sample: `vlan 11` / `    name VLAN 11`; `description VSX ISL`. Newer releases: **UNVERIFIED (medium)**. [K-CX-DCN]
- An interface line can carry a trailing space (`interface 1/1/31 `) in that sample, so trim when diffing. [K-CX-DCN]
- Comments inside contexts are `!`-prefixed and indented (`    !interface group 1 contains ports 1/1/1-1/1/12`). [K-CX-DCN]
- Interface naming: physical `member/slot/port` e.g. `1/1/1` (VSF/stack members 1..n in the first number); `interface lag 11` (also `interface lag 11 multi-chassis`); `interface vlan 10`; `interface loopback 0`; `interface mgmt`. [K-CX-DCN][K-CXC]
- Ranges: the CLI supports range contexts like `interface 1/1/1-1/1/4`. [K-CXG] (rules at "Support for range contexts").
- `show running-config` shows only non-default config; `show running-config all` adds defaults (e.g. `snmp-server community public` and `snmp-server agent-port 161` only appear with `all`). [K-CXG]
- Users: `user admin group administrators password ciphertext AQBap...` [K-CX-DCN].

Composite excerpt (made-up values; structure from [K-CX-DCN], command syntax from [K-CXS]/[K-CXG]/[K-SNMP-CX]):
```
!
!Version AOS-CX FL.10.13.1000
!export-password: default
hostname sw-lab-01
user admin group administrators password ciphertext AQBapEXAMPLEEXAMPLEEXAMPLE==
user ops1 group operators password ciphertext AQBapEXAMPLE2EXAMPLE2==
!
ntp server 192.0.2.10 minpoll 4 maxpoll 4 iburst
ntp enable
!
radius-server host 192.0.2.20 key ciphertext AQBapEXAMPLE3EXAMPLE3==
tacacs-server host 192.0.2.21 key ciphertext AQBapEXAMPLE4EXAMPLE4==
aaa authentication login default group radius local
!
snmp-server vrf default
snmp-server community lab-ro
snmpv3 user monitor auth sha auth-pass ciphertext AQBapEXAMPLE5== priv aes priv-pass ciphertext AQBapEXAMPLE6==
!
ssh server vrf mgmt
!
vlan 1
vlan 10
    name Users
vlan 20
    name Voice VLAN
spanning-tree
interface mgmt
    no shutdown
    ip static 192.0.2.50/24
    default-gateway 192.0.2.1
interface lag 1
    no shutdown
    description Uplink to core
    no routing
    vlan trunk native 1
    vlan trunk allowed 10,20
    lacp mode active
interface 1/1/1
    no shutdown
    description User port
    no routing
    vlan access 10
interface 1/1/49
    no shutdown
    lag 1
interface vlan 10
    ip address 10.10.10.1/24
https-server vrf mgmt
```
Notes on this excerpt: the exact on-device rendering of `aaa authentication login default group radius local`, `snmpv3 user ... ciphertext` and `tacacs-server host ... key ciphertext` lines is **UNVERIFIED (medium)** (syntax is documented; the printed form is inferred, and the radius ciphertext form is shown in HPE examples [K-CXS]). The `ssh server vrf mgmt` / `https-server vrf mgmt` lines are from [K-CX-DCN]. The `ntp server ... iburst` / `ntp enable` lines are from a hand-written lab config [K-CX-Clab] and the documented syntax [K-CXG].

### 3.2 AOS-S
Facts:
- Header: `Running configuration:` then `; J9850A Configuration Editor; Created on release #KB.16.06.0006` and `; Ver #13:4f.f8...` (two comment lines starting with `;`). With `encrypt-credentials` there is also `; encrypt-cred <base64>`. [K-S21][K-S10]
- **Contexts end with `exit`.** `vlan`, `interface`, `oobm`, `spanning-tree` blocks etc. [K-S21][K-S10]
- **Indentation: 3 spaces** in a community dump of a 5406R config [K-S21]; the HPE guides render ~5 spaces because of page layout. I treat 3 spaces as the device format: **UNVERIFIED (medium)** (the HPE PDF text extraction cannot show true whitespace).
- **Names are double-quoted**: `hostname "Switch-5406Rzl2"`, `name "DEFAULT_VLAN"`, `snmp-server community "public" unrestricted`, `aaa authentication local-user "dol" group "Level-15"`. A name with spaces is therefore inside the quotes. [K-S10][K-S21]
- Port naming: single switch `1`..`48`; chassis/modular slots `A1`, `F24`; stack/VSF `member/port` e.g. `1/1`, `2/27`; trunks `Trk1`; lists `A1-A24,F1-F24`; in status tables a trunk member shows as `1/28-Trk1` or `A24-Trk1`. [K-ntc-hp][K-S21]
- Trunk/LAG: created with `trunk A1,B1 trk1 lacp` (global command), referenced as `Trk1` in `vlan`/`interface` lines. [K-S21][K-S14 (trunk <port-list> <trk1...trk60> {<trunk|lacp>})]
- Stack member line: `stacking member 1 type "JL320A" mac-address 941882-dccf00` ... `exit`. [K-S14]
- Time: `timesync ntp`, `ntp unicast`, `ntp server 10.11.3.2`, `ntp enable`, `time timezone -300`. [K-S21]
- In the factory-default state a bare `password manager` / `password operator` line (no hash) indicates a configured password; the hash appears only with `include-credentials`. [K-S12][K-S9]

Composite excerpt (made-up values; structure from [K-S21], [K-S10], [K-S12], [K-S14]):
```
Running configuration:

; JL255A Configuration Editor; Created on release #WC.16.10.0010C
; Ver #14:67.6f.f8.1d.9b.3f.bf.bb.ef.7c.59.fc.6b.fb.9f.fc.ff.ff.37.ef:44
hostname "sw-lab-02"
module 1 type jl255a
trunk 25-26 trk1 lacp
timesync ntp
ntp unicast
ntp server 192.0.2.10 iburst
ntp enable
time timezone -300
snmp-server community "lab-ro" operator
snmp-server host 192.0.2.30 community "lab-ro"
radius-server host 192.0.2.20
tacacs-server host 192.0.2.21
aaa authentication ssh login radius local
aaa authentication local-user "ops1" group "Level-1"
oobm
   ip address 192.0.2.60 255.255.255.0
   exit
vlan 1
   name "DEFAULT_VLAN"
   no untagged 1-12
   exit
vlan 10
   name "Users"
   untagged 1-12
   tagged Trk1
   ip address 10.10.10.1 255.255.255.0
   exit
vlan 20
   name "Voice VLAN"
   tagged Trk1
   voice
   exit
interface 1
   name "User port"
   exit
spanning-tree
password manager
password operator
```
Notes: lines `snmp-server host ... community`, `aaa authentication ssh login radius local`, `aaa authentication local-user ... group "Level-1"` are my composite of documented syntax [K-S12][K-S21]; the printed form of the `radius-server host` line with `include-credentials` off is **UNVERIFIED (medium)**. `snmp-server community "public" unrestricted` is the documented default rendering; whether the keyword is `operator`/`unrestricted` depends on the restriction setting. [K-S14]

---

## 4. Secret-bearing config lines

### 4.1 AOS-CX
Core rules:
- Passwords/keys are stored as **ciphertext** that only an AOS-CX switch can produce (Base64, begins `AQBap...`). Administrators "cannot construct ciphertext passwords"; ciphertext is copied from `show running-config` of another switch and works only if "The target switch must have the same export password (default or otherwise) as the source switch." [K-CXS-user]
- `service export-password` sets a non-default export password; "All factory-default switches have identical default export passwords"; "Only switches with identical export passwords can exchange configuration information." The header line `!export-password: default` shows the default. [K-CXS-exp][K-CXG]
- **Plaintext** forms are accepted on input (`password plaintext X`, `key plaintext X`). They are stored encrypted thereafter. "Command history does show the password as cleartext." [K-CXS-user] Do not log them.
- Ciphertext user passwords are rejected when password complexity is enabled ("Ciphertext passwords cannot be used when password complexity is enabled"). [K-CXS]
- Administrators see ciphertext; operators see no user information. [K-CX-Roles][K-CXG]
- Entering the command with no key prompts for plaintext twice, masked with `*` (interactive). [K-CXS-rad][K-CXS-tac]

Lines that carry secrets (syntax documented in [K-CXS], [K-CXG], [K-SNMP-CX]):
| Feature | Line (running-config form) | Notes |
|---|---|---|
| Local user | `user NAME group {administrators|operators|auditors|GROUP} password ciphertext <b64>` | [K-CXS-user] |
| RADIUS per-server | `radius-server host <h> key ciphertext <b64> [timeout..][vrf ..]` | [K-CXS-rad] shows `key ciphertext AQBapStbgHt1...` |
| RADIUS global | `radius-server key ciphertext <b64>` | [K-CXS] |
| RADIUS tracking, dyn-auth | `radius-server tracking user-name X password ciphertext ..`, `radius dyn-authorization client <h> secret-key ciphertext ..` | [K-CXS] |
| RADIUS+IPsec | `radius-server host <h> ... secure ipsec authentication spi ...` ("IPsec key is shown in an exportable ciphertext format") | [K-CXC] |
| TACACS+ | `tacacs-server key ciphertext <b64>`; per-server `tacacs-server host <h> key ...` | [K-CXS-tac], global-vs-per-server rule in [K-CXS] |
| SNMP v1/v2c | `snmp-server community NAME` (plaintext; no ciphertext form) | default community `public` appears only in `show running-config all`. [K-CXG][K-CX-Hard-SNMP] |
| SNMPv3 | `snmpv3 user NAME auth sha auth-pass {plaintext|ciphertext} .. priv aes priv-pass {plaintext|ciphertext} ..` ("ciphertext is used when copying user configuration between switches") | [K-CX-Hard-SNMP][K-SNMP-CX]; printed running-config form **UNVERIFIED (high)** |
| NTP | `ntp authentication-key <id> {md5|sha1} {<plain> [trusted] | ciphertext <b64>}` ("ciphertext ... used to restore the NTP authentication key when copying configuration files between switches") | [K-CX-NTP] |
| OSPF | `ip ospf authentication-key ciphertext ..`, `ip ospf message-digest-key N md5 ciphertext ..`, `ip ospf sha-key N sha ciphertext ..` | [K-CXI] (IP routing guide) |
| Key chains | `keychain NAME key 1` / `key-string ciphertext ..` | [K-CXI] |
| BGP | `neighbor {ip|group} password {ciphertext|plaintext} ..` | [K-CX-BGP] |
| VRRP | `authentication md5 ciphertext ..` | [K-CXI] |
| 802.1X/MAC auth | `aaa authentication port-access mac-auth password {plaintext|ciphertext} ..`, `eap-identity password ciphertext ..` | [K-CXS] |
| EST/PKI | `username X password ciphertext ..` in EST profile; trust-anchor public certs print inline (`crypto pki ta-profile ... ta-certificate -----BEGIN CERTIFICATE-----`) | public certs only; private keys not seen in running-config: **UNVERIFIED (medium)** |
| MACsec, UBT | `cak ciphertext ..`, `papi-security-key ciphertext ..` | [K-CXS] |
| IKE/IPsec site-to-site | not researched: **UNVERIFIED (low)** | |

Backup/restore implication: a CX text backup contains ciphertext that is restorable on the same switch (or a switch with the same export password). It is not restorable on a different export password, and cannot be generated by the tool. `copy <url> running-config` of a CLI file applies line by line and does not clear the running config. [K-CXG]

### 4.2 AOS-S
Core rules:
- **Default: credentials are not in the running config.** "Default: The security credentials ... are not stored in the running configuration." They live in internal flash. [K-S9]
- `include-credentials [radius-tacacs-only|store-in-config]` moves manager/operator usernames+passwords, RADIUS shared secret keys, SNMP and 802.1X security credentials and SSH client public keys into the config file (so they show in `show running-config` / `write terminal` and are copied by `copy running-config tftp|sftp`). The `no` form "disables only the display and copying". [K-S9][K-S12]
  - Enabling prints a caution and `Continue?[y/n]`, affects *all* stored configurations, makes credentials unreadable by older software, and needs `write memory`. "Erasing configurations with 'include-credentials' enabled will erase stored passwords and security credentials. The system will reboot with the factory default configuration." Booting a startup-config that contains `include-credentials` ignores and erases credentials kept in flash. [K-S12]
- `encrypt-credentials` (config context, manager) encrypts passwords and keys shown in `show` output/config ("Encrypts all passwords and authentication keys in show commands"). It prints a CAUTION asking `Save config and continue (y/n)?`; the encrypted config "cannot be used by older software versions" and may prevent older versions from booting. `show encrypt-credentials` shows `Encryption : Enabled` and `Pre-shared Key: none`. [K-S10]
  - Custom key: `encrypt-credentials pre-shared-key plaintext <key>`; otherwise a built-in key common to all AOS-S switches is used. Secondary source only: Aruba Central doc/KB-style article via search summary; **UNVERIFIED (medium)**. (The pre-shared key syntax shown in the search summary: `encrypt-credentials pre-shared-key plaintext encryptme`.)
  - `hide-sensitive-data` is a separate command seen in the same example; it hides keys in show output. [K-S10]
- SNMPv3 credentials are saved hashed with the **switch engine ID** and "cannot be used after downloading the file on a different switch". [K-S12]

Lines that carry secrets:
| Feature | Line form | Notes |
|---|---|---|
| Manager/operator | `password manager user-name "manager" sha1 "<40 hex>"` (with include-credentials); bare `password manager` otherwise | [K-S14 (3810M example)][K-S12]. SHA-256 variants exist in newer releases: **UNVERIFIED (medium)** |
| Local user (RBAC) | `aaa authentication local-user "name" group "Level-15" [password ...]` | group line is visible [K-S21]; the password hash form: **UNVERIFIED (medium)** |
| SNMP v1/v2c | `snmp-server community "public" unrestricted` | always shown, plaintext, even without include-credentials. [K-S11][K-S12] |
| SNMPv3 | `snmpv3 user NAME auth sha "<hash>" priv "<hash>"` | only with include-credentials; hashed + engine-ID bound. [K-S11][K-S12] |
| TACACS+ | `tacacs-server key "<key>"`, `tacacs-server host <ip> key "<key>"`; with encrypt-credentials `tacacs-server encrypted-key "<b64>"`, `tacacs-server host <ip> encrypted-key "<b64>"` | [K-S10][K-S12] |
| RADIUS | `radius-server host <ip> key "<key>"` (cmd form `radius-server host 10.73.4.136 key "secret"` [K-S15]); encrypted analog `encrypted-key "<b64>"` | printed encrypted-key form for RADIUS: **UNVERIFIED (medium)** |
| SNTP/NTP auth | `sntp authentication key-id N authentication-mode md5 key-value <key> [trusted]` or `encrypted-key <<key-string>>` (base64 AES-256); `ntp authentication key-id ...` | [K-S14]; whether key-value prints in clear without include-credentials: **UNVERIFIED (medium)** |
| 802.1X | `password port-access user-name ... ` | [K-S12] |
| SSH client keys | stored per config file when store-in-config | [K-S12] |
| OSPF/BGP auth | not researched on AOS-S: **UNVERIFIED (low)** | |
| IPsec/IKE | not researched on AOS-S: **UNVERIFIED (low)** | |
| Certificates | managed via `crypto pki ...`; deleting uses `crypto pki clear certificate-name ...` | [K-S13 ASG 2930 16.10 p.~24103]; running-config contents **UNVERIFIED (medium)** |

Backup/restore implication (AOS-S): With include-credentials off, a running-config text backup (and a `cfg-backup` file, which is a copy of the config) lacks the credentials. The HPE rollback procedure says "If the configuration involves any sensitive information, backup and restore the configuration by enabling the `include-credentials` command." [K-S5] What `cfg-restore` does with a file that lacks credential lines is **UNVERIFIED (medium)** (likely leaves flash-stored creds untouched). Do not let the tool toggle include-credentials/encrypt-credentials as a side effect of rollback: both are config-wide, interactive, and have downgrade/boot-failure caveats.

---

## 5. Save

- **AOS-CX:** `write memory` ("an alias of the command `copy running-config startup-config`"; "overwrites the startup configuration") prints `Success`. No confirmation prompt. [K-CXG] Netmiko `save_config()` sends `copy running-config startup-config` with `confirm=False`. [N3] Startup-config is also a checkpoint-style object (`startup-config`). [K-CXG]
- **AOS-S:** `write memory` (manager/config context) copies running-config to startup-config. No prompt in the normal case; it prints a warning only in redundant-management cases ("The next reboot or failover is set to boot from a different software image..."). [K-S13b]. Success output text: **UNVERIFIED (medium)**.
- AOS-S prompts you must handle elsewhere: `boot` / `reload` ask to save unsaved config; `reload at|after` never ask and silently drop unsaved changes [K-S6][K-S13b]; `erase startup-config` asks `Configuration will be deleted and device rebooted, continue [y/n]?` [K-S13b].
- AOS-CX prompts: `boot system` asks `Do you want to save the current configuration (y/n)?` and a reboot confirmation. [K-CXG]

---

## 6. Rollback options (most important)

### 6.1 AOS-CX checkpoints (all in Manager `#` context, authority administrators)
| Task | Exact command | Min. doc version | Source |
|---|---|---|---|
| Create named checkpoint from running | `copy running-config checkpoint <NAME>` (replies `Success`) | "10.07 or earlier" | [K-CX-cp-create] |
| Restore checkpoint to running | `copy checkpoint <NAME> running-config` (replies `Success`) | "10.07 or earlier" | [K-CX-cp-restore] |
| Same (rollback form) | `checkpoint rollback {<NAME> | startup-config}` | "10.07 or earlier" | [K-CX-cp-rollback] |
| Restore checkpoint to startup | `copy checkpoint <NAME> startup-config` | "10.07 or earlier" | [K-CX-cp-restore] |
| Delete | `erase checkpoint <NAME>` / `erase checkpoint all` | "10.07 or earlier" | [K-CX-cp-erase] |
| List / show / diff | `show checkpoint`, `show checkpoint <NAME>`, `show checkpoint date`, `checkpoint diff <A> <B>` | "10.07 or earlier" | [K-CXG][K-CX-cp-diff] |
| Rename | `checkpoint rename <OLD> <NEW>` | "10.07 or earlier" | [K-CXG] |
| Auto checkpoint with timer | `checkpoint auto <1-60>` then `checkpoint auto confirm` | "10.07 or earlier" | [K-CX-auto][K-CX-auto-confirm] |

Details:
- Name rules: alphanumeric, `_`, `-`, **32 characters or fewer**, must not start with `CPC` (reserved for system checkpoints). Limits: 64 total checkpoints (including startup), 32 user, 32 system. [K-CX-cp-create][K-CXG]
- System checkpoints: created automatically (enabled by default) `CPC<YYYYMMDDHHMMSS>` after a quiet period of 300 s (configurable 5-600 s via `checkpoint post-configuration timeout`); the newest replaces the oldest at 32. They can be applied with `checkpoint rollback` or `copy`. [K-CX-cp-post][K-CXG]
- "All previous configurations are lost after the execution of this command" (rollback replaces running config). Checkpoints "can be applied to other switches of the same platform". [K-CXG]
- Cross-version caution: a 2018 HPE community post says "checkpoints created with one software version may not work properly on another"; the doc has no such statement. [K-AH-ckpt-ov] (secondary source)
- **`checkpoint auto <minutes>`**: "Starts auto checkpoint mode. In auto checkpoint mode, the switch temporarily saves the runtime configuration as a checkpoint only for the specified time lapse interval." Range 1 to 60 minutes. Example transcript: `checkpoint auto 20` -> `Auto checkpoint mode expires in 20 minute(s)`; later `WARNING Please "checkpoint auto confirm" within 2 minutes`. [K-CX-auto]
  - Confirm: `checkpoint auto confirm` -> `checkpoint AUTO20170801011154 created` ("AUTO<YYYYMMDDHHMMSS>"). It "also ends the auto checkpoint mode". [K-CX-auto][K-CX-auto-confirm]
  - **Expiry**: "If the checkpoint auto confirm command is not entered during the specified time lapse interval, the previous runtime configuration is restored." The device prints `WARNING: Restoring configuration. Do NOT add any new configuration.` then `Restoration successful`. [K-CX-auto] Whether `confirm` also writes `startup-config` is not stated: **UNVERIFIED (medium)**; run `write memory` after confirming.
  - Not the same as `checkpoint confirm` (a blog claims that; the HPE docs say `checkpoint auto confirm`). [K-CX-auto-confirm]
  - Reliability reports (secondary, older releases): on 8320 10.04.0001 one user found that if the SSH session ended before the timer expired, the timer stayed "stuck on" and the config was not restored, and `checkpoint auto 3` then failed with `Error: Checkpoint auto timer is already ON`; another user could not reproduce and confirmed the revert worked after a disconnect. [K-AH-ckpt] Treat session-loss rollback as needing a lab test on your target release. Also no documented way to show remaining time. [K-AH-ckpt2]
- **Non-disruptive?** Not stated as a guarantee. The documented transcripts show the CLI staying up with `Restoration successful`; no reboot is mentioned anywhere in the checkpoint docs. The restore re-applies the stored configuration to the running config in place, so objects that differ will change live (interface flaps, address changes, losing the management path if the old config lacked it). **UNVERIFIED (medium)** as a statement about service impact.
- **Do not use `copy <url|usb> running-config` as rollback.** A CLI-format file "does not clear the running configuration before applying the CLI commands. All of the CLI commands in the file are applied line-by-line" (additive merge). JSON/checkpoint-format files are the replace-style path. [K-CXG]
- Job scheduler alternative: jobs cannot contain `copy`, `repeat`, `show boot-history`, `show core-dump`, `show events`, `show job`, `show tech`, `sleep`, `terminal-monitor`; `(y/n)` prompts are auto-confirmed. There is no CX `reload after`/`reload cancel` in the 10.14 Job Scheduler guide. [K-CX-job] (A CLI Bank page for a CX `reload` exists in search results but I could not read it: **UNVERIFIED (low)**.)
- **Recommended CX flow** (Manager context, not config mode): `checkpoint auto 5` -> push config (config mode) -> verify from an independent path -> `checkpoint auto confirm` -> `write memory`. Optionally take `copy running-config checkpoint pre_<ts>` first so you can `checkpoint rollback pre_<ts>` explicitly if verification fails while still connected.

### 6.2 AOS-S
**Yes, there is a no-reboot restore from flash: `cfg-backup` + `cfg-restore` (ArubaOS-Switch 16.05 and later).**
- Versions/platforms: "Starting with the 16.05 software release, a switch configuration can be backed up and restored without requiring a reboot": 2530, 2540, 2920, 2930M/F, 3810M, 5400R. [K-S8]
- Backup: `cfg-backup {running-config | startup-config} config <FILE-NAME>` in config context; writes a named config file in internal flash. Up to five config files including the startup config ("maximum number of backup files ... increased from three to five"; fewer visible after a firmware downgrade). List with `show config files` (details: `show config files details`). [K-S2][K-S8][K-S15][K-S3]
- Restore: `cfg-restore {flash | tftp <IP> | sftp <IP>} <FILE-NAME> [diff | force | non-blocking | recovery-mode | verbose]`, context `config`. "Restores the given configuration as the running configuration without reboot." Works on **flash files** and on TFTP/SFTP files. [K-S1]
  - It asks `Current running-configuration will be replaced with '<name>'. Continue (y/n)?` and prints `Configuration restore is in progress, configuration changes are temporarily disabled.` then `Successfully applied configuration '<name>' to running configuration.` [K-S4][K-S8]
  - While running, "configuration changes from other sources (including other CLI sessions, the Web UI, SNMP, and REST) are blocked, as are show commands that display either the running or startup configuration." [K-S8]
  - `diff` previews changes (configuration delete list / add list). `recovery-mode` is enabled by default and retains the current running config if the restore fails. `show cfg-restore status` reports `Status : Success|Failed`, `Failure Reason`, numbers of add/remove commands; `show cfg-restore latest-diff`; `show hash`. [K-S1][K-S3][K-S15]
  - **Fails if a reboot is required**: error `Configuration restoration is not allowed as the configuration has reboot required commands`; with `force` the switch applies the config and **reboots**, after which the backup becomes the active config. Reboot-required commands include `secure-mode`, `max-vlans`, `no allow-v2-modules`, `qinq`, `vsf member/remove`, console settings (`console terminal`, `console baud-rate`, `flow-control`), `qos queue-config`, `access-list grouping`, module/stack provisioning. [K-S4][K-S8][K-S15]
  - Stacks/VSF: cannot use it to add/remove members or modules. [K-S8]
- **Copy a config file to running:** `copy tftp running-config <ip> <file> [pc|unix]` exists; the docs do not say it avoids a reboot or whether it merges or replaces. Do not rely on it: **UNVERIFIED (low)**. [K-S16]
- **`reload after` / `reload at`:** `reload [after <[[DD:]HH:]MM> | at <HH:MM[:SS]> [<MM/DD[/[YY]YY]>]] [system]`. Cancel with `no reload [after | at] ...`; inspect with `show reload after` / `show reload at`. "The `reload at/after` versions of this command do not display a prompt to save configuration file changes: the changes are lost on the scheduled reload." Example `reload after 15`. Scheduling prints `Continue (y/n)?` (seen with the `system` variant). **This is a reboot** (warm reboot, "skips the Power on Self Test"). [K-S6][K-S13b]
- **Job scheduler:** `job <NAME> delay [[DD:]HH:]MM <command> count <1-1000>`, `job <NAME> at ...`, `no job <NAME>`, `show job <NAME>`. "All commands run with manager privilege in configuration context." Default repeat is infinite unless `count` is set. [K-S7]
- **HPE's own automatic-rollback recipe (no reboot)** [K-S5][K-S8]:
```
alias "cfg_rollback" "cfg-restore flash stable"
cfg-backup running-config config stable
job "cfg_stable" delay 00:00:15 "cfg_rollback" count 1      ! 00:00:15 = 15 minutes (DD:HH:MM)
... make changes ...
no job cfg_stable                                            ! cancel when you're sure
show job cfg_stable / show cfg-restore status                ! verify
```
  HPE: "If the user loses connectivity after applying the new configuration, the job scheduler executes the job after the 15-minute timer expires, and 'stable' configuration is restored." [K-S5] (The job's confirmation prompt did not block it in HPE's transcript.)
- **Recommendation for AOS-S:**
  1. Primary: `cfg-backup running-config config <name>` -> alias + `job ... delay <N> ... count 1` -> apply -> verify out-of-band -> `no job <name>` (then `no alias`, `erase config <name>`, and `write memory` only after confirmation). It is **not reboot-based** unless the diff contains reboot-required commands.
  2. Pre-check: if the change set (or the diff `cfg-restore ... diff`) includes reboot-required commands, the job's `cfg-restore` will fail without `force`, i.e. there would be **no rollback at all**. In that case use the fallback below or refuse unattended changes.
  3. Fallback (**plainly reboot-based**): leave the startup-config unsaved/known-good and schedule `reload after <N>` before changing; cancel with `no reload after`. At expiry the switch reboots into the startup-config (outage of a full reboot; longer on 5400R). Do not `write memory` until verified.
  4. Granularity is minutes (format `[[DD:]HH:]MM`), so choose N with margin (verification time + reconnect time).
  5. The HPE docs have no commit-confirm and no automatic checkpoint on AOS-S.

---

## 7. Facts (model, version, serial, uptime)

### 7.1 AOS-CX
- `show version` [K-CX-ver]:
```
-----------------------------------------------------------------------------
AOS-CX
(c) Copyright 2017-2022 Hewlett Packard Enterprise Development LP
-----------------------------------------------------------------------------
Version      : ML.xx.xx.xxxx
Build Date   : 2022-05-27 17:00:46 PDT
Build ID     : AOS-CX:ML.xx.xx.xxxx:85c3c2f3d59e:201910222335
Build SHA    : 85c3c2f3d59ec8318ba97178fad387aecb671b33
Active Image : secondary
Service OS Version : ML.01.05.0001
BIOS Version : ML.01.0001
```
  Version prefix by platform: ML = 6200, FL = 6300/6400, PL = 6100/6000, XL = 8400 (a release-doc table; 6200/6300/6100/8400 from [K-CX-ver]). Regex: `^Version\s*:\s*(\S+)`.
- `show system` (real capture from a 6300F, values changed) [K-ntc-cx]:
```
Hostname           : 6300
System Description : FL.10.11.1021
System Contact     :
System Location    :

Vendor             : Aruba
Product Name       : JL665A 6300F 48G CL4 PoE 4SFP56 Sw
Chassis Serial Nbr : XXXXXXXXXX
Base MAC Address   : 0a00a0-00a000
ArubaOS-CX Version : FL.10.11.1021

Time Zone          : UTC

Up Time            : 12 minutes
CPU Util (%)       : 15
Memory Usage (%)   : 26
```
  Other captures: `Up Time : 2 weeks, 22 hours under a minute` [K-ntc-cx]; doc: `Up Time : 1 week, 5 hours, 28 minutes`; the doc spells the version key `AOS-CX Version`, the capture `ArubaOS-CX Version`, so use `^(?:ArubaOS|AOS)-CX Version\s*:\s*(\S+)`. 10.12+ adds `CPU Util (% avg 1 min)` / `(% avg 5 min)`. [K-CX-sys]
  Regexes: `^Product Name\s*:\s*(\S+)\s+(.*)$` (part number JLxxxA then description), `^Chassis Serial Nbr\s*:\s*(\S+)`, `^Hostname\s*:\s*(\S+)`, `^Up Time\s*:\s*(.+)$` (free text; parse weeks/days/hours/minutes, "under a minute").
- `show module` (real capture, 4-member stack; serials changed) [K-ntc-cx], table in [K-CX-mod]:
```
Management Modules
==================

     Product                                        Serial
Name Number  Description                            Number     Status
---- ------- -------------------------------------- ---------- ----------------
1/1  JL658A  6300M 24SFP+ 4SFP56 Swch               SG1ABCDEF1 Active (local)
2/1  JL661A  6300M 48G CL4 PoE 4SFP56 Swch          SG2ABCDEF2 Ready
...
Line Modules
============
...
```
  Row regex: `^(\d+/\d+)\s+(J[A-Z]\d{3}[A-Z])\s+(.+?)\s{2,}(\S+)\s+(.+)$`. For stacks use the "Management Modules" rows (VSF members) for member serials.

### 7.2 AOS-S
- `show system information` (or `show system`): two-column layout, labels on the same line, so use `re.search` not `re.match`. Real capture from a 2530/K train (values changed) [K-ntc-hp]:
```
 Status and Counters - General System Information

  System Name        : HP_Procurve-01
  System Contact     : John Smith
  System Location    : Downtown DC2

  MAC Age Time (sec) : 300

  Time Zone          : -480
  Daylight Time Rule : Continental-US-and-Canada


  Software revision  : K.15.06.0017         Base MAC Addr      : 7446a0-41a56f
  ROM Version        : K.15.29              Serial Number      : AB12CDE3FG
  Allow V1 Modules   : Yes

  Up Time            : 15 days              Memory   - Total   : 128,380,928
  CPU Util (%)       : 22                              Free    : 61,999,280
```
  Newer single-column variant (2930F VSF/stack, per-member blocks `VSF-Member :1` ... with their own `Serial Number`, `Up Time`, `ROM Version`) [K-ntc-hp][K-S14]. Uptime forms: `51 secs`, `34 mins`, `23 hours`, `15 days`, `458 days`. [K-S14][K-S17b]
  Regexes: `Software revision\s*:\s*(\S+)`, `Serial Number\s*:\s*(\S+)`, `Base MAC Addr\s*:\s*(\S+)`, `ROM Version\s*:\s*(\S+)`, `Up Time\s*:\s*(\d+ \w+)`, `System Name\s*:\s*(.*?)\s*$`. Model is **not** in this output.
- `show modules` gives the model and chassis serial [K-S18][K-S17b]:
```
Status and Counters - Module Information
   Chassis: 3800-24G-PoE+-2SFP+ J9573A            Serial Number: xxxxxxxxxx
   Slot  Module Description                           Serial Number Status
   ...
```
  and for 5400R: `Chassis: 5406Rzl2 J9850A   Serial Number: SG4BG491BL   Allow V2 Modules: No` then a module table with `1/MM1 J9827A Management Module 5400Rzl2 SG4BG4C0C0 Active ...`. Regex: `Chassis:\s*(.+?)\s+(J[A-Z0-9]{4})\s+Serial Number:\s*(\S+)`. Whether non-chassis models (2530/2540/2930) print exactly this "Chassis:" line: **UNVERIFIED (medium)**.
- `show version` [K-S17b]:
```
Image stamp:
/ws/swbuildm/rel_orlando_qaoff/code/build/tam(swbuildm_rel_orlando_qaoff_rel_orlando)
                Nov 19 2014 15:08:22
                KA.15.16.0005
                333
Boot Image:     Primary
```
  On redundant-management chassis it is prefixed by `Management Module 1: Active`. [K-S13b] Regex for the version line: `^\s+([A-Z]{1,2}\.\d{2}\.\d{2}\.\d{4}\w*)\s*$`. `show flash` lists Primary/Secondary versions and `Default Boot`. [K-S13b]
- Caution: the ntc-templates `hp_procurve_show_version` fixture is actually an Aruba *Instant/ArubaOS 8* output, not AOS-S; do not use it as a pattern source. [K-ntc-hp]

---

## 8. Console / day-0

### 8.1 AOS-CX
- Default console: **115200 bps, 8 data bits, 1 stop bit, no parity, no flow control**. `console baud-rate {9600|115200}` exists since 10.08; "The no form ... resets the console port speed to its default of 115200 bps"; changing it is immediate and prompts re-login. [K-CX-baud] HPE KB (older) says AOS-CX baud is fixed at 115200. [K-S-KB-baud]
- Factory login: user `admin`, **empty password**. "The user is prompted to create a password before access is given to the CLI, Web UI, and REST API". Prompt text:
```
switch login: admin
Password:
Please configure the 'admin' user account password.
Enter new password: ***********
Confirm new password: ***********
switch#
```
  Up to 32 characters; blank passwords are technically allowed but discouraged. [K-CX-Login][K-CX-Hard-Factory][K-CX-defadm]
- Ready indicator: the manager prompt `<hostname>#` after login. Before that: boot log, then `login:`. The Service OS (bootloader/recovery) has its own `admin` user and CLI (`SVOS>`). [K-CX-defadm][K-CXC]
- Factory reset: `erase all zeroize` (several minutes to an hour; reboot to ServiceOS) [K-CX-Hard-Factory]; after it the forced-password dialog returns.
- Other day-0 behaviors: management interface defaults to DHCP; SSH on 22 and HTTPS/REST on 443 are enabled by default (VRF scope depends on model). [K-CX-Hard-Factory]

### 8.2 AOS-S
- Boot messages are at **9600 baud only**; afterwards the console **speed-senses** up to 115200. The `console baud-rate` setting defaults to `speed-sense`; flow control default **XON/XOFF**; terminal VT100; 8N1. A fixed baud/flow-control change needs `write memory` and a reboot. [K-S-KB-baud][K-S13b show console]
- HPE guidance: emulator DEC VT100 (ANSI), 8 data bits, 1 stop bit, no parity, flow control off (2930F connection note, secondary). [K-S-2930F-console, search summary only; **UNVERIFIED (medium)**] -> given the default XON/XOFF, test with `xonxoff=True` vs False.
- Login banner then `Press any key to continue`: "the switch displays the banner above the prompts for local password and Press any key to continue. Entering a correct password or, if no password is configured, pressing any key clears the banner". Netmiko handles this on SSH/telnet-style flows. [K-S13b][N4]
- Factory default: no IP, no passwords, console only. From **16.10.0009**, "factory default switches prompt you to configure the manager password at the first login" (exact prompt text **UNVERIFIED**; a different, related prompt appears after a cold reboot in enhanced secure mode: `New Manager password:` / `Retype password:` / `New Operator password:`). [K-S13][K-S-first-login]
- Prompt: `<hostname>#` (manager), `<hostname>>` (operator). Default hostname is model-derived: **UNVERIFIED (medium)**.
- Factory reset: `erase startup-config` (prompt `Configuration will be deleted and device rebooted, continue [y/n]?`), `erase all zeroize`, ROM console `erase-all zeroize`, or Reset+Clear buttons. [K-S13b][K-S13]

### 8.3 Does `BaseConnection` choose serial from a `_serial` suffix? (Netmiko 4.8.0)
Yes, by substring, not by registry. Relevant code in `BaseConnection.__init__` [N2]:
```python
if not ip and not host and "serial" not in device_type:
    raise ValueError("Either ip or host must be set")
...
self.serial_settings = {
    "port": "COM1",
    "baudrate": 9600,
    "bytesize": serial.EIGHTBITS,
    "parity": serial.PARITY_NONE,
    "stopbits": serial.STOPBITS_ONE,
}
if serial_settings is None: serial_settings = {}
self.serial_settings.update(serial_settings)

if "serial" in device_type:
    self.host = "serial"
    comm_port = self.serial_settings.pop("port")
    # Get the proper comm port reference if a name was enterred
    comm_port = check_serial_port(comm_port)
    self.serial_settings.update({"port": comm_port})
...
if "_telnet" in device_type:
    self.protocol = "telnet"
    self.password = password or ""
elif "_serial" in device_type:
    self.protocol = "serial"
    self.password = password or ""
else:
    self.protocol = "ssh"
```
and in `establish_connection`:
```python
elif self.protocol == "serial":
    self.remote_conn = serial.Serial(**self.serial_settings)
    self.channel = SerialChannel(conn=self.remote_conn, encoding=self.encoding)
    self.serial_login()
```
`serial_login` is defined in `CiscoBaseConnection` (inherited by both classes) as: write `\r\n`, return immediately if the response matches `\#\s*$` or `>\s*$`, otherwise call `telnet_login(...)` (patterns: user `(?:user:|username|login)`, password `assword`). [N3] `check_serial_port` requires the port name to match exactly `serial.tools.list_ports.comports()` `.device` (e.g. `COM3`) else `ValueError`. [N6]

So `ArubaCxSSH(device_type="aruba_aoscx_serial", serial_settings={"port": "COM3", "baudrate": 115200}, username=..., password=...)` and `HPProcurveSSH(device_type="hp_procurve_serial", serial_settings={"port": "COM4", "baudrate": 9600}, ...)` will take the serial path. `ConnectHandler(device_type="aruba_aoscx_serial")` will **not** work (not in `CLASS_MAPPER`). Extra `serial.Serial` kwargs such as `xonxoff=True` pass through `**self.serial_settings`. [N2]

Gotchas:
- Netmiko's default is 9600 baud; CX needs `baudrate=115200`. AOS-S speed-senses but boot text is 9600.
- CX day-0 `Please configure the 'admin' user account password.` is not handled by `telnet_login`: it will keep sending CR and finally raise `NetmikoAuthenticationException("Login failed")`. Do day-0 with a custom `serial_login` override or raw pyserial, then reconnect.
- On AOS-S, `telnet_login`'s loop sends `\r\n` every iteration, which doubles as "press any key"; but `session_preparation` in `HPProcurveBase` also tries to consume the banner, so a first-login password prompt would need custom handling.
- `session_preparation` then runs the normal preparation (`enable()`, `no page`). `disconnect()` closes the pyserial port. [N2]
- Windows: only one process can hold a COM port; the USB-serial device name changes with ports.

---

## 9. Read-only show commands (IOS equivalent)

| IOS | AOS-CX | AOS-S |
|---|---|---|
| `show version` | `show version` [K-CX-ver] (+ `show images`, `show system`) | `show version`; `show flash`; `show system information` [K-S13b][K-S17b] |
| `show inventory` | `show system` + `show module` [K-CX-sys][K-CX-mod]. A `show inventory` command was not found in the 10.13 CLI guide for 6000/6100: **UNVERIFIED (medium)** that it does not exist on other models | `show modules` (+ `show system information`) [K-S17b] |
| `show ip interface brief` | The Jan 2019 HPE cross-vendor guide lists `show ip interface brief` in the CX column [K-S17c]. The 10.13 6000/6100 CLI guide documents only `show ip interface <INTERFACE-ID>` [K-CXC]: **verify per model**; fallbacks: `show interface brief` + `show ip interface <id>` | `show ip` (per-VLAN IP table) [K-ntc-hp]; `show vlans` |
| `show interfaces status` | `show interface brief` (tabular), `show interface [<id>] physical` [K-CXC] | `show interfaces brief` (documented) [K-S17b]; a `show interfaces status` template exists in ntc-templates but I found no HPE doc for it: **UNVERIFIED (medium)** |
| `show interfaces` | `show interface [<id>]`, `show interface lag [<id>]`, `show interface vlan [<id>]` [K-CXC] | `show interfaces [<port-list>]` (plain form **UNVERIFIED (high)**); `show interfaces brief` and `show interfaces custom <ports> <fields>` are documented [K-S15][K-S17b] |
| `show vlan brief` | `show vlan` (+ `show vlan summary`, `show vlan port`) [K-CXC] | `show vlans` (+ `show vlans ports <p> detail`) [K-S17b] |
| `show cdp neighbors` | `show cdp neighbor-info` (+ `show cdp`, `show cdp traffic`) [K-CXC] | `show cdp neighbors [<port>] [detail]` (CDP receive-only by default) [K-S17b] |
| `show lldp neighbors` | `show lldp neighbor-info [detail]` [K-CXC] | `show lldp info remote-device [<port>]` [K-S17b] |
| `show spanning-tree summary` | `show spanning-tree` (also `show spanning-tree summary root|port`, `detail`, `mst`) [K-CXC] | `show spanning-tree` (+ `show spanning-tree mst-config`) [K-S17c]: no `summary` keyword found |
| `show running-config` | `show running-config` (+ `current-context`, `all`, `<feature>`, `json`) [K-CXG] | `show running-config` (+ `concise`, `write terminal`) [K-S14][K-S9] |

Real captures for table parsing: CX `show vlan` / `show interface`, AOS-S `show interfaces brief` (incl. stack `1/28-Trk1`), `show vlans`, `show trunks`, `show lldp info remote-device`, `show ip`. [K-ntc-cx][K-ntc-hp]

### 9.1 Pipes / output filters
- **AOS-CX**: `show ... | include <PATTERN> | exclude <PATTERN> | begin [<LINES>] <PATTERN> | count [<PATTERN>] | redirect [<FILE-NAME>]`; pipes chain; the 10.13 CLI guide says "supported for use with the show and list commands only", the Fundamentals text says show only. `Tab` completion is not supported for pipes. [K-CXC]
  - **`redirect` writes the show output to a file.** Where (local flash only, or a URL) is not stated: **UNVERIFIED (medium)**. Block `redirect` (the whole `| redirect` token) in a read-only guard.
  - Other CX output-to-file/remote paths to block: `copy command-output "<cmd>" <url|usb:/file>` (10.15 diagnostics guide, via search summary: **UNVERIFIED (medium)** on min version), `copy show-tech feature ...`, `copy running-config|startup-config|checkpoint <url|usb>`, `copy ... usb:`. [K-CXG][K-CX-copy-remote]
- **AOS-S**: `show <cmd> | include <regex> | exclude <regex> | begin <regex>` (regex, case-sensitive). No `redirect`/`count` documented. [K-S17d] (the pipe is written `show run | include ipv6`).
  - AOS-S write-out paths to block: `copy command-output "<cli-command>" tftp|sftp|usb|xmodem ...`, `copy running-config|startup-config|flash|config <name> tftp|sftp|usb|xmodem ...`, `tftp`/`sftp` client commands, `cfg-backup` (writes flash), `terminal`/`kill`/`repeat` as session-affecting. [K-S15 (copy command-output <"cli-command"> tftp...)][K-S14]
- Suggested read-only guard for both: allow only commands starting with `show ` (and `display`-style none), strip whitespace and abbreviations, then allow pipes only with `include|exclude|begin` (and `count` on CX); reject `redirect`, `;`/`&&`-style chaining, and `do `.

---

## 10. Destructive commands to guard

### 10.1 AOS-CX
- Reboot: `boot system [primary|secondary|serviceos]` (prompts to save config, then confirm); `boot set-default {primary|secondary}`. There is no IOS-style `reload` documented in the 10.14 job-scheduler guide. [K-CXG][K-CX-job]
- Config wipe: `erase startup-config`; `erase checkpoint <name>|all`; `erase core-dump ...`. [K-CX-cp-erase]
- Factory reset: `erase all` (retains secure-mode settings), `erase all zeroize` (not available in enhanced secure mode); from ServiceOS `erase zeroize`, `format` (wipes primary storage and reboots), `boot` menu. [K-CXC][K-CX-Hard-Factory]
- Other: `copy <url|usb> running-config` (additive merge, can partially apply), `copy ... startup-config` (overwrites), `write memory`, `no user <name>` (deletes sessions; prompts `[y/n]`), `console baud-rate` (breaks the console), `service export-password`, `auto-confirm`, `usb`, firmware `copy <url> primary|secondary` (not researched in detail). [K-CXS-user][K-CX-baud][K-CXC]
- Config-mode lines that can cut you off: `no ssh server vrf <vrf>`, `no https-server vrf <vrf>`, `interface mgmt` changes, `shutdown` on the path in use. [K-CX-Hard-Factory]

### 10.2 AOS-S
- Reboot: `reload` (prompts to save), `reload at|after` (no prompt; drops unsaved changes), `boot`, `boot system flash {primary|secondary}`, `boot set-default flash ...`, `cfg-restore ... force` (reboots). [K-S6][K-S13b][K-S4]
- Config wipe: `erase startup-config`, `erase default-config`, `erase config <name>`, `erase all [zeroize]` (also destroys passwords/keys; software images are kept; up to 60 min), ROM console `erase-all zeroize`, `erase flash [primary|secondary]` (deletes the OS image), `stacking factory-reset`, `front-panel-security factory-reset`. [K-S13b][K-S13][K-S14][K-S12-asg]
- Other: `copy tftp|sftp|usb|xmodem flash ...` (firmware), `copy ... startup-config|running-config` (overwrites), `include-credentials`/`encrypt-credentials` and their `no` forms (rewrite credential storage, reboot-to-default risk), `no include-credentials store-in-config`, `password manager|operator` changes, `kill`, console settings (`console baud-rate`, `terminal`, `flow-control`) that need a reboot. [K-S12][K-S10][K-S15]
- No `delete`/`format` equivalent was found in the AOS-S docs I read (`erase` is the verb): **UNVERIFIED (medium)**.

---

## 11. Lab / virtual testing

### 11.1 AOS-CX Switch Simulator (OVA)
- Official, real AOS-CX NOS in a VM ("AOS-CX Switch Simulation Software OVA"). Description: "a virtual platform to enable simulation of the AOS-CX Network Operating System ... OSPF and BGP ... Network Analytics Engine and the REST API can be simulated ... easily implemented in the EVE-NG or GNS3". [K-OVA-RN]
- Download: HPE Networking Support Portal (`https://networkingsupport.hpe.com`), login required; free account reported (vendor blog) but the GNS3 registry file lists availability as "service-contract" and older threads say registration/device requirements changed by release, so access terms are **UNVERIFIED (medium)**. [K-GNS3][K-netpilot (secondary)][K-AH-ova]
- Versions: community/HPE posts name 10.13.1000 (Jan 2024) as latest stable in Mar 2024; the GNS3 registry lists images up to 10.15.0005 (Nov 2024) and netlab cites a 10.15 download. 10.16+ simulator availability: **UNVERIFIED**. [K-AH-ova][K-GNS3][K-netlab]
- Resources: 4 GB RAM, 2 vCPU (2100 MHz); GNS3 template: 4096 MB, 2 CPUs, 8 virtio NICs, telnet console, KVM required, first port `mgmt`, then `1/1/1`.. [K-OVA-RN][K-GNS3]
- Hypervisors in release notes: VirtualBox 5.2.8+, VMware Workstation Pro 12.5.8+, Fusion 10.0.1+ (run only one OVA at a time), ESXi 6.0+. [K-OVA-RN]
- **Non-functional** features in the 10.13.1000 simulator (configurable but not functional): CoPP, link detection ("link is always on"; use `shutdown`/`no shutdown`), classifier policy, ECMP, MAC ACLs, QoS, ADC (NAE), UDLD, mirroring, ERPS, **firmware upgrade**, DCBx, PBR, VXLAN with L3 VTEPs/with VSX, UBT, **VSF**, VLAN translation, watchdog, PBT, MAC lockdown/lockout, captive portal, GBP. Also unsupported: virtual MAC / symmetric IRB issues. [K-OVA-RN] (Earlier note: "ACL will work but not policy or classifier" [K-AH-lim].)
- Implication for this tool: checkpoints, `checkpoint auto`, `copy`, `write memory`, running-config rendering, ciphertext, and user/role behavior are normal CX software and should be testable; VSF/stacking, firmware upgrade, and link-state behavior are not.
- Default login on the simulator: `admin`, no password, new password required at first login. [K-GNS3] netlab recipe: log in as admin, set password, `write memory`, paste config, `start-shell`, `sudo poweroff`. [K-netlab]
- **Cisco CML:** no official AOS-CX node/image definition found (Cisco's `ciscops/cml-custom-images` repo has only a GitLab example; a Cisco Learning Network thread on running the Aruba switch simulator in CML had no answer in my fetch). CML supports custom QEMU/KVM node definitions with qcow2 disks, and the simulator already runs under KVM in GNS3, EVE-NG and netlab/libvirt, so a custom node definition is **plausible**. Feasibility/exact definition for CML: **UNVERIFIED (medium)**. The conversion path used by EVE-NG: extract the VMDK from the OVA (`tar -xvf`), `qemu-img convert -f vmdk -O qcow2`. [K-netpilot (secondary, citing the EVE-NG how-to)][K-netlab]
- Also usable: containerlab (`tjbalzer/clab-aoscx-labs` shows many AOS-CX lab configs) [K-CX-Clab]; netlab libvirt box [K-netlab].

### 11.2 AOS-S
- No official virtual AOS-S/ProCurve image found. Searches returned only the AOS-CX simulator. **UNVERIFIED (high)** that none exists. Use physical switches (e.g. 2530/2540/2930F) for AOS-S testing.

---

## Design implications (per-platform driver, compared with the IOS driver)

**AOS-CX**
- Rollback is in-band and strong: use `checkpoint auto <N>` ... `checkpoint auto confirm` (Manager context) or explicit `copy running-config checkpoint <name>` / `checkpoint rollback <name>`. Do not use `copy <url> running-config` (additive merge). Test the "session lost" case on the target release before trusting the timer (older reports disagree).
- Do not use Netmiko's CX `check_config_mode` as-is: it only matches `(config)#`. Subclass (match `(config`) or always end with `end` yourself, and verify the prompt after every push.
- Prompts: use a prompt regex that accepts `hostname#` and `hostname(config-xxx)#`. There is no enable secret. A `>` prompt means a non-admin role.
- Errors are not uniformly `%`-prefixed. Treat `% Command incomplete.`, `Invalid input:`, `Error:`, `Cannot execute command` and `... doesn't exist` as failures; file-based loads report only `NOT applied` plus `show events -d hpe-config`.
- Secrets are ciphertext bound to the export password. A tool that wants to push users/keys must either send `plaintext` (visible in command history/session logs; keep out of `session_log`) or copy ciphertext from a switch with the same export password. Never try to generate ciphertext.
- Use `no page` (session-local). Save with `write memory`. Use full command words (CX auto-completes on space; Netmiko's comment).
- Day-0: 115200 8N1, `admin`/empty then forced password dialog; handle that dialog outside Netmiko's `telnet_login`.
- Lab: the OVA covers most of the driver logic; VSF/firmware/link-state are not simulated.

**AOS-S**
- Rollback is two-tier: `cfg-backup` + alias + `job ... delay ... count 1` -> `cfg-restore flash <name>` (no reboot, 16.05+), but it silently has no effect (fails) if the diff contains reboot-required commands. The only unconditional fallback is `reload after <N>` (reboot-based, discards unsaved config). There is no commit-confirm.
- Credentials are outside the running config by default; `include-credentials`/`encrypt-credentials` are global, interactive, version-sensitive and can lead to a factory-default boot if mishandled. The tool should detect their state (`show include-credentials`, `show encrypt-credentials`) and refuse to rely on text backups for credentials. SNMPv3 hashes are engine-ID bound.
- Contexts end with `exit`, names are double-quoted, ports can be `1`, `A1`, `1/1`, `Trk1`; handle lists/ranges (`A1-A24,F1-F24`) and quote stripping. Use 3-space indentation as the observed norm but parse by structure, not by indentation width.
- Netmiko handles `enable`, `no page` and the banner, but the privilege dance requires an operator-or-manager account with a password (`enable` prompts `Password:`, and `Username:` when user names are configured). Plan for RADIUS operator landing.
- Errors have no `%`; use `Invalid input:` / `Incomplete input:` patterns. Mid-config `(y/n)` prompts exist (credentials, erase, cfg-restore, reload) and need explicit handling.
- Day-0: boot text at 9600, then speed-sense; flow control default XON/XOFF; "Press any key to continue"; first-login manager-password dialog from 16.10.0009 (text unverified).
- Facts need three commands (`show system information`, `show modules`, `show version`) with two-column outputs and per-member stack blocks; model comes from `show modules`.

---

## Unverified / open items (could not verify)

CX: `% Unknown command.` / `% Ambiguous command.` strings; generic out-of-range text; whether `enable` works for operator-group users; whether `auto-confirm` is per-session; whether checkpoint restore is service-impacting (docs silent; in-place apply inferred); whether `checkpoint auto confirm` also saves startup-config; reliability of auto-revert after session loss on 10.13+ (only 10.04-era reports found); whether CX 10.1x prints names with spaces quoted; 4-space indent on current releases; printed form of `aaa authentication ...`, `snmpv3 user ... ciphertext`, `tacacs-server host ... key ciphertext` lines; `show ip interface brief` and `show inventory` on the 10.13 releases (conflicting evidence); whether `| redirect` can target a URL; `copy command-output` min version; IPsec/IKE/site-to-site and certificate-private-key handling in running-config; `reload` in AOS-CX; AOS-CX min version beyond "10.07 or earlier"; simulator download terms and 10.16+ availability; whether a CML custom node for the CX OVA boots and numbers interfaces correctly.

AOS-S: "Ambiguous" error text; out-of-range wording outside the user-role context; whether `configure terminal` abbreviations/`terminal width 511` are accepted (the second is harmless); exact first-login manager-password prompt text on 16.10.0009+; what `copy <src> running-config` does (merge/replace/reboot); how `cfg-restore` treats a backup that lacks credentials; how the radius-server host line renders without include-credentials and the RADIUS `encrypted-key` printed form; whether SNTP/NTP key-value prints in clear without include-credentials; OSPF/BGP/IPsec/certificate secret lines; whether `show interfaces status` exists; non-chassis `show modules` layout; default hostname; success output of `write memory`; `job` availability on every listed model/version; `no reload` exact accepted forms beyond the documented `no reload [after|at]`; whether `cfg-backup` file names have length limits; HPE's statement of console flow-control recommendation (secondary).

Not researched: AOS-CX VSX/VSF specifics, CX firmware-upgrade commands, AOS-S stacking (VSF vs backplane) rollback behavior beyond the documented limitation.

---

## Sources (key -> URL)

**Netmiko v4.8.0 (read directly)**
- [N1] https://raw.githubusercontent.com/ktbyers/netmiko/v4.8.0/netmiko/aruba/aruba_aoscx.py
- [N2] https://raw.githubusercontent.com/ktbyers/netmiko/v4.8.0/netmiko/base_connection.py
- [N3] https://raw.githubusercontent.com/ktbyers/netmiko/v4.8.0/netmiko/cisco_base_connection.py
- [N4] https://raw.githubusercontent.com/ktbyers/netmiko/v4.8.0/netmiko/hp/hp_procurve.py
- [N5] https://raw.githubusercontent.com/ktbyers/netmiko/v4.8.0/netmiko/ssh_dispatcher.py
- [N6] https://raw.githubusercontent.com/ktbyers/netmiko/v4.8.0/netmiko/utilities.py (`check_serial_port`)

**AOS-CX (HPE docs)**
- [K-CXG] Fundamentals Guide 10.13, 6200: https://arubanetworking.hpe.com/techdocs/AOS-CX/10.13/PDF/fundamentals_6200.pdf (checkpoints chapter, copy/erase/write memory/boot/show running-config/show system/show module/console)
- [K-CXC] CLI Guide 10.13, 6000/6100: https://arubanetworking.hpe.com/techdocs/AOS-CX/10.13/PDF/cli_6000-6100.pdf (page, pipe, auto-confirm, enable, configure terminal, erase zeroize, format, show interface/ip interface/spanning-tree)
- [K-CXS] Security Guide 10.13, 6200/6300/6400: https://arubanetworking.hpe.com/techdocs/AOS-CX/10.13/PDF/security_6200-6300-6400.pdf ; 8xxx: https://arubanetworking.hpe.com/techdocs/AOS-CX/10.13/PDF/security_8xxx-9300-10000.pdf
- [K-CXI] IP Routing Guide 10.13, 4100i/6000/6100/6200: https://arubanetworking.hpe.com/techdocs/AOS-CX/10.13/PDF/ip_route_4100i-6000-6100-6200.pdf
- [K-CX-auto] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.13/HTML/fundamentals_8400/Content/Chp_Cfg_FW_mgt/Chk_cmds/che-aut.htm
- [K-CX-auto-confirm] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.13/HTML/fundamentals_8400/Content/Chp_Cfg_FW_mgt/Chk_cmds/che-aut-con.htm
- [K-CX-cp-create] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.13/HTML/fundamentals_8400/Content/Chp_Cfg_FW_mgt/Chk_cmds/cop-run-con-st-con-che-che-nam.htm
- [K-CX-cp-restore] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.13/HTML/fundamentals_8400/Content/Chp_Cfg_FW_mgt/Chk_cmds/cop-che-che-nam-ru-con-sta-con.htm
- [K-CX-cp-rollback] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.13/HTML/fundamentals_8400/Content/Chp_Cfg_FW_mgt/Chk_cmds/che-rol.htm
- [K-CX-cp-erase] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.13/HTML/fundamentals_8400/Content/Chp_Cfg_FW_mgt/Chk_cmds/era-ch-che-nam-sta-con.htm
- [K-CX-cp-post] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.13/HTML/fundamentals_8400/Content/Chp_Cfg_FW_mgt/Chk_cmds/che-pos-con.htm
- [K-CX-cp-diff] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.13/HTML/fundamentals_8400/Content/Chp_Cfg_FW_mgt/Chk_cmds/che-dif.htm
- [K-CX-copy-remote] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.13/HTML/fundamentals_8400/Content/Chp_Cfg_FW_mgt/Chk_cmds/cop-ru-con-sta-con.htm
- [K-CX-ver] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.14/HTML/fundamentals_6200/Content/SysHW_cmds/sho-ver-10.htm
- [K-CX-sys] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.14/HTML/fundamentals_6200/Content/SysHW_cmds/sho-sys-10.htm
- [K-CX-mod] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.14/HTML/fundamentals_6200/Content/SysHW_cmds/sho-mod-10.htm
- [K-CX-baud] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.14/HTML/fundamentals_6200/Content/SysHW_cmds/con-baud-rate.htm
- [K-CX-NTP] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.14/HTML/fundamentals_6200/Content/Chp_IniCfg/NTP_cmds/ntp-aut-key-10.htm
- [K-CX-Login] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.17/HTML/fundamentals_8400/Content/Chp_IniCfg/log-fir-tim-10.htm
- [K-CX-defadm] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.16/HTML/security_5420-6200-6300-6400/Content/Chp_Mng_Use/def-use-adm-10.htm
- [K-CX-Hard-Factory] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.14/HTML/hardening/Content/Chp_hard-mgmt/fact-def.htm
- [K-CX-Hard-SNMP] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.14/HTML/hardening/Content/Chp_hard-mgmt/sec-snmp-acc.htm
- [K-CX-Roles] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.14/HTML/security_6200-6300-6400/Content/Chp_Mng_Use/bui-in-use-gro-the-pri-10.htm
- [K-CXS-user] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.14/HTML/security_6200-6300-6400/Content/Chp_Mng_Use/User_grp_cmds/use-10.htm
- [K-CXS-exp] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.14/HTML/security_6200-6300-6400/Content/Chp_Mng_Use/User_grp_cmds/ser-exp-pas.htm
- [K-CXS-rad] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.14/HTML/security_6200-6300-6400/Content/Rem_AAA_cmds/rad-ser-hos-10.htm
- [K-CXS-tac] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.14/HTML/security_6200-6300-6400/Content/Rem_AAA_cmds/tac-ser-key-10.htm
- [K-CX-BGP] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.15/HTML/ip_route_8400/Content/Chp_BGP/BGP_cmds/nei-pas-10.87.htm (search-result page; syntax from search summary, page itself not read in full)
- [K-SNMP-CX] https://arubanetworking.hpe.com/techdocs/AOS-CX/10.13/PDF/snmp_mib.pdf and https://arubanetworking.hpe.com/techdocs/AOS-CX/10.14/HTML/fundamentals_4100i-6000-6100/Content/Chp_SNMP/cnf-snm.htm
- [K-CX-job] Job Scheduler Guide 10.14: https://arubanetworking.hpe.com/techdocs/AOS-CX/10.14/PDF/job_scheduler.pdf
- [K-OVA-RN] OVA release notes 10.13.1000: https://arubanetworking.hpe.com/techdocs/AOS-CX/10.13/OVA/RN/rn_ova_10.13.1000.pdf
- [K-CX-DCN] Device-generated sample config (10.04): https://raw.githubusercontent.com/aruba/aoscx-ansible-dcn-workflows/master/configs/sample_configs/arch2/Zone1-Rack1-Access1-final.conf
- [K-CX-Clab] Hand-written lab configs for AOS-CX in containerlab: https://github.com/tjbalzer/clab-aoscx-labs
- [K-AnsT] https://raw.githubusercontent.com/aruba/aoscx-ansible-collection/master/plugins/terminal/aoscx.py
- [K-Scrapli] https://raw.githubusercontent.com/scrapli/scrapli_community/main/scrapli_community/aruba/aoscx/aruba_aoscx.py
- [K-ntc-cx] Real CX outputs: https://github.com/networktocode/ntc-templates/tree/master/tests/aruba_aoscx (show_system, show_module, show_vlan, show_interface)
- [K-GNS3] https://raw.githubusercontent.com/GNS3/gns3-registry/master/appliances/aruba-arubaoscx.gns3a
- [K-netlab] https://netlab.tools/labs/arubacx/

**AOS-CX (community / secondary; lower weight)**
- [K-AH-ckpt-ov] https://community.arubanetworks.com/discussion/arubaos-cx-configuration-checkpoints-and-auto-rollback
- [K-AH-ckpt] https://community.arubanetworks.com/discussion/aos-cx-8320-checkpoint-auto-minutes-failure-after-admin-disconnect
- [K-AH-ckpt2] https://community.arubanetworks.com/discussion/aos-cx-8320-checkpoint-auto-lapsed-time-show and https://community.arubanetworks.com/discussion/aos-cx-boot-reload-restart-disconnect-timer
- [K-AH-Incomplete] https://airheads.hpe.com/discussion/not-able-to-backup-config-via-tftp-output-gives-command-incomplete
- [K-AH-Internal] https://airheads.hpe.com/discussion/os-cx-cannot-execute-command-internal-error
- [K-AH-ova] https://community.arubanetworks.com/discussion/latest-aruba-aos-cx-simulator-release
- [K-AH-lim] https://community.arubanetworks.com/discussion/limitations-of-aos-cx-simulator
- [K-netpilot] https://www.netpilot.io/blog/aruba-cx-simulator-lab-options (vendor blog; use only for corroboration)

**AOS-S (HPE docs)**
- [K-S1] cfg-restore: https://arubanetworking.hpe.com/techdocs/AOS-S/16.11/MCG/KB/content/kb/cfg-res.htm
- [K-S2] cfg-backup: https://arubanetworking.hpe.com/techdocs/AOS-S/16.11/MCG/KB/content/kb/cfg-bac.htm
- [K-S3] command list: https://arubanetworking.hpe.com/techdocs/AOS-S/16.11/MCG/YAYB/content/common%20files/com-use-swi-con.htm
- [K-S4] force restore: https://arubanetworking.hpe.com/techdocs/AOS-S/16.10/MCG/YC/content/common%20files/for-cnf-res.htm (and cfg-restore sample: .../cfg-res-con-bkp.htm)
- [K-S5] rollback with job scheduler: https://arubanetworking.hpe.com/techdocs/AOS-S/16.11/MCG/WC/content/common%20files/rol-bac-sta-cnf-usi-job-sch.htm
- [K-S6] reload: https://arubanetworking.hpe.com/techdocs/AOS-S/16.11/MCG/KB/content/kb/boo-act-man-mod.htm
- [K-S7] job: https://arubanetworking.hpe.com/techdocs/AOS-S/16.10/MCG/WB/content/common%20files/job-del-ena-dis8.htm
- [K-S8] HPE white paper "Configuration restore without reboot" (16.05): https://higherlogicdownload.s3.amazonaws.com/HPE/MigratedAssets/Config_Restore_without_Reboot.pdf
- [K-S9] include-credentials: https://arubanetworking.hpe.com/techdocs/AOS-S/16.11/ASG/WC/content/common%20files/ena-sto-dis-sec-cre.htm
- [K-S10] encrypt-credentials: https://arubanetworking.hpe.com/techdocs/AOS-S/16.10/ASG/KB/content/asg%20kb/enc-cre.htm
- [K-S11] SNMP credentials: https://arubanetworking.hpe.com/techdocs/AOS-S/16.11/ASG/WC/content/common%20files/snm-sec-cre.htm
- [K-S12] Access Security Guide 2540 16.11: https://arubanetworking.hpe.com/techdocs/AOS-Switch/16.11/Aruba%202540%20Access%20Security%20Guide%20for%20AOS-S%2016.11.pdf
- [K-S12-asg] Access Security Guide 2930M/F 16.10: https://arubanetworking.hpe.com/techdocs/AOS-Switch/16.10/Aruba%202930M&F%20Access%20Security%20Guide%20for%20AOS-S%20Switch%2016.10.pdf (also cited as [K-S13] for the crypto pki delete line)
- [K-S13] Basic Operation Guide 16.11: https://arubanetworking.hpe.com/techdocs/AOS-Switch/16.11/Aruba%20Basic%20Operation%20Guide%20for%20AOS-S%2016.11.pdf
- [K-S13b] Basic Operation Guide 16.06: https://icecat-content.ingrammicro.com/inishop/objects/mmo_43113615_1584514798_2436_14606.pdf
- [K-S14] 2930M/F Management and Configuration Guide 16.11: https://arubanetworking.hpe.com/techdocs/AOS-Switch/16.11/Aruba%202930%20M&F%20Management%20and%20Configuration%20Guide%20for%20AOS-S%2016.11.pdf
- [K-S15] 2530 Management and Configuration Guide 16.05: https://www.intesiscon.com/ficheros/manuales-tecnicos/206-Switch-hp-aruba-j97773a.pdf
- [K-S16] copy tftp: https://arubanetworking.hpe.com/techdocs/AOS-S/16.11/MCG/YC/content/common%20files/tft-cop-cnf-fil-rem-hos-cli.htm
- [K-S17b] ArubaOS-Switch/Comware/IOS CLI Reference v3.3: https://higherlogicdownload.s3.amazonaws.com/HPE/MigratedAssets/ArubaOS-Switch,%20Comware%20and%20Cisco%20IOS%20CLI%20Reference%20Guide%20Version%203.3.pdf
- [K-S17c] CX/AOS-S/IOS CLI Reference (Jan 2019): https://cadinc.com/wp-content/uploads/2024/08/CLI-Ref-Guide-2019v1-HPE-Aruba-Cisco-CX-from-Carolina-Advanced-Digtial.pdf
- [K-S17d] pipe syntax: 2930 MCG 16.11 ([K-S14]), section "Searching for text using pattern matching with show command"
- [K-S18] show modules: https://arubanetworking.hpe.com/techdocs/AOS-S/16.11/MCG/KB/content/kb/sho-mod-det-vsf-mem.htm
- [K-S19] RADIUS privilege-mode: https://arubanetworking.hpe.com/techdocs/AOS-S/16.10/ASG/KB/content/asg%20kb/ena-man-acc-pri-opt.htm ; [K-S19b] attributes: https://arubanetworking.hpe.com/techdocs/AOS-S/16.11/ASG/WC/content/common%20files/man-acc.htm
- [K-S-KB-baud] HPE KB, console baud rates: https://support.hpe.com/hpesc/public/docDisplay?docId=sf000095853en_us&docLocale=en_US
- [K-S-first-login] enhanced-secure-mode password prompt: https://www.arubanetworks.com/techdocs/AOS-S/16.10/ASG/WC/content/common%20files/pro-pas-whe-fir-log-in..htm
- [K-S21] Community AOS-S config dump (5406R, 16.06): https://raw.githubusercontent.com/Drannor-Dosalith/vscode-arubaos-config-syntax/HEAD/Test_config.aos
- [K-ntc-hp] Real AOS-S outputs: https://github.com/networktocode/ntc-templates/tree/master/tests/hp_procurve (show_system, show_vlans, show_interfaces_brief, show_trunks, show_ip, show_lldp_info_remote-device)
- [K-S-2930F-console] https://support.hpe.com/hpesc/public/docDisplay?docId=a00091345en_us&docLocale=en_US (found via search summary only; not read)
