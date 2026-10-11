"""Per-call enforcement in spec order, result redaction, and audit rows.

Every tool call goes through McpApp.call:

1. the policy is enabled (re-read from %APPDATA% on every call, so GUI edits apply
   at once; the MCP server has no write path to it);
2. the customer is not blocked (a blocked customer is reported as unknown);
3. the tool's tier is allowed;
4. paths are checked by the tool itself through core.pathguard;
5. the rate limit holds;
6. the tool runs;
7. every string in the result is redacted, with every stored secret and login password
   as exact values, except values the caller supplied (redacting an echo of a guess
   would confirm it);
8. an audit row is written to history.db.

See design.md, MCP interface: Enforcement order, Leak paths.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from tsunami.core.issues import TsunamiError
from tsunami.core.models.policy import McpPolicy
from tsunami.core.models.settings import load_settings
from tsunami.core.paths import AppPaths
from tsunami.core.policy import load_policy
from tsunami.core.redact import Redactor
from tsunami.core.workspace import Workspace, load_workspace

log = logging.getLogger(__name__)


class ToolRefused(TsunamiError, ToolError):
    """A tool call refused by policy or input checks. The message is safe to return.

    It is an MCP ToolError, so the client sees the reason, not a generic failure.
    """


class McpApp:
    """State shared by every tool call in one MCP server process."""

    def __init__(
        self,
        paths: AppPaths,
        *,
        history: Any,  # tsunami.core.history.History
        secrets: Any,  # tsunami.core.secret_store.SecretStore
        transport_factory: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.paths = paths
        self.history = history
        self.secrets = secrets
        self.transport_factory = transport_factory
        self.clock = clock
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()
        self.threads: list[threading.Thread] = []

    # -- state read fresh on every call -------------------------------------------------

    def policy(self) -> McpPolicy:
        return load_policy(self.paths.policy_file)

    def settings(self):
        return load_settings(self.paths.settings_file)

    def workspace(self, *, render: bool = False) -> Workspace:
        folder = self.settings().workspace
        if not folder:
            raise ToolRefused("no workspace is open in Tsunami; open one in the GUI first")
        return load_workspace(folder, render=render)

    def transport(self) -> Any:
        if self.transport_factory is not None:
            return self.transport_factory()
        from tsunami.core.transports import DeviceTransport

        return DeviceTransport(self.paths, self.settings())

    def visible_customer(self, policy: McpPolicy, workspace: Workspace, customer: str) -> Any:
        """The customer's inventory, or ToolRefused if unknown or blocked (indistinguishable)."""
        if customer in policy.customers_blocked or customer not in workspace.customers:
            raise ToolRefused(f"unknown customer {customer!r}")
        return workspace.customers[customer]

    # -- the enforcement wrapper --------------------------------------------------------

    def call(
        self,
        tool: str,
        tier: int,
        args: dict[str, Any],
        fn: Callable[[McpPolicy], dict[str, Any]],
        *,
        customer: str | None = None,
        known: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Run fn under the policy; customer is checked against the block list.

        Results are redacted with every stored secret of every customer, except values
        the caller supplied (args, plus known: earlier input such as a job's prompts).
        Redacting the echo of a guess would confirm the guess. Audit rows get the full
        redaction, with no exception.
        """
        start = self.clock()
        status = "ok"
        result: dict[str, Any] = {}
        full = redactor = Redactor()
        try:
            secrets = self.secret_values()
            full = Redactor(secrets)
            supplied = _supplied_values(args) | _supplied_values(list(known))
            redactor = Redactor(v for v in secrets if v not in supplied)
            policy = self.policy()
            if not policy.enabled:
                raise ToolRefused("MCP access is turned off in Tsunami Settings")
            if customer is not None and customer in policy.customers_blocked:
                raise ToolRefused(f"unknown customer {customer!r}")
            if tier not in (0, 1, 2):
                raise ToolRefused("unknown tool tier")
            self._rate_limit(policy)
            result = fn(policy)
            result = _redact_value(result, redactor)
            return result
        except ToolRefused as exc:
            status = "refused"
            raise ToolRefused(redactor.text(str(exc))) from None
        except TsunamiError as exc:
            status = "error"
            raise ToolRefused(redactor.text(str(exc))) from None
        except Exception as exc:  # never leak internals or secrets in an error
            status = "error"
            log.exception("tool %s failed", tool)
            raise ToolRefused(f"internal error ({type(exc).__name__}); see mcp.log") from None
        finally:
            self._audit(tool, args, status, start, result, full)

    # -- helpers ----------------------------------------------------------------------

    def _rate_limit(self, policy: McpPolicy) -> None:
        now = self.clock()
        with self._lock:
            while self._calls and now - self._calls[0] > 60:
                self._calls.popleft()
            if len(self._calls) >= policy.rate_limit_per_minute:
                raise ToolRefused(
                    f"rate limit: at most {policy.rate_limit_per_minute} tool calls per minute"
                )
            self._calls.append(now)

    def secret_values(self) -> list[str]:
        """Every stored secret and login password. Fails closed: no call runs without them."""
        try:
            return list(self.secrets.all_secret_values())
        except Exception:  # noqa: BLE001
            log.warning("cannot read the credential store for redaction", exc_info=True)
            raise ToolRefused("the credential store cannot be read; see mcp.log") from None

    def full_redactor(self) -> Redactor:
        """Every stored secret, with no exception for values the caller supplied."""
        return Redactor(self.secret_values())

    def _audit(
        self,
        tool: str,
        args: dict[str, Any],
        status: str,
        start: float,
        result: dict[str, Any],
        redactor: Redactor,
    ) -> None:
        try:
            self.history.record_mcp_call(
                tool=tool,
                args_redacted=_redact_value(args, redactor),
                status=status,
                duration_ms=int((self.clock() - start) * 1000),
                result_bytes=len(json.dumps(result, default=str)),
                job_id=result.get("job_id") if isinstance(result, dict) else None,
            )
        except Exception:  # noqa: BLE001 - auditing must not break the call
            log.warning("could not record audit row for %s", tool, exc_info=True)


_WORD_SPLIT = re.compile(r"[\s'\"`,\[\]{}()]+")


def _supplied_values(value: Any) -> set[str]:
    """Every string the caller sent, whole and split into words."""
    out: set[str] = set()
    if isinstance(value, str):
        out |= {value, value.strip(), *(w for w in _WORD_SPLIT.split(value) if w)}
    elif isinstance(value, dict):
        for key, item in value.items():
            out |= _supplied_values(key) | _supplied_values(item)
    elif isinstance(value, list | tuple | set):
        for item in value:
            out |= _supplied_values(item)
    elif value is not None and not isinstance(value, bool):
        out.add(str(value))
    return out


def _redact_value(value: Any, redactor: Redactor) -> Any:
    if isinstance(value, str):
        return redactor.text(value)
    if isinstance(value, dict):
        return {k: _redact_value(v, redactor) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_value(v, redactor) for v in value]
    return value
