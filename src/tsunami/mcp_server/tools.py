"""The MCP tools, as thin wrappers over core.

Tier 0 touches only the workspace. Tier 1 contacts devices read-only: the engine uses
credentials, and Claude sees only redacted output. Tier 2 requests changes, which wait
in history.db for a person to approve them in the GUI, unless the customer is a lab and
the policy allows direct apply. Every device connection made here refuses unknown host
keys: trust on first use is a GUI action.

See design.md, MCP interface: Tools, Approval flow, Leak paths.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from pathlib import Path, PureWindowsPath
from typing import Any

from tsunami.core.issues import Issue, Severity, TsunamiError
from tsunami.core.models.common import PLATFORM_FAMILY, Platform, PlatformFamily
from tsunami.core.models.jobs import Actor, JobStatus
from tsunami.core.models.play import ConnectionMethod, PlayType, RunMode
from tsunami.core.pathguard import MAX_DRAFT_BYTES, resolve_draft_write, resolve_read
from tsunami.core.policy import PolicyError, check_show_command
from tsunami.core.redact import supplied_values
from tsunami.core.render.environment import TemplateError, compile_template, make_environment
from tsunami.core.render.lookup import find_snippet
from tsunami.core.runner import Runner, RunRequest, play_hash
from tsunami.core.services import play_entry, render_play
from tsunami.core.showfilter import FilterError, apply_filter, compile_pattern, split_command
from tsunami.core.targets import resolve_targets
from tsunami.core.transports.base import ConnectTarget, JumpTarget
from tsunami.core.workspace import validate_play_text
from tsunami.core.yamlio import YamlLoadError, load_yaml_text
from tsunami.mcp_server.enforcement import McpApp, ToolRefused

PLAY_DIRS = ("plays", "drafts/plays")


class Tools:
    def __init__(self, app: McpApp) -> None:
        self.app = app
        self._cancels: dict[str, threading.Event] = {}

    # == tier 0: workspace only ======================================================

    def list_customers(self) -> dict[str, Any]:
        def run(policy):
            ws = self.app.workspace()
            return {
                "customers": [
                    {
                        "slug": slug,
                        "name": cust.config.name,
                        "lab": cust.config.lab,
                        "device_count": len(cust.devices),
                    }
                    for slug, cust in ws.customers.items()
                    if slug not in policy.customers_blocked
                ]
            }

        return self.app.call("list_customers", 0, {}, run)

    def list_devices(self, customer: str, site: str | None = None, role: str | None = None):
        def run(policy):
            cust = self.app.visible_customer(policy, self.app.workspace(), customer)
            return {
                "devices": [
                    {
                        "name": d.name,
                        "host": d.host,
                        "site": d.site,
                        "role": d.role,
                        "platform": str(d.platform),
                        "model": d.model,
                    }
                    for d in cust.devices
                    if (site is None or d.site == site) and (role is None or d.role == role)
                ]
            }

        args = {"customer": customer, "site": site, "role": role}
        return self.app.call("list_devices", 0, args, run, customer=customer)

    def list_plays(
        self,
        type: str | None = None,
        customer: str | None = None,  # noqa: A002
        include_drafts: bool = False,
    ):
        def run(policy):
            ws = self.app.workspace()
            plays = []
            for path, entry in ws.plays.items():
                play = entry.play
                if entry.draft and not include_drafts:
                    continue
                if play is not None and play.customer in policy.customers_blocked:
                    continue
                if customer and (play is None or play.customer != customer):
                    continue
                if type and (play is None or play.type != type):
                    continue
                issues = ws.issues_for(path)
                plays.append(
                    {
                        "path": path.as_posix(),
                        "name": play.name if play else None,
                        "type": str(play.type) if play else None,
                        "customer": play.customer if play else None,
                        "draft": entry.draft,
                        "errors": sum(i.severity == Severity.ERROR for i in issues),
                        "warnings": sum(i.severity == Severity.WARNING for i in issues),
                    }
                )
            return {"plays": plays}

        args = {"type": type, "customer": customer, "include_drafts": include_drafts}
        known = self._drafts() if include_drafts else []
        return self.app.call("list_plays", 0, args, run, customer=customer, known=known)

    def read_play(self, path: str):
        def run(policy):
            ws = self.app.workspace()
            real = resolve_read(ws.root, path, allowed=PLAY_DIRS)
            text = _read_text(real)
            self._check_play_customer(policy, ws, text)
            return {"path": _rel(ws.root, real), "text": text}

        customer, known = self._customer_of(path), self._drafts_for(path)
        return self.app.call("read_play", 0, {"path": path}, run, customer=customer, known=known)

    def list_snippets(self, customer: str | None = None, platform: str | None = None):
        def run(policy):
            ws = self.app.workspace()
            if customer:
                self.app.visible_customer(policy, ws, customer)
            files = sorted((ws.root / "snippets").rglob("*.j2"))
            names = sorted({f.stem for f in files})
            if customer and platform:
                plat = _platform(platform)
                found = []
                for name in names:
                    hit = find_snippet(ws.root, name, customer=customer, platform=plat)
                    if hit is not None:
                        found.append({"name": name, "path": hit.as_posix(), "level": _level(hit)})
                return {"snippets": found}
            listing = []
            for f in files:
                rel = f.relative_to(ws.root)
                if rel.parts[1] == "customers" and (
                    rel.parts[2] in policy.customers_blocked
                    or (customer and rel.parts[2] != customer)
                ):
                    continue
                listing.append({"name": f.stem, "path": rel.as_posix(), "level": _level(rel)})
            return {"snippets": listing}

        args = {"customer": customer, "platform": platform}
        return self.app.call("list_snippets", 0, args, run, customer=customer)

    def read_snippet(self, name: str, customer: str | None = None, platform: str | None = None):
        def run(policy):
            ws = self.app.workspace()
            plat = _platform(platform or (ws.config.defaults.platform if ws.config else "ios-xe"))
            if customer:
                self.app.visible_customer(policy, ws, customer)
                hit = find_snippet(ws.root, name, customer=customer, platform=plat)
            else:
                family = PLATFORM_FAMILY[plat]
                candidates = [
                    Path("snippets", "platform", plat, f"{name}.j2"),
                    Path("snippets", "common", family, f"{name}.j2"),
                ]
                hit = next((c for c in candidates if (ws.root / c).is_file()), None)
            if hit is None:
                raise ToolRefused(f"snippet {name!r} not found for {plat}")
            real = resolve_read(ws.root, hit.as_posix(), allowed=("snippets",))
            return {"path": hit.as_posix(), "level": _level(hit), "content": _read_text(real)}

        args = {"name": name, "customer": customer, "platform": platform}
        return self.app.call("read_snippet", 0, args, run, customer=customer)

    def resolved_vars(self, customer: str, device: str):
        def run(policy):
            ws = self.app.workspace()
            cust = self.app.visible_customer(policy, ws, customer)
            if cust.device(device) is None:
                raise ToolRefused(f"unknown device {device!r}")
            resolved = ws.resolve_vars(customer, device=device)
            provenance = {
                ".".join(path): {
                    "level": source.level.label,
                    "file": source.file.as_posix() if source.file else None,
                }
                for path, source in resolved.provenance.items()
            }
            return {"values": resolved.values, "provenance": provenance}

        args = {"customer": customer, "device": device}
        return self.app.call("resolved_vars", 0, args, run, customer=customer)

    def validate(self, path: str | None = None, content: str | None = None):
        def run(policy):
            if (path is None) == (content is None):
                raise ToolRefused("give exactly one of path or content")
            ws = self.app.workspace()
            if path is not None:
                real = resolve_read(ws.root, path, allowed=PLAY_DIRS)
                text, rel = _read_text(real), Path(_rel(ws.root, real))
            else:
                text, rel = content, Path("drafts/plays/push/unsaved.yml")
            self._check_play_customer(policy, ws, text)
            issues = validate_play_text(ws, text, rel)
            return _issues_result(issues)

        args = {"path": path, "content": _short(content)}
        customer, known = self._customer_of(path, content), self._drafts_for(path)
        known += [content] if content is not None else []  # args hold only a prefix
        return self.app.call("validate", 0, args, run, customer=customer, known=known)

    def render(self, path: str, device: str | None = None, prompts: dict | None = None):
        def run(policy):
            ws = self.app.workspace()
            real = resolve_read(ws.root, path, allowed=PLAY_DIRS)
            rel = _rel(ws.root, real)
            self._check_play_customer(policy, ws, _read_text(real))
            rendering = render_play(ws, rel, device=device, prompts=prompts or {})
            return {
                "path": rel,
                "device": device,
                "platform": str(rendering.platform),
                "lines": rendering.masked_lines(),
                "text": rendering.masked_text(),
                "secret_names": list(rendering.secret_refs),
                "render_hash": rendering.render_hash(),
            }

        args = {"path": path, "device": device, "prompts": prompts}
        customer, known = self._customer_of(path), self._drafts_for(path)
        return self.app.call("render", 0, args, run, customer=customer, known=known)

    def write_draft(self, path: str, content: str):
        def run(policy):
            if not isinstance(content, str) or "\x00" in content:
                raise ToolRefused("content must be text")
            if len(content.encode("utf-8")) > MAX_DRAFT_BYTES:
                raise ToolRefused(f"a draft is limited to {MAX_DRAFT_BYTES // 1024} KB")
            ws = self.app.workspace()
            target = resolve_draft_write(ws.root, path)
            rel = Path(_rel(ws.root, target))
            if target.suffix == ".yml":
                self._check_play_customer(policy, ws, content)
            elif rel.parts[2] == "customers":
                self.app.visible_customer(policy, ws, rel.parts[3])
            _atomic_write(target, content)
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            self.app.history.record_mcp_draft(rel.as_posix(), digest)
            if target.suffix == ".yml":
                result = _issues_result(validate_play_text(self.app.workspace(), content, rel))
            else:
                result = _template_check(content, rel)
            return {"path": rel.as_posix(), "written": True, **result}

        args = {"path": path, "content": _short(content)}
        customer = self._customer_of(None, content)
        return self.app.call("write_draft", 0, args, run, customer=customer, known=[content])

    def delete_draft(self, path: str):
        def run(policy):
            ws = self.app.workspace()
            target = resolve_draft_write(ws.root, path)
            if not target.is_file():
                raise ToolRefused(f"no draft at {path}")
            target.unlink()
            self.app.history.forget_mcp_draft(_rel(ws.root, target))
            return {"path": _rel(ws.root, target), "deleted": True}

        return self.app.call("delete_draft", 0, {"path": path}, run)

    # == tier 1: read-only device access =================================================

    def check(self, path: str, targets: list[str] | None = None, prompts: dict | None = None):
        return self._read_only_job("check", RunMode.CHECK, path, targets, prompts)

    def compliance(self, path: str):
        return self._read_only_job("compliance", RunMode.COMPLIANCE, path, None, None)

    def show(self, customer: str, device: str, command: str):
        def run(policy):
            ws = self.app.workspace()
            cust = self.app.visible_customer(policy, ws, customer)
            dev = cust.device(device)
            if dev is None:
                raise ToolRefused(f"unknown device {device!r}")
            try:
                normalized = check_show_command(policy, PLATFORM_FAMILY[dev.platform], command)
                parts = split_command(normalized)
                if parts.pattern is not None:
                    compile_pattern(parts.pattern)  # refuse a bad pattern before connecting
            except (PolicyError, FilterError) as exc:
                raise ToolRefused(str(exc)) from None
            target = ConnectTarget(
                customer=customer,
                device=dev.name,
                host=dev.host,
                port=dev.port,
                platform=dev.platform,
                method=ConnectionMethod.SSH,
                credential=self.app.secrets.get_login(cust.config.credential),
                jump=self._jump(cust),
                allow_new_host_key=False,
            )
            transport = self.app.transport()
            session = transport.open(target)
            try:
                output = session.send_exec(parts.base).output
            finally:
                session.close()
                close = getattr(transport, "close", None)
                if callable(close):
                    close()
            # The pipe runs here, on fully redacted text. On the device it would see the
            # secrets, and which lines came back would reveal them (showfilter.py). The
            # command is not echoed back: Claude knows what it sent.
            output = apply_filter(self.app.full_redactor().text(output), parts)
            return {"device": dev.name, "untrusted_device_output": output}

        args = {"customer": customer, "device": device, "command": command}
        return self.app.call("show", 1, args, run, customer=customer)

    def get_job(self, job_id: str):
        def run(policy):
            job = self._visible_job(policy, job_id)
            return {
                "job_id": job.id,
                "play": job.play_path,
                "customer": job.customer,
                "mode": job.mode,
                "status": str(job.status),
                "actor": str(job.actor),
                "created_at": job.created_at.isoformat(),
                "approved_by": job.approved_by,
                "summary": job.summary,
                "devices": [
                    {
                        "device": d.device,
                        "status": str(d.status),
                        "failed_line": d.failed_line,
                        "error": d.error_redacted,
                        "rollback_status": d.rollback_status,
                        "render": d.render_masked,
                        "untrusted_device_output": {
                            "output": d.output_redacted,
                            "diff": d.diff_redacted,
                        },
                    }
                    for d in self.app.history.devices(job_id)
                ],
            }

        args = {"job_id": job_id}
        return self.app.call("get_job", 1, args, run, known=self._job_supplied(job_id))

    def list_jobs(self, customer: str | None = None, limit: int = 20):
        def run(policy):
            jobs = self.app.history.list_jobs(customer=customer, limit=max(1, min(limit, 200)))
            return {
                "jobs": [
                    {
                        "job_id": j.id,
                        "created_at": j.created_at.isoformat(),
                        "actor": str(j.actor),
                        "play": j.play_path,
                        "customer": j.customer,
                        "mode": j.mode,
                        "status": str(j.status),
                    }
                    for j in jobs
                    if j.customer not in policy.customers_blocked
                ]
            }

        args = {"customer": customer, "limit": limit}
        return self.app.call("list_jobs", 1, args, run, customer=customer)

    # == tier 2: change requests =========================================================

    def request_run(
        self,
        path: str,
        targets: list[str],
        prompts: dict | None = None,
        tags: list[str] | None = None,
    ):
        def run(policy):
            ws = self.app.workspace()
            entry = self._entry(ws, path)
            play = entry.play
            cust = self.app.visible_customer(policy, ws, play.customer)
            if play.type == PlayType.INITIAL:
                raise ToolRefused("initial (day-0) plays run over a console cable from the GUI")
            if not targets:
                raise ToolRefused("targets must list at least one device")
            if len(targets) > policy.max_check_targets:
                raise ToolRefused(f"at most {policy.max_check_targets} targets per request")
            direct = cust.config.lab and policy.apply == "lab_only"
            if entry.draft and not (direct and policy.lab_allow_drafts):
                raise ToolRefused(
                    "drafts cannot run against devices; a person must promote it in the GUI"
                )
            self._check_targets(ws, cust, play, targets)
            job_id = self._create_job(entry.path, play.customer, "apply", prompts, tags, targets)
            if direct:
                self._background(
                    RunRequest(
                        entry.path,
                        RunMode.APPLY,
                        prompts=prompts or {},
                        tags=tags,
                        limit=targets,
                        actor=Actor.MCP,
                        job_id=job_id,
                        allow_new_host_key=False,
                        exact_exempt=self._exempt(entry, prompts),
                    )
                )
                return {
                    "job_id": job_id,
                    "status": "running",
                    "note": "lab customer: runs without approval; poll get_job",
                }
            self._background(
                RunRequest(
                    entry.path,
                    RunMode.CHECK,
                    prompts=prompts or {},
                    tags=tags,
                    limit=targets,
                    actor=Actor.MCP,
                    job_id=job_id,
                    allow_new_host_key=False,
                    exact_exempt=self._exempt(entry, prompts),
                ),
                after=lambda: self._await_approval(job_id),
            )
            return {
                "job_id": job_id,
                "status": "preparing",
                "note": "Tsunami renders and diffs each device, then the request waits for a "
                "person to approve it in the Tsunami GUI. Show the user the diff from get_job.",
            }

        args = {"path": path, "targets": targets, "prompts": prompts, "tags": tags}
        return self.app.call("request_run", 2, args, run, customer=self._customer_of(path))

    def cancel_job(self, job_id: str):
        def run(policy):
            current = self._visible_job(policy, job_id)
            if current.actor != Actor.MCP:
                raise ToolRefused("only jobs requested over MCP can be cancelled here")
            if current.status not in (JobStatus.PENDING, JobStatus.AWAITING_APPROVAL):
                raise ToolRefused(
                    f"job is {current.status}; only jobs not yet started can be cancelled"
                )
            event = self._cancels.get(job_id)
            if event is not None:
                event.set()
            self.app.history.set_job_status(job_id, JobStatus.CANCELLED)
            return {"job_id": job_id, "status": "cancelled"}

        args = {"job_id": job_id}
        return self.app.call("cancel_job", 2, args, run, known=self._job_supplied(job_id))

    # == helpers ===========================================================================

    def _read_only_job(self, tool, mode, path, targets, prompts):
        def run(policy):
            ws = self.app.workspace()
            entry = self._entry(ws, path)
            play = entry.play
            cust = self.app.visible_customer(policy, ws, play.customer)
            if play.type == PlayType.INITIAL:
                raise ToolRefused("initial (day-0) plays have no inventory targets to check")
            if mode == RunMode.COMPLIANCE and play.type != PlayType.BASELINE:
                raise ToolRefused("compliance is for baseline plays")
            if entry.draft and not (cust.config.lab and policy.lab_allow_drafts):
                raise ToolRefused("drafts can only be checked against lab customers")
            devices = self._check_targets(ws, cust, play, targets)
            if len(devices) > policy.max_check_targets:
                raise ToolRefused(f"at most {policy.max_check_targets} devices per check")
            job_id = self._create_job(entry.path, play.customer, str(mode), prompts, None, targets)
            self._background(
                RunRequest(
                    entry.path,
                    mode,
                    prompts=prompts or {},
                    limit=targets,
                    actor=Actor.MCP,
                    job_id=job_id,
                    allow_new_host_key=False,
                    exact_exempt=self._exempt(entry, prompts),
                )
            )
            return {"job_id": job_id, "status": "running", "devices": len(devices)}

        args = {"path": path, "targets": targets, "prompts": prompts}
        return self.app.call(tool, 1, args, run, customer=self._customer_of(path))

    def _entry(self, ws, path: str):
        real = resolve_read(ws.root, path, allowed=PLAY_DIRS)
        return play_entry(ws, _rel(ws.root, real))

    def _check_targets(self, ws, cust, play, targets):
        result = resolve_targets(
            play.targets,
            cust.devices,
            sites=cust.sites.keys(),
            roles=ws.roles.keys(),
            limit=targets,
        )
        if not result.ok:
            raise ToolRefused("; ".join(f.message for f in result.findings))
        return result.devices

    def _create_job(self, path: Path, customer, mode, prompts, tags, targets) -> str:
        ws_root = Path(self.app.settings().workspace)
        return self.app.history.create_job(
            actor=Actor.MCP,
            play_path=path.as_posix(),
            play_hash=play_hash(ws_root / path),
            customer=customer,
            mode=mode,
            status=JobStatus.PENDING,
            prompts=dict(prompts or {}),
            tags=list(tags) if tags else None,
            limit=list(targets) if targets else None,
        )

    def _background(self, request: RunRequest, after=None) -> None:
        cancel = threading.Event()
        self._cancels[request.job_id] = cancel

        def work() -> None:
            try:
                runner = Runner(
                    self.app.workspace(),
                    transport=self.app.transport(),
                    history=self.app.history,
                    secrets=self.app.secrets,
                    paths=self.app.paths,
                    app_settings=self.app.settings(),
                    confirm_canary=lambda device: True,
                    cancel=cancel,
                )
                runner.run(request)
                if after is not None and not cancel.is_set():
                    after()
            finally:
                self._cancels.pop(request.job_id, None)

        thread = threading.Thread(target=work, name=f"mcp-{request.job_id[:8]}", daemon=True)
        self.app.threads.append(thread)
        thread.start()

    def _await_approval(self, job_id: str) -> None:
        job = self.app.history.get_job(job_id)
        if job.status == JobStatus.COMPLETED:
            self.app.history.set_job_status(job_id, JobStatus.AWAITING_APPROVAL)

    def _jump(self, cust) -> JumpTarget | None:
        jh = cust.config.jump_host
        if jh is None:
            return None
        return JumpTarget(jh.host, jh.port, self.app.secrets.get_login(jh.credential))

    def _visible_job(self, policy, job_id: str):
        """The job, or ToolRefused if unknown or its customer is blocked (indistinguishable)."""
        try:
            job = self.app.history.get_job(job_id)
        except TsunamiError:
            job = None
        if job is None or job.customer in policy.customers_blocked:
            raise ToolRefused(f"unknown job {job_id!r}")
        return job

    def _job_supplied(self, job_id: str) -> list[str]:
        """What Claude supplied for an MCP job: its prompt values, and the draft it ran."""
        try:
            job = self.app.history.get_job(job_id)
        except TsunamiError:
            return []
        if job.actor != Actor.MCP:
            return []  # a person typed these; Claude never saw them
        return [str(v) for v in job.prompts.values()] + self._drafts_for(job.play_path)

    def _exempt(self, entry, prompts) -> frozenset[str]:
        """Text Claude supplied for a run, kept out of the runner's exact-value layer."""
        known = list((prompts or {}).values()) + (self._drafts() if entry.draft else [])
        return frozenset(supplied_values(known))

    def _drafts_for(self, path: str | None) -> list[str]:
        if path and PureWindowsPath(path).as_posix().startswith("drafts/"):
            return self._drafts()
        return []

    def _drafts(self) -> list[str]:
        """Path, name and text of each draft Claude wrote over MCP and nobody changed since.

        When Claude reads its own text back, an exact-value redaction would answer whether
        the text it wrote is a secret. A draft a person saved or edited is not Claude's
        input, so it is redacted like any other file.
        """
        try:
            root = Path(self.app.settings().workspace or "")
            written = self.app.history.mcp_draft_hashes()
        except Exception:  # noqa: BLE001
            return []
        out: list[str] = []
        for rel, digest in written.items():
            file = root / rel
            try:
                data = file.read_bytes()
            except OSError:
                continue
            if hashlib.sha256(data).hexdigest() != digest:
                continue
            out += [rel, file.stem, data.decode("utf-8-sig", errors="replace")]
        return out

    def _check_play_customer(self, policy, ws, text: str) -> None:
        customer = _yaml_customer(text)
        if customer is not None:
            self.app.visible_customer(policy, ws, customer)

    def _customer_of(self, path: str | None, content: str | None = None) -> str | None:
        """The play's customer, for exact-value redaction; None if it cannot be read."""
        try:
            if content is not None:
                return _yaml_customer(content)
            if path is None:
                return None
            ws_root = Path(self.app.settings().workspace or "")
            return _yaml_customer(_read_text(resolve_read(ws_root, path, allowed=PLAY_DIRS)))
        except Exception:  # noqa: BLE001
            return None


