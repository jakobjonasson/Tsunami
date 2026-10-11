"""Workspace loader and lint.

Loads workspace.yml, the inventory, every play and draft play, and checks each file
against its model, then checks the rules that span files: devices name real sites and
roles, plays name a real customer, targets resolve, snippets exist for every target's
family. It never stops at the first problem: the result holds every Issue, with its
workspace-relative file and line.

See design.md, Workspace layout; Inventory and variables; Play schema: Cross-field
validation; Hardware catalog.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from tsunami.core.catalog import load_catalog
from tsunami.core.issues import Finding, Issue, Severity, TsunamiError
from tsunami.core.models.common import (
    PLATFORM_FAMILY,
    SLUG_PATTERN,
    SUPPORTED_PLATFORMS,
    Platform,
    PlatformFamily,
)
from tsunami.core.models.inventory import CustomerFile, Device, VarsFile
from tsunami.core.models.play import Play, PlayType, PromptType, play_findings
from tsunami.core.models.workspace import SCHEMA_VERSION, WorkspaceFile
from tsunami.core.prompts import PromptContext, PromptValueError, coerce
from tsunami.core.render.lookup import find_snippet, snippet_paths
from tsunami.core.resolver import Layer, Level, ResolvedVars, resolve
from tsunami.core.targets import resolve_targets
from tsunami.core.yamlio import Loc, YamlDoc, YamlLoadError, load_yaml, validation_issues

M = TypeVar("M", bound=BaseModel)

_SLUG_RE = re.compile(SLUG_PATTERN)
_SECRET_KEY_RE = re.compile(r"password|secret|key|community|psk", re.IGNORECASE)
_TEMPLATED_RE = re.compile(r"\{\{|\{%")

GLOBAL_VARS = Path("inventory", "global.yml")
ROLES_DIR = Path("inventory", "roles")
CUSTOMERS_DIR = Path("inventory", "customers")
PLAY_DIRS = ((Path("plays"), False), (Path("drafts", "plays"), True))
SNIPPET_DIRS = (Path("snippets"), Path("drafts", "snippets"))


@dataclass(frozen=True)
class CustomerInventory:
    slug: str
    config: CustomerFile
    devices: tuple[Device, ...]
    sites: Mapping[str, Mapping[str, Any]]  # site slug -> vars

    def device(self, name: str) -> Device | None:
        return next((d for d in self.devices if d.name == name), None)


@dataclass(frozen=True)
class PlayEntry:
    path: Path  # workspace-relative
    draft: bool
    play: Play | None  # None when the file failed schema validation


@dataclass
class Workspace:
    root: Path
    config: WorkspaceFile | None
    global_vars: dict[str, Any] = field(default_factory=dict)
    roles: dict[str, dict[str, Any]] = field(default_factory=dict)
    customers: dict[str, CustomerInventory] = field(default_factory=dict)
    plays: dict[Path, PlayEntry] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    # Line maps of loaded plays, so later checks (validation renders) can report lines.
    play_docs: dict[Path, YamlDoc] = field(default_factory=dict, repr=False)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.WARNING]

    def issues_for(self, path: Path) -> list[Issue]:
        return [i for i in self.issues if i.file == Path(path)]

    def customer(self, slug: str) -> CustomerInventory:
        try:
            return self.customers[slug]
        except KeyError:
            raise TsunamiError(f"unknown customer: {slug!r}") from None

    def resolve_vars(
        self,
        customer: str,
        *,
        device: str | None = None,
        play: PlayEntry | None = None,
        prompt_values: Mapping[str, Any] | None = None,
    ) -> ResolvedVars:
        """Variables for one device, with provenance.

        Without a device (an initial play staging a new switch), site and role come from
        the play's vars or the prompt answers, as in the two-stage day-0 form.
        """
        cust = self.customer(customer)
        dev = None
        if device is not None:
            dev = cust.device(device)
            if dev is None:
                raise TsunamiError(f"unknown device {device!r} for customer {customer!r}")
            site, role = dev.site, dev.role
        else:
            answers = {**(play.play.vars if play and play.play else {}), **(prompt_values or {})}
            site, role = answers.get("site"), answers.get("role")

        cust_dir = CUSTOMERS_DIR / customer
        layers = [Layer(Level.GLOBAL, self.global_vars, GLOBAL_VARS)]
        if role in self.roles:
            layers.append(Layer(Level.ROLE, self.roles[role], ROLES_DIR / f"{role}.yml"))
        layers.append(Layer(Level.CUSTOMER, cust.config.vars, cust_dir / "customer.yml"))
        if role in cust.config.role_vars:
            layers.append(
                Layer(Level.CUSTOMER_ROLE, cust.config.role_vars[role], cust_dir / "customer.yml")
            )
        if site in cust.sites:
            layers.append(Layer(Level.SITE, cust.sites[site], cust_dir / "sites" / f"{site}.yml"))
        if dev is not None:
            layers.append(Layer(Level.DEVICE, dev.vars, cust_dir / "devices.yml"))
        if play is not None and play.play is not None:
            layers.append(Layer(Level.PLAY, play.play.vars, play.path))
        if prompt_values:
            layers.append(Layer(Level.PROMPT, prompt_values))

        if dev is None:
            return resolve(layers, builtins={"customer": customer})
        builtins = {
            "inventory_name": dev.name,
            "customer": customer,
            "site": dev.site,
            "role": dev.role,
            "platform": str(dev.platform),
            "host": dev.host,
        }
        return resolve(layers, defaults={"hostname": dev.name}, builtins=builtins)


def load_workspace(root: Path, *, render: bool = True) -> Workspace:
    """Load and validate a workspace. render=False skips the validation renders."""
    workspace = _Loader(Path(root)).load()
    if render and not any(i.code == "workspace.schema_too_new" for i in workspace.issues):
        from tsunami.core.render.renderer import validation_render_issues  # imports this module

        workspace.issues.extend(validation_render_issues(workspace))
        workspace.issues.sort(key=lambda i: (str(i.file), i.line or 0))
    return workspace


def validate_play_text(workspace: Workspace, text: str, path: Path | str) -> list[Issue]:
    """Validate play YAML that may not be on disk (the MCP validate and write_draft tools).

    Checks run against the loaded workspace's inventory, exactly as the loader would check a
    file at `path`; a path under drafts/ resolves draft snippets first.
    """
    from tsunami.core.render.renderer import entry_render_issues
    from tsunami.core.yamlio import load_yaml_text

    rel = Path(path)
    loader = _Loader(workspace.root)
    try:
        doc = load_yaml_text(text, rel)
    except YamlLoadError as exc:
        return [exc.issue]
    play = loader.model(Play, doc, doc.data, "play")
    if play is None:
        return loader.issues
    loader.findings(doc, play_findings(play))
    entry = PlayEntry(path=rel, draft=rel.parts[:1] == ("drafts",), play=play)
    loader.check_play_against_workspace(entry, doc, workspace)
    if not any(i.severity == Severity.ERROR for i in loader.issues):
        loader.issues.extend(entry_render_issues(workspace, entry, doc))
    return sorted(loader.issues, key=lambda i: (str(i.file), i.line or 0))


class _Loader:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.issues: list[Issue] = []

    # -- helpers ---------------------------------------------------------------

    def rel(self, path: Path) -> Path:
        return path.relative_to(self.root)

    def add(self, severity: Severity, code: str, message: str, file: Path, line: int | None = None):
        self.issues.append(Issue(severity, code, message, file, line))

    def at(self, doc: YamlDoc, severity: Severity, code: str, message: str, *loc: str | int):
        self.add(severity, code, message, doc.path, doc.line_for(loc))

    def findings(self, doc: YamlDoc, findings: Iterable[Finding]) -> None:
        for f in findings:
            self.add(f.severity, f.code, f.message, doc.path, doc.line_for(f.loc))

    def doc(self, path: Path) -> YamlDoc | None:
        try:
            loaded = load_yaml(path)
        except YamlLoadError as exc:
            self.issues.append(replace(exc.issue, file=self.rel(path)))
            return None
        return YamlDoc(path=self.rel(path), data=loaded.data, lines=loaded.lines)

    def model(self, cls: type[M], doc: YamlDoc, data: Any, code: str, prefix: Loc = ()) -> M | None:
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            self.issues.extend(validation_issues(exc, doc, code=code, prefix=prefix))
            return None

    def slug_name(self, path: Path, what: str) -> str | None:
        name = path.stem if path.is_file() else path.name
        if not _SLUG_RE.fullmatch(name):
            self.add(
                Severity.ERROR,
                "inventory.name",
                f"{what} name {name!r} must be a slug (lowercase letters, digits and hyphens)",
                self.rel(path),
            )
            return None
        return name

    def lint_vars(self, doc: YamlDoc, values: Any, prefix: Loc) -> None:
        """Warn on secret-looking keys with literal values (design.md, Merge rules)."""
        if isinstance(values, Mapping):
            for key, value in values.items():
                loc = (*prefix, key)
                if isinstance(value, str) and _SECRET_KEY_RE.search(str(key)):
                    self.at(
                        doc,
                        Severity.WARNING,
                        "vars.secret_literal",
                        f"'{key}' looks like it holds a secret; secret values belong in "
                        "Credential Manager, referenced with secret() in a template",
                        *loc,
                    )
                self.lint_vars(doc, value, loc)
        elif isinstance(values, list):
            for index, item in enumerate(values):
                self.lint_vars(doc, item, (*prefix, index))

    # -- loading ---------------------------------------------------------------

    def load(self) -> Workspace:
        ws = Workspace(root=self.root, config=self.load_workspace_file())
        ws.issues = self.issues
        if any(i.code == "workspace.schema_too_new" for i in self.issues):
            return ws  # never interpret files written for a newer schema
        ws.global_vars = (
            self.load_vars(self.root / GLOBAL_VARS) if (self.root / GLOBAL_VARS).is_file() else {}
        )
        ws.roles = self.load_roles()
        for folder in sorted((self.root / CUSTOMERS_DIR).glob("*")):
            if folder.is_dir():
                cust = self.load_customer(folder, ws.roles)
                if cust is not None:
                    ws.customers[cust.slug] = cust
        for base, draft in PLAY_DIRS:
            for path in sorted((self.root / base).rglob("*")):
                if path.is_file() and path.suffix in (".yml", ".yaml"):
                    entry = self.load_play(path, base, draft, ws)
                    ws.plays[entry.path] = entry
        self.lint_snippets()
        self.issues.sort(key=lambda i: (str(i.file), i.line or 0))
        return ws

    def load_workspace_file(self) -> WorkspaceFile | None:
        path = self.root / "workspace.yml"
        if not path.is_file():
            self.add(
                Severity.ERROR,
                "workspace.missing",
                "workspace.yml not found; this folder is not a Tsunami workspace",
                Path("workspace.yml"),
            )
            return None
        doc = self.doc(path)
        config = self.model(WorkspaceFile, doc, doc.data, "workspace") if doc else None
        if config is not None and config.schema_version > SCHEMA_VERSION:
            self.at(
                doc,
                Severity.ERROR,
                "workspace.schema_too_new",
                f"schema_version {config.schema_version} is newer than this version of "
                f"Tsunami understands ({SCHEMA_VERSION}); update Tsunami",
                "schema_version",
            )
        return config

    def load_vars(self, path: Path, prefix: Loc = ()) -> dict[str, Any]:
        doc = self.doc(path)
        if doc is None:
            return {}
        data = {} if doc.data is None else doc.data
        model = self.model(VarsFile, doc, data, "vars")
        if model is None:
            return {}
        self.lint_vars(doc, model.root, prefix)
        return dict(model.root)

    def load_roles(self) -> dict[str, dict[str, Any]]:
        roles = {}
        for path in sorted((self.root / ROLES_DIR).glob("*.yml")):
            name = self.slug_name(path, "role")
            if name is not None:
                roles[name] = self.load_vars(path)
        return roles

    def load_customer(self, folder: Path, roles: Mapping[str, Any]) -> CustomerInventory | None:
        slug = self.slug_name(folder, "customer folder")
        customer_path = folder / "customer.yml"
        if not customer_path.is_file():
            self.add(
                Severity.ERROR,
                "inventory.customer_missing",
                "customer folder has no customer.yml",
                self.rel(folder),
            )
            return None
        doc = self.doc(customer_path)
        config = self.model(CustomerFile, doc, doc.data, "customer") if doc else None
        if config is not None:
            self.lint_vars(doc, config.vars, ("vars",))
            for role, values in config.role_vars.items():
                if role not in roles:
                    self.at(
                        doc,
                        Severity.ERROR,
                        "inventory.unknown_role",
                        f"role_vars names unknown role {role!r}",
                        "role_vars",
                        role,
                    )
                self.lint_vars(doc, values, ("role_vars", role))

        sites = {}
        for path in sorted((folder / "sites").glob("*.yml")):
            name = self.slug_name(path, "site")
            if name is not None:
                sites[name] = self.load_vars(path)

        devices = self.load_devices(folder / "devices.yml", sites, roles)
        if slug is None or config is None:
            return None
        return CustomerInventory(slug=slug, config=config, devices=devices, sites=sites)

    def load_devices(
        self, path: Path, sites: Mapping[str, Any], roles: Mapping[str, Any]
    ) -> tuple[Device, ...]:
        if not path.is_file():
            return ()
        doc = self.doc(path)
        if doc is None or doc.data is None:
            return ()
        if not isinstance(doc.data, list):
            self.at(
                doc, Severity.ERROR, "devices.not_a_list", "devices.yml must be a list of devices"
            )
            return ()
        catalog = load_catalog()
        devices: list[Device] = []
        seen: set[str] = set()
        for index, item in enumerate(doc.data):
            device = self.model(Device, doc, item, "devices", prefix=(index,))
            if device is None:
                continue
            if device.name in seen:
                self.at(
                    doc,
                    Severity.ERROR,
                    "inventory.duplicate_device",
                    f"device name {device.name!r} is used twice",
                    index,
                    "name",
                )
                continue
            seen.add(device.name)
            if device.site not in sites:
                self.at(
                    doc,
                    Severity.ERROR,
                    "inventory.unknown_site",
                    f"site {device.site!r} has no file in sites/",
                    index,
                    "site",
                )
            if device.role not in roles:
                self.at(
                    doc,
                    Severity.ERROR,
                    "inventory.unknown_role",
                    f"role {device.role!r} has no file in inventory/roles/",
                    index,
                    "role",
                )
            if device.model is not None:
                if device.model not in catalog.ids():
                    self.at(
                        doc,
                        Severity.ERROR,
                        "inventory.unknown_model",
                        f"model {device.model!r} is not in the hardware catalog",
                        index,
                        "model",
                    )
                elif catalog.get(device.model).platform != device.platform:
                    entry = catalog.get(device.model)
                    self.at(
                        doc,
                        Severity.ERROR,
                        "inventory.model_platform",
                        f"model {device.model!r} runs {entry.platform}, but the device "
                        f"platform is {device.platform}",
                        index,
                        "platform",
                    )
            self.lint_vars(doc, device.vars, (index, "vars"))
            devices.append(device)
        return tuple(devices)

    def load_play(self, path: Path, base: Path, draft: bool, ws: Workspace) -> PlayEntry:
        rel = self.rel(path)
        if path.suffix == ".yaml":
            self.add(Severity.WARNING, "play.extension", "play files use .yml", rel)
        doc = self.doc(path)
        play = self.model(Play, doc, doc.data, "play") if doc else None
        if play is None:
            return PlayEntry(path=rel, draft=draft, play=None)
        self.findings(doc, play_findings(play))
        folder = rel.parts[len(base.parts)] if len(rel.parts) > len(base.parts) + 1 else None
        if folder != play.type.value:
            self.at(
                doc,
                Severity.WARNING,
                "play.folder",
                f"{play.type} plays belong under {base / play.type.value}/",
                "type",
            )
        entry = PlayEntry(path=rel, draft=draft, play=play)
        ws.play_docs[rel] = doc
        self.check_play_against_workspace(entry, doc, ws)
        return entry

    # -- rules across files ----------------------------------------------------

    def check_play_against_workspace(self, entry: PlayEntry, doc: YamlDoc, ws: Workspace) -> None:
        play = entry.play
        assert play is not None
        cust = ws.customers.get(play.customer)
        if cust is None:
            self.at(
                doc,
                Severity.ERROR,
                "play.unknown_customer",
                f"unknown customer {play.customer!r}",
                "customer",
            )
            return
        catalog = load_catalog()
        model_prompts = [n for n, p in play.prompts.items() if p.type == PromptType.MODEL]

        # Platforms the play's snippets must resolve for.
        if play.type == PlayType.INITIAL:
            platforms = self.initial_platforms(play, doc)
        else:
            result = resolve_targets(
                play.targets, cust.devices, sites=cust.sites.keys(), roles=ws.roles.keys()
            )
            self.findings(doc, result.findings)
            platforms = sorted({d.platform for d in result.devices})

        snippet_steps = [(i, s.snippet) for i, s in enumerate(play.steps) if s.snippet]

        def missing(platform: Platform) -> list[tuple[int, str]]:
            return [
                (i, name)
                for i, name in snippet_steps
                if find_snippet(
                    self.root, name, customer=play.customer, platform=platform, draft=entry.draft
                )
                is None
            ]

        if play.type == PlayType.INITIAL and model_prompts:
            # The model dropdown offers only models whose platform has every snippet.
            candidates = sorted({e.platform for e in catalog.selectable()})
            platforms = [p for p in candidates if not missing(p)]
            if not platforms:
                self.at(
                    doc,
                    Severity.ERROR,
                    "play.no_model_fits",
                    "no hardware model's platform has every snippet this play uses",
                    "prompts",
                    model_prompts[0],
                )
        else:
            gaps: dict[int, list[str]] = {}
            names = dict(snippet_steps)
            for platform in platforms:
                for i, _ in missing(platform):
                    gaps.setdefault(i, []).append(str(platform))
            for i, plats in sorted(gaps.items()):
                first = snippet_paths(names[i], customer=play.customer, platform=Platform(plats[0]))
                looked = ", ".join(p.as_posix() for p in first)
                self.at(
                    doc,
                    Severity.ERROR,
                    "play.snippet_missing",
                    f"snippet {names[i]!r} not found for {', '.join(plats)} (looked in {looked})",
                    "steps",
                    i,
                    "snippet",
                )

        families = sorted({PLATFORM_FAMILY[p] for p in platforms})
        if len(families) > 1:
            span = ", ".join(families)
            for i, step in enumerate(play.steps):
                if step.config is not None or step.exec is not None:
                    self.at(
                        doc,
                        Severity.ERROR,
                        "play.mixed_family_inline",
                        f"inline {step.kind} lines are written in one CLI, but the targets "
                        f"span families {span}; use snippets or split the play",
                        "steps",
                        i,
                    )
            for phase in ("pre", "post"):
                for i, check in enumerate(getattr(play.checks, phase)):
                    if check.exec is not None:
                        self.at(
                            doc,
                            Severity.ERROR,
                            "play.mixed_family_inline",
                            f"exec checks are written in one CLI, but the targets span "
                            f"families {span}",
                            "checks",
                            phase,
                            i,
                        )
            for name, prompt in play.prompts.items():
                if prompt.type == PromptType.INTERFACE and not prompt.per_device:
                    self.at(
                        doc,
                        Severity.ERROR,
                        "play.mixed_family_interface",
                        f"interface prompt {name!r} must be per_device when targets span "
                        f"families {span}",
                        "prompts",
                        name,
                    )

        family: PlatformFamily | None = families[0] if len(families) == 1 else None
        self.check_prompt_defaults(play, doc, cust, ws, family, catalog.ids())
        if play.type == PlayType.INITIAL and play.on_success.add_to_inventory:
            self.check_add_to_inventory(play, doc, cust, ws, bool(model_prompts))

    def initial_platforms(self, play: Play, doc: YamlDoc) -> list[Platform]:
        value = play.vars.get("platform")
        if value is not None:
            if value not in {str(p) for p in SUPPORTED_PLATFORMS}:
                self.at(
                    doc,
                    Severity.ERROR,
                    "play.unknown_platform",
                    f"platform {value!r} is not a supported platform",
                    "vars",
                    "platform",
                )
                return []
            return [Platform(value)]
        prompt = play.prompts.get("platform")
        if prompt is not None and prompt.type == PromptType.CHOICE and prompt.choices:
            known = [Platform(c) for c in prompt.choices if c in {str(p) for p in Platform}]
            return known
        return sorted(SUPPORTED_PLATFORMS)

    def check_prompt_defaults(
        self,
        play: Play,
        doc: YamlDoc,
        cust: CustomerInventory,
        ws: Workspace,
        family: PlatformFamily | None,
        model_ids: frozenset[str],
    ) -> None:
        context = PromptContext(
            sites=frozenset(cust.sites),
            roles=frozenset(ws.roles),
            model_ids=model_ids,
            family=family,
        )
        for name, prompt in play.prompts.items():
            default = prompt.default
            if default is None or (isinstance(default, str) and _TEMPLATED_RE.search(default)):
                continue
            if prompt.type == PromptType.INTERFACE and family is None:
                continue  # checked per device at run time
            try:
                coerce(
                    prompt.type,
                    default,
                    context=context,
                    choices=prompt.choices,
                    pattern=prompt.pattern,
                    minimum=prompt.min,
                    maximum=prompt.max,
                )
            except PromptValueError as exc:
                self.at(
                    doc,
                    Severity.ERROR,
                    "play.prompt_default",
                    f"default for prompt {name!r}: {exc}",
                    "prompts",
                    name,
                    "default",
                )

    def check_add_to_inventory(
        self, play: Play, doc: YamlDoc, cust: CustomerInventory, ws: Workspace, has_model: bool
    ) -> None:
        available = set(play.vars) | set(play.prompts) | set(ws.global_vars) | set(cust.config.vars)
        for values in (*cust.config.role_vars.values(), *ws.roles.values(), *cust.sites.values()):
            available |= set(values)
        if has_model:
            available.add("platform")
        needed = ("hostname", "mgmt_ip", "site", "role", "platform")
        missing = [name for name in needed if name not in available]
        if missing:
            self.at(
                doc,
                Severity.ERROR,
                "play.add_to_inventory",
                f"add_to_inventory needs {', '.join(missing)}, which no prompt, play var "
                "or inventory file provides",
                "on_success",
                "add_to_inventory",
            )

    def lint_snippets(self) -> None:
        families = {str(f) for f in PlatformFamily}
        platforms = {str(p) for p in Platform}
        for base in SNIPPET_DIRS:
            for path in sorted((self.root / base).rglob("*")):
                if not path.is_file() or path.name == ".gitkeep":
                    continue
                rel = self.rel(path)
                parts = rel.parts[len(base.parts) :]
                kind = parts[0] if parts else ""
                placed = (
                    (kind == "common" and len(parts) == 3 and parts[1] in families)
                    or (kind == "platform" and len(parts) == 3 and parts[1] in platforms)
                    or (kind == "customers" and len(parts) == 4 and parts[2] in families)
                )
                if path.suffix != ".j2":
                    self.add(Severity.WARNING, "snippet.extension", "snippets use .j2", rel)
                elif not placed:
                    self.add(
                        Severity.WARNING,
                        "snippet.location",
                        f"snippet is not where lookup finds it; use {base}/common/<family>/, "
                        f"{base}/platform/<platform>/ or {base}/customers/<customer>/<family>/",
                        rel,
                    )
                elif not _SLUG_RE.fullmatch(path.stem):
                    self.add(
                        Severity.WARNING,
                        "snippet.name",
                        f"snippet name {path.stem!r} must be a slug",
                        rel,
                    )
