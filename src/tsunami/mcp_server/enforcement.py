"""Per-call enforcement in spec order, result redaction, and audit rows.

Every tool call goes through McpApp.call:

1. the policy is enabled (re-read from %APPDATA% on every call, so GUI edits apply
   at once; the MCP server has no write path to it);
2. the customer is not blocked (a blocked customer is reported as unknown);
3. the tool's tier is allowed;
4. paths are checked by the tool itself through core.pathguard;
5. the rate limit holds;
6. the tool runs;
7. every string in the result is redacted, with the customer's secret values and
   login password as exact values;
8. an audit row is written to history.db.

See design.md, MCP interface: Enforcement order, Leak paths.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from tsunami.core.issues import TsunamiError
from tsunami.core.models.policy import McpPolicy
from tsunami.core.models.settings import load_settings
from tsunami.core.paths import AppPaths
from tsunami.core.policy import load_policy
from tsunami.core.redact import Redactor
from tsunami.core.workspace import Workspace, load_workspace

log = logging.getLogger(__name__)


class ToolRefused(TsunamiError):
    """A tool call refused by policy or input checks. The message is safe to return."""


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
    ) -> dict[str, Any]:
        start = self.clock()
        status = "ok"
        result: dict[str, Any] = {}
        redactor = self._redactor(customer)
        try:
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
            self._audit(tool, args, status, start, result, redactor)

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

    def _redactor(self, customer: str | None) -> Redactor:
        """Pattern layer always; exact values when the call concerns one customer."""
        exact: list[str] = []
        if customer:
            try:
                exact += self.secrets.secret_values(customer)
                workspace = self.workspace()
                cust = workspace.customers.get(customer)
                if cust is not None:
                    login = self.secrets.get_login(cust.config.credential)
                    exact += [v for v in (login.password, login.enable) if v]
            except Exception:  # noqa: BLE001 - redaction falls back to the pattern layer
                log.warning("exact-value redaction unavailable for %s", customer)
        return Redactor(exact)

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


def _redact_value(value: Any, redactor: Redactor) -> Any:
    if isinstance(value, str):
        return redactor.text(value)
    if isinstance(value, dict):
        return {k: _redact_value(v, redactor) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_value(v, redactor) for v in value]
    return value
