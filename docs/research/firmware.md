<!-- Research notes gathered 2026-11-15 for firmware (OS image) lifecycle management.
     Sources are in the table at the end. Items marked UNVERIFIED must be confirmed on a device. -->

# Firmware lifecycle research: Cisco IOS / IOS-XE, AOS-CX, AOS-S (for Netmiko 4.8.0 / Paramiko 5)

Research date: 2026-11-15. Installed versions checked in `.venv`: Netmiko 4.8.0, scp 0.16.2 (`scp-*.dist-info/METADATA`), Paramiko 5.0.0 (`paramiko-5.0.0.dist-info`).

## 0. How to read this report

- Every fact carries `[Kn]` (key into the Sources table) or is marked **UNVERIFIED (confidence: high/medium/low)**. "UNVERIFIED (high)" means background knowledge or a community post, not a vendor document or source file I read.
- Quoted CLI text is copied from the vendor page. Vendor examples use their own addresses; in the examples I wrote myself I use 192.0.2.x.
- `arubanetworking.hpe.com` PDFs and Cisco PDFs returned 403/500 to my fetchers. I read the HTML pages through a JS-rendering fetcher. AOS-CX release-note PDFs could not be read; facts from them come only from search-engine excerpts and are marked as such.
- The research was cut short by the session budget. Sections I did not reach are listed under "Not covered" at the end.

---

## A. Cisco IOS classic (Catalyst 2960-X, 3560-CX; 2960-X stacks)

### A.1 Current state

- `show version` shows the running image; the model name at the end is the factory value and "does not change if you upgrade the software license" [C1]. The line `System image file is "flash:..."` names the booted file (format shown for IOS-XE in [C6]; same line on IOS classic is UNVERIFIED (high)).
- `show boot` shows the boot variables; on a stack, "information is shown for each switch in the stack" [C2]. Doc sample:
  ```
  Switch# show boot
  BOOT path-list        :flash:/image
  Config file           :flash:/config.text
  Private Config file   :flash:/private-config.text
  Enable Break          :no
  Manual Boot           :yes
  HELPER path-list      :
  Auto upgrade          :yes
  ```
  `BOOT path-list` is the boot variable; `Manual Boot : yes` means the switch stops in boot loader; `Auto upgrade` is the `boot auto-copy-sw` stack feature [C2].
- `dir flash:` lists other images on flash [C1].
- Images: 2960-X ships both a `.bin` (`c2960x-universalk9-mz.152-6.bin`) and a `.tar` (`c2960x-universalk9-tar.152-6.tar`, image plus Device Manager HTML) per release [C1 (15.2(6)E notes)].

### A.2 Free space

- `dir flash:` ends with `<total> bytes total (<free> bytes free)` (format shown on IOS-XE in [C4]; same on IOS classic UNVERIFIED (high)). Netmiko parses `(\d+) \w+ free` from `dir <fs>` [N1 L145-156].
- Per stack member: member flashes are `flash1:`, `flash2:` ... on 2960-X. UNVERIFIED (high): naming from experience; IOS-XE uses `flash-2:` instead [C5].

### A.3 Getting the image onto the device

(a) TFTP pull
- `copy tftp://192.0.2.10/c2960x-universalk9-mz.152-7.E10.bin flash:` asks `Destination filename [<name>]?` then prints `!!!!`, `[OK - <n> bytes]` and `<n> bytes copied in <t> secs` (exact output documented on IOS-XE [C4]; same prompts on IOS classic UNVERIFIED (high)). The prompt `Do you want to over write? [confirm]` when the file exists is UNVERIFIED (high).
- `archive download-sw` (the recommended path for stacks, takes the `.tar`): syntax per 15.2(7)E command reference [C2]: `archive download-sw {/directory | /force-reload | /imageonly | /leave-old-sw | /no-set-boot | /no-version-check | /overwrite | /reload | /safe} source-url`. Example: `archive download-sw /overwrite tftp://172.20.129.10/test-image.tar`.
  - `/reload` "Reloads the system after successfully downloading the image, unless the configuration has been changed and has not saved." `/force-reload` reloads unconditionally [C2].
  - Default: the current image is not overwritten (it is deleted and replaced if different), BOOT is updated to the new image, HTML files are downloaded [C2].
  - Without `/overwrite`, if the new image equals the one on flash or running on any member, nothing is downloaded [C2].
  - `/safe` and `/leave-old-sw` can fail on insufficient flash, "and an error message is displayed" (text not given) [C2].
  - `/no-version-check` skips the stack-protocol-version check; all members must share the stack protocol version [C2].
  - `/allow-feature-upgrade` appears in older platform guides but is not in the 2960-X 15.2(7)E reference [C2]; a user reports it rejected on 2960-X (community, UNVERIFIED).
- Source interface: `ip tftp source-interface <if>` is the usual fix when the server is only reachable via a management VRF (community reports only; UNVERIFIED (medium)).

(b) HTTP/HTTPS pull: `copy http://...` / `copy https://...` with `ip http client source-interface` — UNVERIFIED (medium) for 2960-X/3560-CX; not checked in a vendor doc.

(c) SCP/SFTP pull: `copy scp://user@192.0.2.10/file flash:` — UNVERIFIED (high) for these platforms; `archive download-sw ... scp://` is mentioned by users (community). IOS-XE lists `scp:` and `sftp:` as network file systems in `show file systems` [C5].

(d) Client push with SCP
- Server side needs: SSH working, an RSA key pair, AAA (`aaa new-model`, `aaa authentication login default local`, `aaa authorization exec default local`), a privilege-15 user, and `ip scp server enable` [C7]. "The `exec` keyword runs authorization to determine if the user is allowed to run an EXEC shell; therefore, you must use it when you configure SCP." [C7]
- Remote path is an IFS path such as `flash:/name.bin` (Netmiko builds `f"{file_system}/{dest_file}"` with `file_system` from `dir` -> `flash:`) [N1 L399-404][N4 L204-228].
- The SCP channel is a second SSH channel ("Once you are authenticated, another channel is opened to transfer the file with SCP") [C8]. Netmiko opens a whole new SSH connection for it [N1 L43-53]. Whether IOS accepts a second exec channel on the existing Netmiko transport is UNVERIFIED (medium).
- No SFTP server (see Python section iii).