def _yaml_customer(text: str) -> str | None:
    try:
        data = load_yaml_text(text, Path("x.yml")).data
    except YamlLoadError:
        return None
    customer = data.get("customer") if isinstance(data, dict) else None
    return customer if isinstance(customer, str) else None


def _platform(value: str) -> Platform:
    try:
        return Platform(value)
    except ValueError:
        raise ToolRefused(f"unknown platform {value!r}") from None


def _level(path: Path) -> str:
    parts = path.parts
    return {"customers": "customer", "platform": "platform", "common": "common"}.get(
        parts[1] if parts[0] == "snippets" else parts[2], "unknown"
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ToolRefused("no such file") from None


def _rel(root: Path, real: Path) -> str:
    return real.relative_to(Path(root).resolve()).as_posix()


def _short(text: str | None) -> str | None:
    """Audit rows record a prefix of long text arguments, not all of it."""
    if text is None:
        return None
    return text if len(text) <= 200 else text[:200] + f"... ({len(text)} chars)"


def _atomic_write(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _issue_dict(issue: Issue) -> dict[str, Any]:
    return {
        "severity": str(issue.severity),
        "code": issue.code,
        "message": issue.message,
        "file": issue.file.as_posix() if issue.file else None,
        "line": issue.line,
    }


def _issues_result(issues: list[Issue]) -> dict[str, Any]:
    return {
        "valid": not any(i.severity == Severity.ERROR for i in issues),
        "issues": [_issue_dict(i) for i in issues],
    }


def _template_check(content: str, rel: Path) -> dict[str, Any]:
    try:
        compile_template(make_environment(), content, name=rel.as_posix())
    except TemplateError as exc:
        issue = Issue(Severity.ERROR, "template.syntax", str(exc), rel, exc.line)
        return _issues_result([issue])
    return _issues_result([])


__all__ = ["PlatformFamily", "Tools"]
