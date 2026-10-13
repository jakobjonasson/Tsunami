"""The local link between tsunami-mcp.exe and the Tsunami window.

tsunami-mcp.exe holds no credentials and decrypts nothing. It forwards each tool call
over a named pipe to the Tsunami GUI, which runs it through the enforcement wrapper
(policy, redaction, audit) only while Tsunami is unlocked. Locked, or not running, the
call is refused with a 403- or 503-style message for Claude.

The pipe is created by the GUI with Windows' default security for named pipes (only
you, SYSTEM and administrators can open it read-write), and the handshake needs a
random token the GUI writes to %LOCALAPPDATA%/Tsunami each time it starts. Messages are
JSON, never pickles.

See design.md, MCP interface: Process model.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import tempfile
import threading
from collections.abc import Callable
from multiprocessing.connection import AuthenticationError, Client, Listener
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from tsunami.core.paths import AppPaths

log = logging.getLogger(__name__)

LOCKED = "403 Tsunami is locked. Ask the user to unlock it in the Tsunami window, then retry."
NOT_RUNNING = "503 Tsunami is not running. Ask the user to start and unlock it, then retry."
INTERNAL = "500 internal error in Tsunami; see app.log"
MAX_MESSAGE = 16 * 1024 * 1024


def pipe_address(paths: AppPaths) -> str:
    """One pipe per data folder, so tests and other Windows users never collide."""
    digest = hashlib.sha256(str(paths.machine).lower().encode("utf-8")).hexdigest()[:16]
    return rf"\\.\pipe\tsunami-mcp-{digest}"


def token_path(paths: AppPaths) -> Path:
    return paths.machine / "mcp-bridge.token"


class BridgeServer:
    """Runs in the GUI process: answers tool calls from tsunami-mcp.exe."""

    def __init__(
        self,
        paths: AppPaths,
        handle: Callable[[str, dict[str, Any]], dict[str, Any]],
        is_unlocked: Callable[[], bool],
    ) -> None:
        self.paths = paths
        self._handle_call = handle
        self._is_unlocked = is_unlocked
        self._listener: Listener | None = None
        self._token = b""
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._token = secrets.token_bytes(32)
        self._listener = Listener(pipe_address(self.paths), family="AF_PIPE", authkey=self._token)
        _write_private(token_path(self.paths), self._token.hex())
        self._thread = threading.Thread(target=self._serve, name="mcp-bridge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._listener is None or self._stopped.is_set():
            return
        self._stopped.set()
        # accept() on a pipe does not wake when the listener closes; a connection wakes it.
        # Connect from a helper thread: if the serve thread saw the flag and left before
        # reaching accept(), nobody answers the handshake, and stop() must not wait for it.
        # Closing the listener then breaks that pipe, so the helper ends either way.
        waker = threading.Thread(target=self._wake, name="mcp-bridge-wake", daemon=True)
        waker.start()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._listener.close()
        waker.join(timeout=5)
        token_path(self.paths).unlink(missing_ok=True)

    def _wake(self) -> None:
        try:
            Client(pipe_address(self.paths), family="AF_PIPE", authkey=self._token).close()
        except (OSError, EOFError, AuthenticationError):
            pass

    def _serve(self) -> None:
        assert self._listener is not None
        while not self._stopped.is_set():
            try:
                conn = self._listener.accept()
            except (OSError, EOFError, AuthenticationError):
                continue  # a client with a stale token, or one that hung up
            if self._stopped.is_set():
                conn.close()
                break
            threading.Thread(target=self._answer, args=(conn,), daemon=True).start()

    def _answer(self, conn: Any) -> None:
        from tsunami.mcp_server.enforcement import ToolRefused

        with conn:
            try:
                request = json.loads(conn.recv_bytes(MAX_MESSAGE))
                tool, args = str(request["tool"]), dict(request.get("args") or {})
                if not self._is_unlocked():
                    reply: dict[str, Any] = {"error": LOCKED}
                else:
                    reply = {"result": self._handle_call(tool, args)}
            except ToolRefused as exc:
                reply = {"error": str(exc)}
            except Exception:  # noqa: BLE001 - never leak internals over the pipe
                log.exception("MCP bridge call failed")
                reply = {"error": INTERNAL}
            try:
                conn.send_bytes(json.dumps(reply, default=str).encode("utf-8"))
            except OSError:
                pass  # the MCP server went away


class BridgeClient:
    """Runs in tsunami-mcp.exe: forwards one tool call per connection."""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            token = bytes.fromhex(token_path(self.paths).read_text(encoding="ascii").strip())
            conn = Client(pipe_address(self.paths), family="AF_PIPE", authkey=token)
        except (OSError, ValueError, AuthenticationError, EOFError):
            raise ToolError(NOT_RUNNING) from None
        with conn:
            conn.send_bytes(json.dumps({"tool": tool, "args": args}).encode("utf-8"))
            try:
                reply = json.loads(conn.recv_bytes(MAX_MESSAGE))
            except (OSError, EOFError):
                raise ToolError(NOT_RUNNING) from None  # Tsunami closed mid-call
        if "error" in reply:
            raise ToolError(str(reply["error"]))
        return reply["result"]


def _write_private(path: Path, text: str) -> None:
    """%LOCALAPPDATA% is readable only by you; write atomically so a reader never sees half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