### A.4 Verifying

- `verify /md5 flash:<file>` prints dots, `Done!`, then `verify /md5 (flash:<file>) = <32 hex>`; with an expected hash appended it prints `Verified (flash:<file>) = <hash>`; on mismatch `%Error verifying flash:<file>` then `Computed signature = ...` / `Submitted signature = ...` [C3][C9]. Netmiko parses `=\s+(\S+)` [N1 L344-356] and waits up to 300 s [N1 L369-382].
- `verify /sha512` on IOS classic 15.2E: UNVERIFIED (low); I found no vendor doc for it on IOS classic. Cisco posts SHA512 checksums for downloads [C10].
- Run time on a large image: not documented.

### A.5 Activating

- `boot system filename [switch {switch-number | all}]` (global config) [C2]. Typical: `no boot system`, `boot system switch all flash:<new>.bin`, `write memory` (pattern from IOS-XE bundle mode [C5]; `switch all` keyword from [C2]).
- Boot-loader behaviour: "If the BOOT variable is set but the specified images cannot be loaded, the system attempts to boot the first bootable file that it can find in the flash: file system"; if BOOT is unset, a depth-first search of flash is used [C2].
- Reload prompts: `System configuration has been modified. Save? [yes/no]:` and `Proceed with reload? [confirm]` — UNVERIFIED (high) on IOS classic (IOS-XE stack text in B.5).

### A.6 Stacks (2960-X FlexStack-Plus)

- Up to 8 switches homogeneous / 4 mixed [C1].
- `archive download-sw`/`archive upload-sw` run on the stack master and images reach all members (3560-X/2960 guide excerpts via search; 15.2(7)E reference describes member flash targets) [C2]; UNVERIFIED (medium) for the exact wording on 2960-X.
- With the `.bin` method the file must be copied to each member's flash (`copy flash:x.bin flash2:`); UNVERIFIED (high).
- `boot auto-copy-sw` copies the stack image to a version-mismatched joiner, which then reboots and joins [C2].
- Member state: `show switch` (State column; `V-Mismatch` text documented for IOS-XE [C5]). Rolling member upgrade: none documented for 2960-X (UNVERIFIED (medium)).

### A.7 After reload / cleanup

- Confirm with `show version` (and per member, `show version` member table — UNVERIFIED (high)). Delete old `.bin` with `delete flash:<old>.bin`; `/leave-old-sw` leaves the old image, removable with `delete` [C2].
- Reload duration: not documented.

### A.8 Hazards

- Boot variable naming a missing file falls back to "first bootable file" on flash, which may be the old image [C2] — tool should verify the file exists and hash matches before setting BOOT.
- `archive download-sw` without `/leave-old-sw` deletes the old image first (no rollback image) [C2].

---

## B. Cisco IOS-XE (Catalyst 9200/9300/9500, 3850; ISR 1100; 9800 WLC)

### B.1 Current state

- Mode: `show version` ends with a table; the `Mode` column is `INSTALL` or `BUNDLE` and `System image file is "flash:packages.conf"` (install) vs `"flash:cat9k_iosxe.17.15.03.SPA.bin"` (bundle) [C6]:
  ```
  System image file is "flash:packages.conf"
  Switch Ports Model SW Version SW Image Mode
  ------ ----- ----- ---------- ---------- ----
  * 1 65 C9300-48UXM 17.15.03 CAT9K_IOSXE INSTALL
  ```
  Running version: first line `Cisco IOS XE Software, Version 17.12.01` [C4].
- `show boot` (17.x, per member on stacks) [C4][C6]:
  ```
  Current Boot Variables:
  BOOT variable = flash:packages.conf;
  Boot Variables on next reload:
  BOOT variable = flash:packages.conf;
  Manual Boot = no
  ```
  16.x format differs: `BOOT variable = bootflash:cat9k_iosxe.16.11.01.SPA.bin;` and `MANUAL_BOOT variable = no` [C5].
- `show install summary` (install mode) [C5]:
  ```
  [ R0 R1 ] Installed Package(s) Information:
  State (St): I - Inactive, U - Activated & Uncommitted,
              C - Activated & Committed, D - Deactivated & Uncommitted
  --------------------------------------------------------------------------------
  Type  St   Filename/Version
  --------------------------------------------------------------------------------
  IMG   C    16.11.1.0.312
  --------------------------------------------------------------------------------
  Auto abort timer: inactive
  ```
  After `install add`: a second row `IMG   I    16.12.1.0.544`. After activate: `IMG   U ...` and `Auto abort timer: active on install_activate, time before rollback - 01:52:08` [C5].
- 9200 (and 9400/9600) support install mode only [C5].

### B.2 Free space

- `dir flash:` last line `11353194496 bytes total (8976625664 bytes free)` [C4].
- `show file systems` gives `Size(b)` and `Free(b)` for every member flash (`bootflash:`, `bootflash-2:` / `flash-2:`) [C5].
- Install mode needs "at least 1GB of space in flash, to expand a new image" [C4]. 9300 `.bin` is ~600 MB (17.12.1: 601216545 bytes) [C4].

### B.3 Getting the image onto the device

(a) TFTP: `copy tftp://10.8.0.6/image/cat9k_iosxe.17.12.01.SPA.bin flash:` [C4]:
  ```
  destination filename [cat9k_iosxe.17.12.01.SPA.bin]?
  Accessing tftp://10.8.0.6/image/cat9k_iosxe.17.12.01.SPA.bin...
  Loading /cat9k_iosxe.17.12.01.SPA.bin from 10.8.0.6 (via GigabitEthernet0/0):
  !!!!!!!!
  [OK - 601216545 bytes]
  601216545 bytes copied in 50.649 secs (11870255 bytes/sec)
  ```
  The 17.12 notes print the prompt both as `destination filename [...]?` and `Destination filename [...]?` — match case-insensitively [C4]. `install add file tftp:<path>` can also pull directly [C4].
  - Gi0/0 is the management port in `Mgmt-vrf`; `ip tftp source-interface GigabitEthernet0/0` is the common fix (community; UNVERIFIED (medium)).
(b) HTTP/HTTPS: `http:` and `https:` exist as network file systems [C5]; command form, prompts and `ip http client source-interface` UNVERIFIED (medium).
(c) SCP/SFTP pull: `scp:` and `sftp:` exist as network file systems [C5]; prompts UNVERIFIED.
(d) Client push: same SCP server prerequisites as IOS (privilege 15, AAA or local login, `ip scp server enable`) [C8][C7]. "By default, the file is copied to bootflash." [C8] IOS-XE has no SFTP server: "SFTP is not currently supported to transfer files over SSH", so OpenSSH 9+ needs `scp -O` [C8].

### B.4 Verifying

- `verify /md5 <fs>:<file> [hash]` output as in A.4 (IOS-XE example in [C9]).
- `verify <file>` on signed images prints `Embedded Hash   SHA1 : ...`, `Computed Hash   SHA2: ...` (multi-line) and `Digital signature successfully verified in file ...` [C9] (ASR/CSR example; on Cat9k UNVERIFIED (medium)).
- `show software authenticity file <file>` shows signer and `Hash Algorithm : SHA512` [C9].
- `file verify auto` / `copy /verify` verify images on copy and reload [C9].
- `verify /sha512`: a community user reports using it on a 3850; vendor doc not found — UNVERIFIED (medium).

### B.5 Activating

Install mode (must already be booted from `packages.conf`) [C4][C5]:
```
configure terminal
no boot system
boot system flash:packages.conf
no boot manual
end
write memory
install add file flash:cat9k_iosxe.17.12.01.SPA.bin activate commit
```
- Prompt during activate: `This operation may require a reload of the system. Do you want to proceed? [y/n]` [C4][C5].
- Success marker: `SUCCESS: install_add_activate_commit <date>`, then `Chassis 1 reloading, reason - Reload command`. "The system reloads automatically" [C4].
- Add `prompt-level none` to skip the prompt; the switch reloads as soon as ready [C5][C11].
- Separate steps: `install add file ...`, `install activate [auto-abort-timer <30-1200>]`, `install commit`, `install abort`, `install auto-abort-timer stop`; default abort timer 120 min; an activated but uncommitted package "remains active after the first reload, but not after the second reload" [C5][C11].
- Failure text when the package is already added: `FAILED: install_add_activate_commit : Super package already added. Add operation not allowed. 'install remove inactive' can be used to discard added packages` [C5].
- Downgrade warning line: `Warning!!! Image is being downgraded from 17.12.01.0.1186 to 17.11.01.0.1444.` [C4].
- Converting bundle -> install: set `boot system flash:packages.conf`, `wr`, then `install add file ... activate commit`; the output includes `Building configuration...` / `[OK]` (it saves config) [C11].
- Bundle mode: `no boot system`, `boot system bootflash:<new>.bin`, `write`, `reload`; "ensure you have copied the .bin to EVERY switch in the stack!" [C5].
- Reload prompt on a stack: `Reload command is being issued on Active unit, this reloads the whole stack` / `Proceed with reload? [confirm]` [C6]. The `Save? [yes/no]:` prompt text is UNVERIFIED (high).
- `install` commands cannot run from kron/EEM: "Interactive command cannot execute in non-interactive context" (community; UNVERIFIED (medium)).

### B.6 Stacks / redundancy

- Install mode copies the `.bin` to all members itself: `Copying flash:cat9k_iosxe.17.12.01.SPA.bin from Switch 1 to Switch 1 2` and runs add/activate/commit per member (`Add: Passed on [1 2]`) [C4]. Point to the active's flash or TFTP; a member file needs `flash-3:` explicitly [C4].
- Bundle mode: copy the `.bin` to each member yourself (`copy bootflash:x.bin bootflash-2:`) [C5].
- `software auto-upgrade enable` / `install autoupgrade` upgrade a mismatched member or standby when the active is in install mode [C5].
- Member state: `show switch` (`V-Mismatch` = different software) [C5].
- Staged options: ISSU (`install add file ... activate issu commit`, no confirmation prompt) on 9400/9500/9600 dual-sup/StackWise Virtual; "9200 and 9300 do not support ISSU. 9300 does support reloadfast" [C5]. ReloadFast (`... activate reloadfast commit`, 16.8.1+, install mode only, all members reload together, needs a single uplink to the STP root, not with microcode/FPGA upgrades) [C5]. xFSU on 9300 exists (doc referenced, not read) [C5].

### B.7 After reload / cleanup

- Confirm with `show version` and `show install summary` (`C` state) [C4][C5].
- `install remove inactive` lists files, asks `Do you want to remove the above files? [y/n]`, ends `SUCCESS: install_remove <date>`; it removes all `.bin` files too, so run it before copying the new `.bin` [C4][C5].
- Durations: in the 17.12 example the add/activate/commit took ~8 min before the reload (10:15:02 -> 10:22:59), including a bootloader upgrade [C4]. Reload time itself not documented; stacks wait "120 seconds for other switches to boot" [C4].

### B.8 Hazards

- ROMMON upgrades automatically during the first boot of a newer release (`%IOSXEBOOT-4-BOOTLOADER_UPGRADE: ... PLEASE DO NOT POWER CYCLE ### BOOT LOADER UPGRADING`); golden ROMMON is manual (`upgrade rom-monitor capsule golden switch`) [C4]. Do not time out and power-cycle.
- Changing the boot statement mid-install can fail the upgrade; "Do not change your boot statement to an inactive package" [C5].
- Microcode for UPOE 9300 models must be rolled back when downgrading below 16.12 with bundle boot (`hw-module mcu rollback`) [C4].
- New models cannot downgrade below their introduction release [C4].
- `packages.conf` corruption after update reported by users (community; UNVERIFIED).

### B.9 ISR 1100 and 9800 WLC (brief)

- Not researched in detail. UNVERIFIED (medium): both use `install add file ... activate commit` in install mode; 9800 adds N+1/AP rolling upgrade and HA-SSO ISSU; Cisco TAC doc 221693 covers 9800 ISSU [C12 title only].

---

## C. AOS-CX (6000/6100/6200/6300, VSF)

### C.1 Current state

- `show images` [X1] (6300 example):
  ```
  AOS-CX Primary Image
  Version : FL.xx.xx.xxxx
  Size    : 722 MB
  Date    : 2019-10-22 17:00:46 PDT
  SHA-256 : 4c84e49c0961fc56b5c7eab064750a333f1050212b7ce2fab587d13469d24cfa
  AOS-CX Secondary Image
  Version : FL.xx.xx.xxxx
  ...
  Default Image : secondary
  Management Module 1/1 (Active)
  Active Image       : secondary
  Service OS Version : FL.01.05.0001-internal
  BIOS Version       : FL.01.0001
  ```
  `Default Image` = next boot; `Active Image` = running bank; operators may run it [X1].
- `show version` for the running version (see aruba-cli.md). 

### C.2 Free space

- Images go to fixed primary/secondary banks, not a user file system, so no free-space check is documented for `copy ... primary`. UNVERIFIED (medium) that space can never be a failure cause.

### C.3 Getting the image onto the device

Syntax (10.13 and 10.15): `copy <REMOTE-URL> {hot-patch|primary|secondary} [vrf <VRF-NAME>]`, VRF default `default`; URL forms `tftp://<IP>[:<PORT>][;blocksize=<n>]/<FILE>`, `sftp://<USER>@<IP>[:<PORT>]/<FILE>`, `scp://USER@{IP|HOST}[:PORT]/FILE` [X2][X3]. Use `vrf mgmt` for the OOBM port.
- (a) TFTP example [X2]:
  ```
  switch# copy tftp://192.10.12.0/FL_10_12_0001.swi primary
  The primary image will be deleted. Continue (y/n)? y
  ####################################################### 100.0%
  Verifying and writing system firmware...
  ```
- (b) HTTP/HTTPS: not in the CLI `copy` URL list [X2][X3]. The REST API has `PUT /firmware` "Update firmware from remote HTTP server" (developer portal title only) [X8]; UNVERIFIED details.
- (c) SFTP/SCP pull: SFTP example adds host-key and password prompts: `Are you sure you want to continue connecting (yes/no)?`, `<user>@<ip>'s password:` then `Fetching ...`, `100% 179MB 25.6MB/s 00:07`, `Verifying and writing system firmware...` [X2]. A driver must answer the host-key prompt and send the server password through the CLI.
- (d) Client push: `ssh sftp-server [vrf <VRF>]` (10.15+) enables an SFTP server whose files land in a local storage area, then `copy local:/image.swi secondary` [X4][X5]. The command page lists Platforms: **9300 only** in both 10.15 and 10.17 guides for 5420/6200/6300/6400 [X5]. So for 6000/6100/6200/6300 there is no documented SCP/SFTP push; a 2024 community thread asks for it and gets none [X9]. Alternative push: REST `POST /rest/v10.xx/firmware?image=secondary` with `multipart/form-data` (community examples; UNVERIFIED (medium)) [X8].
- Failure output texts (bad signature, timeout, unreachable server): not found. "The switch prevents the download of firmware without a valid signature" [X6].

### C.4 Verifying

- Signature checked at download and every boot (RSA-3072, SHA-256) [X6].
- `show images verify primary` -> `The primary image is valid`; failure example `The image does not contain a signature` [X6].
- `show images` gives SHA-256 of each bank [X1]; compare with the SHA-256 of the local `.swi`. Whether that hash equals `sha256sum` of the downloaded file is UNVERIFIED (medium).
- No MD5/other on-box hash command found.

### C.5 Activating

- `boot system [primary | secondary | serviceos]` reboots all modules and (for primary/secondary) sets the default [X7]. Prompts [X7]:
  ```
  switch# boot system secondary
  Default boot image set to secondary.
  Do you want to save the current configuration (y/n)? n
  This will reboot the entire switch and render it unavailable until the process is complete.
  Continue (y/n)? y
  The system is going down for reboot.
  ```
  Save prompt appears only if config changed; a failed save aborts the boot; answering `n` to Continue prints `Reboot aborted.` [X7].
- `boot set-default {primary|secondary}` changes the default without rebooting [X7].
- Release notes: preview component updates with `boot system <bank>` and answer `n`; output like `2 device(s) need to be updated during the boot process. The estimated update time is between 2 and 3 minute(s).`; `show needed-updates [next-boot [primary|secondary]]` (search-engine excerpt of 10.13/10.14 6300 RNs; exact wording UNVERIFIED (medium)) [X10].

### C.6 VSF stacks

- Image is downloaded to the conductor and "synced with other members of the stack automatically"; then `boot system` reboots the whole stack [X11][X12]. "Adding or rebooting individual members before the upgrade process is complete can cause the individual member to fail while joining the stack." [X12]
- A joiner on a different version is pushed the conductor's firmware, reboots and joins; not across the 10.06/10.07 boundary [X11][X12].
- Single member reboot: `vsf member <id> reboot` [X12].
- State: `show vsf` member table `Mbr ID / Mac Address / type / Status` with `Conductor`, `Standby`, `Member`, `Booting`, `Not Present`; `Topology : Ring|Chain` [X13].
- VSF ISSU (6300 only, minor releases, not S3L75A/76A/77A, ring topology required): stage image in the alternate bank, then `issu update-software [validate|confirm]`; prompt `Continue (y/n)?`; rollback timer (example 30 min) needs `issu update-software confirm` or the switch reverts; chain topology prints `Stack topology is not a ring. ISSU upgrade aborted.`; monitor with `show issu` [X14][X15]. Not supported on 6200 [X15 search excerpt].

### C.7 After reload

- Confirm with `show images` (`Active Image`) and `show version`. Durations not documented beyond the component-update estimate in C.5.

### C.8 Hazards

- Downgrade: CMF does not migrate a newer startup-config to an older build; supported path is to create a checkpoint before upgrading, then set startup-config from it before downgrading [X16]. RNs: use `show checkpoint` to find one matching the target version, copy it to startup-config, boot the older bank and answer no to the save prompt (search excerpt) [X10].
- Answering `y` to `Do you want to save the current configuration` before a downgrade writes the newer config (inferred from [X16]; UNVERIFIED (medium)).
- Boot fails into ServiceOS on `Error: Signature verification failed` / `Error: Signature not found` / `Error: Invalid signature` [X6].

---

## D. AOS-S / ArubaOS-Switch (2530, 2930F/M; VSF and backplane stacking)

### D.1 Current state

- `show flash` [S1]:
  ```
  Image           Size(Bytes) Date     Version Build #:
  -----           ----------  -------- ------- -------
  Primary Image   : 6771179   04/17/08 K.12.51 304
  Secondary Image : 7408949   11/06/08 K.12.43 123
  Boot Rom Version: K.12.12
  Default Boot    : Primary
  ```
  `Default Boot` = image used for the next boot [S2].
- `show system` "Firmware revision" line confirms the running version [S3]. `show version` also works per member [S6].

### D.2 Free space

- Fixed primary/secondary slots; no free-space step documented (UNVERIFIED (medium)).

### D.3 Getting the image onto the device

(a) TFTP: `copy tftp flash <ip-address> <remote-file> [primary | secondary]` (default primary) [S3]:
  ```
  switch# copy tftp flash 10.28.227.103 k0800.swi
  The primary OS Image will be deleted, continue [y/n]? y
  01431K
  Validating and Writing System Software to FLASH ...
  ```
  OOBM keyword (`oobm`) UNVERIFIED (high).
(b) HTTP/HTTPS: not found; UNVERIFIED (low).
(c) SFTP pull (`copy sftp flash ...`): UNVERIFIED (high); syntax not read.
(d) Client push: enable with `ip ssh filetransfer` (needs SSH configured) [S4][S5]. Write to `/os/primary` or `/os/secondary` (SFTP view shows `+---os | primary | secondary`) [S7].
  - Enabling it disables TFTP client and server: `Tftp and auto-tftp have been disabled.`; while it is on, `tftp` cannot be re-enabled (`SFTP must be disabled before enabling tftp.`) [S5][S8]. **TFTP pull (a) and SCP/SFTP push (d) are mutually exclusive.**
  - Without it: `IP file transfer not enabled on the switch` [S7].
  - "The switch supports one SFTP session or one SCP session at a time." [S7]
  - After the transfer returns, "the switch may take approximately a minute and half writing the image to flash"; poll `show flash` or the log `update: Primary Image updated.` [S7].
  - SFTP needs SSHv2; SFTP clients cannot create/remove files [S4][S7].

### D.4 Verifying

- No on-box hash command found. `verify signature flash primary|secondary` is cited by users (community; UNVERIFIED (medium)). Compare `show flash` version/size instead.

### D.5 Activating

- `boot set-default flash {primary | secondary}` prompts: `This command changes the location of the default boot. This command will change the default flash image to boot from <flash chosen>. Hereafter, 'reload' and 'boot' commands will boot from <flash chosen>. Do you want to continue [y/n]?` [S9].
- `boot system flash {primary | secondary}` boots the chosen image (both management modules on chassis) [S9][S10]; `reload` boots from the default flash and startup-config [S3]. Their save/continue prompt texts are UNVERIFIED (high) (e.g. `Device will be rebooted, do you want to continue [y/n]?`, `Do you want to save current configuration [y/n/^C]?`).
- Each flash can be paired with a config file: `boot system flash secondary config configK1243.cfg` [S1].

### D.6 Stacks

- Backplane stacking (2930M): load the image on the Commander; "the Commander installs the software on all the stack members"; `boot system` "causes the entire stack to be rebooted"; check each member via `member-context <n>` then `show flash`/`show version`; "Multiple versions of software are not supported across stack members." [S6].
- VSF (2930F): whole-fabric reboot only; `vsf sequential-reboot` "is not available on the 2930F"; VSF Fast Software Upgrade is 5400R-only (community; UNVERIFIED (medium)). Users report members not always receiving the image; check each member's flash (community).
- `show stacking`, `show vsf` outputs: not read (UNVERIFIED).

### D.7 After reload

- Confirm with `show flash`/`show system` per member [S3][S6]. Durations not documented.

### D.8 Hazards

- Boot ROM upgrades and intermediate-version requirements (e.g. older KB/WC releases): not researched; UNVERIFIED. Release notes list the BootROM version per release.
- Enabling `ip ssh filetransfer` silently removes TFTP for later jobs [S8].

---

## E. Python side

### E.i Netmiko 4.8.0 `file_transfer()` / `FileTransfer` (installed source)

- `FILE_TRANSFER_MAP` keys [N2 L379-398]: `aruba_os, arista_eos, ciena_saos, cisco_asa, cisco_ios, cisco_nxos, cisco_ioswlc, cisco_xe, cisco_xr, dell_os10, dell_sonic, extreme_exos, juniper_junos, linux, nokia_sros, mikrotik_routeros, ubiquiti_edgerouter, zpe_nodegrid` (+ `_ssh` aliases, L408-412). **No entry for `aruba_aoscx`, `aruba_procurve`, `aruba_osswitch` or `hp_procurve`.** `aruba_os` is the ArubaOS wireless controller class (`file_system="/mm/mynode"`, MD5 raises `NotImplementedError`, verification by size only) [N3 L60-213]. `FileTransfer()` raises `ValueError("Unsupported SCP device_type ...")` for other types [N2 L632-646].
- `cisco_ios`, `cisco_xe`, `cisco_ioswlc` -> `CiscoIosFileTransfer` -> `CiscoFileTransfer` -> `BaseFileTransfer` (no overrides) [N4 L266][N5 L82-85].
- Signature [N6 L52-65]:
  ```python
  file_transfer(ssh_conn, source_file, dest_file, file_system=None, direction="put",
                disable_md5=False, inline_transfer=False, overwrite_file=False,
                socket_timeout=10.0, progress=None, progress4=None, verify_file=None)
      -> {"file_exists": bool, "file_transferred": bool, "file_verified": bool}
  ```
  Flow [N6 L121-154]: `check_file_exists()`; if absent -> `verify_space_available()` (raises `ValueError("Insufficient space available on remote device")`) -> `transfer_file()` -> `verify_file()` (raises `ValueError("MD5 failure between source and destination files")`). Existing file without `overwrite_file` -> returns verified if MD5 matches, else `ValueError("File already exists and overwrite_file is disabled")`.
- `BaseFileTransfer.__init__(ssh_conn, source_file, dest_file, file_system=None, direction="put", socket_timeout=10.0, progress=None, progress4=None, hash_supported=True)` [N1 L75-115]. For cisco_ios/xe/xr with no `file_system` it calls `_autodetect_fs()` (sends `dir`, regex `Directory of (.*)/`, requires enable mode) [N4 L204-228]. On put it hashes the local file at construction time (MD5 over 512-byte reads, [N1 L321-341]; the `add_newline` branch is a no-op, L338).
- **SCP connection: a new SSH connection.** `SCPConn.establish_scp_conn()` builds a fresh `paramiko.SSHClient` from `_connect_params_dict()` and calls `connect()`, then `scp.SCPClient(transport, socket_timeout=..., progress=..., progress4=...)` [N1 L43-53][N7 L1076-1098, L1270-1283]. Host-key policy is `self.key_policy` from the Netmiko connection [N7 L1283]; a tool that pins host keys must make sure the second connection uses the same policy/known-hosts. Module docstring: "SCP requires a separate SSH connection for a control channel." [N1 L1-7]
- Space check: `dir <file_system>`, regex `(\d+) \w+ free`, `kbytes` x1000 [N1 L145-156]. Space must be strictly greater than file size [N1 L211].
- Exists check: `dir <fs>/<file>`; false on `Error opening`, `No such file or directory`, `Path does not exist`; true on `Directory of .*<file>`; anything else raises `ValueError("Unexpected output from check_file_exists")` [N1 L215-235].
- Size: third column of the `dir` line [N1 L254-283].
- MD5: `verify /md5 <fs>/<file>`, `read_timeout=300` [N1 L369-382]. Long images on slow switches can exceed 300 s (UNVERIFIED).
- Put: `scp_transfer_file(source, f"{fs}/{dest}")`, then closes the SCP connection "to get the file written (flush)" [N1 L399-404].
- `enable_scp(cmd="ip scp server enable")` / `disable_scp` send config sets [N1 L410-424]; `file_transfer()` does not call them.
- `inline_transfer=True` (TCL) is text-only and IOS/IOS-XE only [N6 L66-97].

### E.ii scp 0.16.2 `SCPClient`

- `SCPClient(transport, buff_size=16384, socket_timeout=10.0, progress=None, progress4=None, sanitize=_sh_quote, limit_bw=None)`; only one of `progress` (`(filename, size, sent)`) or `progress4` (`(filename, size, sent, peername)`), else `TypeError` [S-scp L116-152].
- `put(files, remote_path=b'.', recursive=False, preserve_times=False)` opens a session channel on the given transport (`transport.open_session()`, L288-293) and runs `exec_command(b"scp -t " + sanitize(remote_path))` [S-scp L174-211, L18]. So SCPClient can run on an existing transport; the extra connection is Netmiko's choice.
- `putfo(fl, remote_path, mode='0644', size=None)` for file objects [L213-238].
- `_sh_quote` leaves `[\w@%+=:,./~-]` unquoted, so `flash:/x.bin` and `/os/primary` pass through as is [L48-57].
- Each protocol step waits for a one-byte ack with `socket_timeout`; timeout raises `SCPException('Timeout waiting for scp response')`; `\x01` raises with the device's message [L408-426]. The final ack after the last data byte (L351) arrives only when the device has accepted the file; if a device writes flash before acking, 10 s may be too short (UNVERIFIED (medium)).
- `close()` does `shutdown_write()`, `recv_exit_status()`, `close()` and swallows `EOFError/OSError/SSHException` [L295-304].
- Progress is called after every `buff_size` chunk [L337-349].

### E.iii Paramiko SFTP per platform

- `SSHClient.open_sftp()` [P1 L590] / `SFTPClient.from_transport()` [P2 L141] need an `sftp` subsystem.
- IOS-XE: no SFTP subsystem ("searching for subsystem sftp for vty ... Session terminated") [C8]. IOS classic: same assumed, UNVERIFIED (high). Use SCP.
- AOS-CX 6000-6300: no documented SFTP/SCP server (SFTP server documented for 9300 only) [X5]. Neither works for push; use device-pull (TFTP/SFTP/SCP) or REST.
- AOS-S: both SFTP (SSHv2) and SCP work after `ip ssh filetransfer`, one session at a time, target `/os/primary|secondary` [S4][S7]. Whether `scp.SCPClient` (which sends `scp -t /os/secondary`) is accepted by the AOS-S SCP server is UNVERIFIED (medium); users report `scp file.swi admin@switch:/os/secondary` working (community).

---

## F. Summary table

| Platform | (a) TFTP pull | (b) HTTP(S) pull | (c) SCP/SFTP pull | (d) client push | On-box hash |
|---|---|---|---|---|---|
| IOS classic 2960-X/3560-CX | yes (`copy`, `archive download-sw`) [C2] | UNVERIFIED | UNVERIFIED | SCP (`ip scp server enable` + AAA exec authz + priv 15) [C7] | MD5 (`verify /md5`) [C3]; SHA512 UNVERIFIED |
| IOS-XE Cat9k/3850 | yes [C4] | `http:`/`https:` FS exist [C5], syntax UNVERIFIED | `scp:`/`sftp:` FS exist [C5], syntax UNVERIFIED | SCP only, no SFTP [C8] | MD5 [C9]; signed-image SHA1/SHA2 via `verify` [C9]; `/sha512` UNVERIFIED |
| AOS-CX 6000-6300 | yes, `vrf` option [X2] | not in CLI; REST only (UNVERIFIED) | yes (sftp://, scp://) [X2] | none documented (SFTP server 9300-only [X5]); REST upload UNVERIFIED | SHA-256 shown by `show images`; signature check `show images verify` [X1][X6] |
| AOS-S 2530/2930 | yes [S3] (disabled by `ip ssh filetransfer`) | UNVERIFIED | UNVERIFIED | SCP and SFTP to `/os/primary|secondary` [S7] | none found; `verify signature` UNVERIFIED |

---

## Not covered (session budget ran out)

- 3560-CX specifics (no doc read); IOS classic HTTP/SCP-pull syntax and prompts; `copy` overwrite prompt; reload prompt texts on IOS classic.
- IOS-XE: `verify /sha512` release support; HTTP/SCP pull prompts and VRF options; 9200 flash size limits; xFSU constraints; ISR 1100 and 9800 WLC details; typical reload durations.
- AOS-CX: failure message texts for `copy`; REST firmware upload exact API; reload durations; 6000/6100 differences; release-note upgrade-path requirements.
- AOS-S: `copy sftp flash` syntax; `oobm` keyword; `boot system`/`reload` prompt texts; `show vsf` / `show stacking` output; BootROM and intermediate-version requirements; `verify signature`.

## UNVERIFIED claims (index)

IOS classic: `System image file is` line; `dir` free-bytes format; `flash1:`/`flash2:` member names; copy prompts and overwrite prompt; `ip tftp source-interface` VRF fix; HTTP/HTTPS and SCP pull; second channel on existing transport; `verify /sha512`; `.bin` per-member copy; archive download-sw wording for stacks; no rolling upgrade; reload prompts; per-member `show version` table. IOS-XE: HTTP/SCP pull syntax; `Save? [yes/no]` prompt; kron restriction; `verify /sha512`; signed-image `verify` on Cat9k; packages.conf corruption; ISR1100/9800 details. AOS-CX: no space concern; REST `PUT/POST /firmware` details; `show images` SHA-256 equals file sha256; saving config before downgrade; `boot system` device-update preview wording. AOS-S: free space; `oobm`; HTTP and SFTP pull; `verify signature`; `boot system`/`reload` prompts; VSF whole-fabric-only and FSU 5400R-only; `show vsf`/`show stacking`; BootROM/intermediate versions; SCPClient accepted by AOS-S. Python: 300 s MD5 timeout adequacy; 10 s final-ack timeout; IOS classic SFTP absence.

## Sources

| Key | Source |
|---|---|
| C1 | Release Notes Catalyst 2960-X/XR, IOS 15.2(6)E — https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst2960x/software/15_2_6_e/release_notes/rn-1526e-2960x-xr.html |
| C2 | Consolidated Platform Command Reference 15.2(7)E (2960-X), System Management Commands — https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst2960x/software/15-2_7_e/command_reference/b_1527_2960x_cr/system_management_commands.html |
| C3 | MD5 File Validation (IOS 15.2S) — https://www.cisco.com/en/US/docs/ios-xml/ios/sys-image-mgmt/configuration/15-2s/sysimgmgmt-md5.html |
| C4 | Release Notes Catalyst 9300, IOS XE 17.12.x, Upgrading the Switch Software — https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst9300/software/release/17-12/release_notes/ol-17-12-9300/upgrading_the_switch_software.html |
| C5 | Upgrade Guide for Catalyst 9000 Switches (Doc 216231) — https://www.cisco.com/c/en/us/support/docs/switches/catalyst-9300-series-switches/216231-upgrade-guide-for-cisco-catalyst-9000-sw.html |
| C6 | Change Between Bundle/Install Mode on Catalyst 9000 (Doc 224890) — https://www.cisco.com/c/en/us/support/docs/switches/catalyst-9500-series-switches/224890-change-between-bundle-install-mode-on.html |
| C7 | Secure Copy configuration guide (IOS 15.1S) — https://www.cisco.com/en/US/docs/ios-xml/ios/sec_usr_ssh/configuration/15-1s/sec-secure-copy.html |
| C8 | Use the -O Option for SCP from OpenSSH 9.0 to IOS XE (Doc 220371) — https://www.cisco.com/c/en/us/support/docs/troubleshooting/220371-scp-from-clients-on-openssh9-0-to-ios-xe.html |
| C9 | Cisco IOS XE Software Integrity Assurance — https://sec.cloudapps.cisco.com/security/center/resources/ios_xe_integrity_assurance.html |
| C10 | Cisco blog: SHA512 checksums for all Cisco software — https://blogs.cisco.com/security/sha512-checksums-for-all-cisco-software |
| C11 | Command Reference IOS XE 17.12 (Catalyst 9300), System Management (`install`) — https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst9300/software/release/17-12/command_reference/b_1712_9300_cr/system_management_commands.html |
| C12 | Troubleshoot ISSU upgrade on C9800 (Doc 221693, title only) — https://www.cisco.com/c/en/us/support/docs/wireless/catalyst-9800-series-wireless-controllers/221693-troubleshoot-issu-upgrade-on-c9800.html |
| X1 | AOS-CX 10.15 Fundamentals 6300/6400, `show images` — https://arubanetworking.hpe.com/techdocs/AOS-CX/10.15/HTML/fundamentals_6300-6400/Content/SysHW_cmds/sho-ima-10.htm |
| X2 | AOS-CX 10.13 Fundamentals, `copy <REMOTE-URL>` — https://arubanetworking.hpe.com/techdocs/AOS-CX/10.13/HTML/fundamentals_8400/Content/Chp_Cfg_FW_mgt/FW_mgt_cmds/cop-rem-url.htm |
| X3 | AOS-CX 10.15 Fundamentals 5420/6200, `copy <REMOTE-URL>` — https://arubanetworking.hpe.com/techdocs/AOS-CX/10.15/HTML/fundamentals_5420-6200/Content/Chp_Cfg_FW_mgt/FW_mgt_cmds/cop-rem-url.htm |
| X4 | AOS-CX 10.15 Security, `show ssh sftp-server usage` — https://arubanetworking.hpe.com/techdocs/AOS-CX/10.15/HTML/security_832x-8400-9300-10000/Content/Chp_SSH_serv/SSH_serv_cmds/sho-sft-ser-usa.htm |
| X5 | AOS-CX 10.15 and 10.17 Security 5420/6200/6300/6400, `ssh sftp-server` — https://arubanetworking.hpe.com/techdocs/AOS-CX/10.17/HTML/security_5420-6200-6300-6400/Content/Chp_SSH_serv/SSH_serv_cmds/ssh-sftp-ser.htm |
| X6 | AOS-CX 10.15 Hardening Guide, Firmware Validation — https://arubanetworking.hpe.com/techdocs/AOS-CX/10.15/HTML/hardening/Content/Chp_hard-mgmt/firmw-valid.htm |
| X7 | AOS-CX 10.14 Fundamentals 6200, `boot system` — https://arubanetworking.hpe.com/techdocs/AOS-CX/10.14/HTML/fundamentals_6200/Content/Chp_Cfg_FW_mgt/Boot_cmds/boo-sys.htm |
| X8 | Aruba developer hub `post firmware` / `put firmware` (titles) and community threads — https://developer.arubanetworks.com/aoscx/reference/post_firmware , https://community.arubanetworks.com/discussion/upgrade-6300-via-rest-api |
| X9 | Community: ArubaOS-CX SCP server functionality — https://community.arubanetworks.com/discussion/arubaos-cx-scp-server-functionality |
| X10 | AOS-CX 10.13/10.14 release notes 6000/6100, 6300/6400 (search excerpts only) — https://arubanetworking.hpe.com/techdocs/AOS-CX/10.13/RN/rn_6300-6400_10-13-1030.pdf |
| X11 | AOS-CX 10.13 VSF Guide, Automated image sync — https://arubanetworking.hpe.com/techdocs/AOS-CX/10.13/HTML/vsf/Content/Chp_Cfg_Task/aut-ima-syn.htm |
| X12 | AOS-CX 10.14 VSF Guide, FAQ General — https://arubanetworking.hpe.com/techdocs/AOS-CX/10.14/HTML/vsf/Content/Chp_FAQ/faq-gen.htm |
| X13 | AOS-CX 10.13 VSF Guide, `show vsf` — https://arubanetworking.hpe.com/techdocs/AOS-CX/10.13/HTML/vsf/Content/VSF_cmds/sho-vsf.htm |
| X14 | AOS-CX 10.14 VSF Guide, `issu update-software` — https://arubanetworking.hpe.com/techdocs/AOS-CX/10.14/HTML/vsf/Content/VSF_cmds/issu-upd-soft.htm |
| X15 | AOS-CX 10.13 VSF Guide, VSF In-Service Software Upgrade — https://arubanetworking.hpe.com/techdocs/AOS-CX/10.13/HTML/vsf/Content/Chp_VSF_Enhan_Soft_Up/VSF-enhanc-upd.htm |
| X16 | AOS-CX 10.13 Fundamentals, Upgrade and downgrade scenarios — https://arubanetworking.hpe.com/techdocs/AOS-CX/10.13/HTML/fundamentals_8400/Content/Chp_Cfg_FW_mgt/upg-dgrd.htm |
| S1 | AOS-S 16.10 (2540) Saving the current configuration before a software upgrade — https://arubanetworking.hpe.com/techdocs/AOS-S/16.10/ATMG/YC/content/common%20files/sav-cur-cnf-bef-sof-upg.htm |
| S2 | AOS-S 16.10 Viewing which software version is in each flash image — https://arubanetworking.hpe.com/techdocs/AOS-S/16.10/MCG/KB/content/kb/vie-whi-sof-ver-is-eac-fla-ima.htm |
| S3 | AOS-S 16.10 (2930M/F) Downloading from a server to flash using TFTP (CLI) — https://arubanetworking.hpe.com/techdocs/AOS-S/16.10/MRG/WB/content/common%20files/dow-ser-fla-usi-tft-cli.htm |
| S4 | AOS-S 16.11 Using SCP and SFTP — https://arubanetworking.hpe.com/techdocs/AOS-S/16.11/MCG/KB/content/kb/usi-scp-sft.htm |
| S5 | AOS-S 16.11 Enabling SCP and SFTP — https://arubanetworking.hpe.com/techdocs/AOS-S/16.11/MCG/WC/content/common%20files/ena-scp-sft.htm |
| S6 | AOS-S 16.11 (2930M/F) Downloading new software to the stack — https://arubanetworking.hpe.com/techdocs/AOS-S/16.11/ATMG/WC/content/common%20files/dow-new-sof-sta-1.h.htm |
| S7 | AOS-S 16.10 (2930M/F) SCP/SFTP operating notes — https://arubanetworking.hpe.com/techdocs/AOS-S/16.10/MCG/WC/content/common%20files/scp-ope-not.htm |
| S8 | AOS-S 16.11 Disabling TFTP and auto-TFTP for enhanced security — https://arubanetworking.hpe.com/techdocs/AOS-S/16.11/MCG/WC/content/common%20files/dis-tft-aut-tft-enh-sec.htm |
| S9 | AOS-S 16.11 boot command — https://arubanetworking.hpe.com/techdocs/AOS-S/16.11/MCG/KB/content/kb/boo-com.htm |
| S10 | AOS-S 16.11 Setting the default flash for boot; 16.10 boot system flash — https://arubanetworking.hpe.com/techdocs/AOS-S/16.11/MCG/KB/content/kb/set-def-fla-boo.htm |
| N1 | `.venv/Lib/site-packages/netmiko/scp_handler.py` (Netmiko 4.8.0) |
| N2 | `.venv/Lib/site-packages/netmiko/ssh_dispatcher.py` |
| N3 | `.venv/Lib/site-packages/netmiko/aruba/aruba_os.py` |
| N4 | `.venv/Lib/site-packages/netmiko/cisco_base_connection.py` |
| N5 | `.venv/Lib/site-packages/netmiko/cisco/cisco_ios.py` |
| N6 | `.venv/Lib/site-packages/netmiko/scp_functions.py` |
| N7 | `.venv/Lib/site-packages/netmiko/base_connection.py` |
| S-scp | `.venv/Lib/site-packages/scp.py` (scp 0.16.2) |
| P1 | `.venv/Lib/site-packages/paramiko/client.py` (Paramiko 5.0.0) |
| P2 | `.venv/Lib/site-packages/paramiko/sftp_client.py` |
