"""Runtime CLI driver for the CAO product.

The product layer drives the managed runtime through its command line. The
transport is injected: production supplies a subprocess runner over the
managed executable's path, and tests supply a fake runner, so no test ever
touches a live runtime.

Two rules are load-bearing:

* argv is always built as a list, never a shell string, and a bare textual
  argument (a label or a prompt) that begins with ``-`` is refused before any
  call, because the runtime parses it as a bare argument and could read it as
  an option.
* every identifier a caller then addresses is read back from the runtime's own
  JSON response and bound to the request. A response that cannot be read, or
  that carries an unrecognized error code, is a protocol failure; untrusted
  values are never echoed into a public message.

This module is deliberately runtime-neutral: the executable is supplied as
data (the managed runtime's installed path), never named as a constant here.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Any

__all__ = [
    "DEFAULT_TIMEOUT",
    "MIN_TIMEOUT_MS",
    "MAX_TIMEOUT_MS",
    "READ_SOURCES",
    "WAIT_STATES",
    "PROMPT_ACTIVITY_STATES",
    "Runner",
    "RuntimeDriver",
    "DriverError",
    "DriverUsageError",
    "DriverTransportError",
    "DriverProtocolError",
    "DriverUnavailable",
    "DriverNotFound",
    "wait_state",
]

DEFAULT_TIMEOUT = 30.0
MIN_TIMEOUT_MS = 1
MAX_TIMEOUT_MS = 300_000

#: Extra transport time beyond a native ``--timeout`` so a bounded runtime can
#: answer with its own typed failure instead of being killed by the transport.
_NATIVE_TIMEOUT_GRACE = 5.0

#: Terminal-snapshot sources the runtime documents for a read.
READ_SOURCES = ("visible", "recent", "recent-unwrapped", "detection")
#: Lifecycle states the runtime documents for a wait.
WAIT_STATES = ("idle", "working", "blocked", "done", "unknown")
#: States that prove the runtime observed the submitted prompt. ``idle`` is
#: deliberately excluded: a terminal can acknowledge a prompt while the model
#: never consumed it, which is exactly the unconfirmed write this driver must
#: not mistake for execution.
PROMPT_ACTIVITY_STATES = ("working", "blocked", "done")

#: The closed set of error codes this driver recognizes as a genuine
#: not-found answer. Every other value, whatever its shape, is a protocol
#: failure and is never reflected into a public message.
_NOT_FOUND_CODES = frozenset(
    {"pane_not_found", "workspace_not_found", "agent_not_found", "tab_not_found"}
)

#: A transport runs one runtime CLI command and returns its stdout text.
Runner = Callable[[Sequence[str]], str]


class DriverError(RuntimeError):
    """Base class for driver failures."""

    code = "RUNTIME_DRIVER_ERROR"


class DriverUsageError(DriverError):
    """A caller-supplied argument is unsafe or malformed; nothing was called."""

    code = "RUNTIME_DRIVER_USAGE"


class DriverTransportError(DriverError):
    """The runtime CLI could not be executed or reported a failure."""

    code = "RUNTIME_DRIVER_TRANSPORT"


class DriverProtocolError(DriverError):
    """A runtime response could not be read as the expected shape."""

    code = "RUNTIME_DRIVER_PROTOCOL"


class DriverUnavailable(DriverError):
    """No usable runtime executable is configured."""

    code = "RUNTIME_DRIVER_UNAVAILABLE"


class DriverNotFound(DriverError):
    """The requested runtime object does not exist."""

    code = "RUNTIME_DRIVER_NOT_FOUND"


# --------------------------------------------------------------------------
# Argument and response validation
# --------------------------------------------------------------------------


def _require_text(value: Any, what: str) -> str:
    """A bare textual argument, refused when it begins with ``-``."""

    if not isinstance(value, str) or not value:
        raise DriverUsageError(f"{what} must be a non-empty string")
    if value.startswith("-"):
        raise DriverUsageError(
            f"{what} starting with '-' is not supported: the runtime receives "
            f"it as a bare argument and could read it as an option"
        )
    return value


def _require_id(value: Any, what: str) -> str:
    """An identifier used as a bare positional argument."""

    if not isinstance(value, str) or not value:
        raise DriverProtocolError(f"{what} must be a non-empty string")
    if value.startswith("-") or any(ch.isspace() for ch in value):
        raise DriverProtocolError(f"{what} is not a usable identifier")
    return value


def _require_mapping(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise DriverProtocolError(f"runtime response is missing {key!r}")
    return value


def _require_str(mapping: Mapping[str, Any], key: str, what: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise DriverProtocolError(f"{what} must be a non-empty string")
    return value


def _bounded_timeout_ms(value: Any, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise DriverUsageError("timeout must be an integer number of milliseconds")
    if not MIN_TIMEOUT_MS <= value <= MAX_TIMEOUT_MS:
        raise DriverUsageError(
            f"timeout must be within [{MIN_TIMEOUT_MS}, {MAX_TIMEOUT_MS}] ms"
        )
    return value


def _bounded_lines(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DriverUsageError("lines must be a positive integer")
    return value


def _recognized_not_found(code: Any) -> str | None:
    if isinstance(code, str) and code in _NOT_FOUND_CODES:
        return code
    return None


def _error_for_code(code: Any) -> DriverError:
    recognized = _recognized_not_found(code)
    if recognized is not None:
        return DriverNotFound(f"runtime reports {recognized!r}")
    return DriverProtocolError("runtime returned an unrecognized error response")


def _error_from_stderr(stderr: str, returncode: int) -> DriverError:
    """Map a failed invocation to a typed error without echoing raw text.

    Neither the executable path, the argv, nor raw stderr (which can name host
    paths) is surfaced; only an allowlisted structured error code is reflected.
    """

    code = None
    if stderr:
        try:
            parsed = json.loads(stderr)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, Mapping):
            error = parsed.get("error")
            if isinstance(error, Mapping):
                code = error.get("code")
    recognized = _recognized_not_found(code)
    if recognized is not None:
        return DriverNotFound(f"runtime reports {recognized!r}")
    return DriverTransportError(f"runtime command failed with exit code {returncode}")


class _SubprocessRunner:
    """Run the managed CLI with a transport deadline of its own.

    The deadline is per call: a command that carries a native ``--timeout``
    needs at least that long to answer, otherwise the transport would kill a
    live command before the runtime can report what it observed.
    """

    def __init__(self, executable: str, timeout: float) -> None:
        self._executable = executable
        self._timeout = timeout

    def __call__(self, argv: Sequence[str], timeout: float | None = None) -> str:
        deadline = self._timeout if timeout is None else max(self._timeout, timeout)
        failure: str | None = None
        process: subprocess.CompletedProcess[str] | None = None
        try:
            process = subprocess.run(
                [self._executable, *argv],
                capture_output=True,
                text=True,
                timeout=deadline,
            )
        except FileNotFoundError:
            failure = "runtime executable was not found"
        except subprocess.TimeoutExpired:
            failure = f"runtime command timed out after {deadline}s"
        except OSError:
            failure = "runtime command could not be executed"
        if failure is not None:
            raise DriverTransportError(failure)
        if process is None:  # pragma: no cover - defensive
            raise DriverTransportError("runtime command did not run")
        if process.returncode != 0:
            raise _error_from_stderr(process.stderr or "", process.returncode)
        return process.stdout


def _subprocess_runner(executable: str | None, timeout: float) -> Runner:
    """Build the default transport over the managed runtime's installed path."""

    if not isinstance(executable, str) or not executable:
        raise DriverUnavailable("no managed runtime executable is configured")
    return _SubprocessRunner(executable, timeout)


def _read_envelope(stdout: str, expected_types: Sequence[str]) -> Mapping[str, Any]:
    """Parse one JSON envelope and require an expected result type."""

    malformed = False
    envelope: Any = None
    try:
        envelope = json.loads(stdout)
    except (TypeError, ValueError):
        malformed = True
    if malformed:
        raise DriverProtocolError("runtime returned output that is not JSON")
    if not isinstance(envelope, Mapping):
        raise DriverProtocolError("runtime response must be a JSON object")
    if "error" in envelope:
        error = envelope.get("error")
        code = error.get("code") if isinstance(error, Mapping) else None
        raise _error_for_code(code)
    result = envelope.get("result")
    if not isinstance(result, Mapping):
        raise DriverProtocolError("runtime response is missing a result object")
    if result.get("type") not in expected_types:
        raise DriverProtocolError("unexpected runtime result type")
    return result


def _check_optional_envelope(stdout: str) -> None:
    """Refuse an error envelope from a command whose body is not otherwise read."""

    text = stdout.strip()
    if not text.startswith("{"):
        return
    try:
        parsed = json.loads(text)
    except ValueError:
        return
    if isinstance(parsed, Mapping) and "error" in parsed:
        error = parsed.get("error")
        code = error.get("code") if isinstance(error, Mapping) else None
        raise _error_for_code(code)


def wait_state(result: Mapping[str, Any]) -> str | None:
    """Extract an agent lifecycle state from a wait/get response, or ``None``."""

    for key in ("state", "matched_state", "status", "agent_status"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    agent = result.get("agent")
    if isinstance(agent, Mapping):
        for key in ("agent_status", "state", "status"):
            value = agent.get(key)
            if isinstance(value, str) and value:
                return value
    return None


class RuntimeDriver:
    """Drive the managed runtime's command line through an injected runner."""

    def __init__(
        self,
        *,
        runner: Runner | None = None,
        executable: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if runner is not None and not callable(runner):
            raise TypeError("runner must be callable")
        self._runner: Runner = (
            runner if runner is not None else _subprocess_runner(executable, timeout)
        )

    def _run(self, argv: Sequence[str], timeout_ms: int | None = None) -> str:
        """Run one command, letting a native deadline govern the transport.

        Injected runners stay one-argument callables. Only the managed-CLI
        transport knows how to accept the larger deadline a native ``--timeout``
        requires.
        """

        runner = self._runner
        if timeout_ms is not None and isinstance(runner, _SubprocessRunner):
            return runner(argv, timeout=timeout_ms / 1000.0 + _NATIVE_TIMEOUT_GRACE)
        return runner(argv)

    # -- workspaces --------------------------------------------------------

    def workspace_create(self, label: str, cwd: str | None = None) -> dict[str, str]:
        argv = [
            "workspace",
            "create",
            "--label",
            _require_text(label, "workspace label"),
            "--no-focus",
        ]
        if cwd:
            argv += ["--cwd", _require_text(cwd, "workspace cwd")]
        result = _read_envelope(self._runner(argv), ("workspace_created",))
        workspace = _require_mapping(result, "workspace")
        tab = _require_mapping(result, "tab")
        root = _require_mapping(result, "root_pane")
        return {
            "workspace_id": _require_str(workspace, "workspace_id", "workspace id"),
            "tab_id": _require_str(tab, "tab_id", "tab id"),
            "pane_id": _require_str(root, "pane_id", "pane id"),
        }

    def workspace_get(self, workspace_id: str) -> dict[str, Any]:
        argv = ["workspace", "get", _require_id(workspace_id, "workspace id")]
        result = _read_envelope(self._runner(argv), ("workspace_info",))
        return dict(_require_mapping(result, "workspace"))

    def workspace_list(self) -> list[dict[str, Any]]:
        result = _read_envelope(self._runner(["workspace", "list"]), ("workspace_list",))
        items = result.get("workspaces")
        if not isinstance(items, list):
            raise DriverProtocolError("workspace_list is missing a workspaces array")
        out: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, Mapping):
                raise DriverProtocolError("each workspace entry must be an object")
            out.append(
                {
                    "workspace_id": _require_str(item, "workspace_id", "workspace id"),
                    "label": item.get("label"),
                    "active_tab_id": item.get("active_tab_id"),
                }
            )
        return out

    # -- tabs --------------------------------------------------------------

    def tab_create(
        self, workspace_id: str, label: str | None = None, cwd: str | None = None
    ) -> dict[str, str]:
        argv = [
            "tab",
            "create",
            "--workspace",
            _require_id(workspace_id, "workspace id"),
            "--no-focus",
        ]
        if label:
            argv += ["--label", _require_text(label, "tab label")]
        if cwd:
            argv += ["--cwd", _require_text(cwd, "tab cwd")]
        result = _read_envelope(self._runner(argv), ("tab_created",))
        tab = _require_mapping(result, "tab")
        root = _require_mapping(result, "root_pane")
        return {
            "tab_id": _require_str(tab, "tab_id", "tab id"),
            "pane_id": _require_str(root, "pane_id", "pane id"),
        }

    # -- panes -------------------------------------------------------------

    def pane_create(
        self, parent_pane: str, *, direction: str = "down", cwd: str | None = None
    ) -> str:
        if direction not in ("right", "down"):
            raise DriverUsageError("direction must be 'right' or 'down'")
        argv = [
            "pane",
            "split",
            _require_id(parent_pane, "pane id"),
            "--direction",
            direction,
            "--no-focus",
        ]
        if cwd:
            argv += ["--cwd", _require_text(cwd, "pane cwd")]
        result = _read_envelope(self._runner(argv), ("pane_created",))
        pane = _require_mapping(result, "pane")
        return _require_str(pane, "pane_id", "pane id")

    def pane_list(self, workspace_id: str) -> list[dict[str, Any]]:
        argv = ["pane", "list", "--workspace", _require_id(workspace_id, "workspace id")]
        result = _read_envelope(self._runner(argv), ("pane_list",))
        items = result.get("panes")
        if not isinstance(items, list):
            raise DriverProtocolError("pane_list is missing a panes array")
        out: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, Mapping):
                raise DriverProtocolError("each pane entry must be an object")
            out.append(
                {
                    "pane_id": _require_str(item, "pane_id", "pane id"),
                    "workspace_id": item.get("workspace_id"),
                    "agent_status": item.get("agent_status"),
                }
            )
        return out

    def pane_get(self, pane_id: str) -> dict[str, Any]:
        argv = ["pane", "get", _require_id(pane_id, "pane id")]
        result = _read_envelope(self._runner(argv), ("pane_info",))
        return dict(_require_mapping(result, "pane"))

    def pane_read(self, pane_id: str, *, lines: int = 200, source: str = "recent") -> str:
        lines = _bounded_lines(lines)
        if source not in READ_SOURCES:
            raise DriverUsageError(f"unsupported read source {source!r}")
        argv = [
            "pane",
            "read",
            _require_id(pane_id, "pane id"),
            "--source",
            source,
            "--lines",
            str(lines),
        ]
        return self._runner(argv)

    def pane_close(self, pane_id: str) -> str:
        stdout = self._runner(["pane", "close", _require_id(pane_id, "pane id")])
        _check_optional_envelope(stdout)
        return stdout

    # -- agents ------------------------------------------------------------

    def agent_start(
        self,
        name: str,
        kind: str,
        pane_id: str,
        *,
        argv: Sequence[str] = (),
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        _require_text(name, "agent name")
        _require_text(kind, "agent kind")
        command = [
            "agent",
            "start",
            name,
            "--kind",
            kind,
            "--pane",
            _require_id(pane_id, "pane id"),
        ]
        if timeout_ms is not None:
            timeout_ms = _bounded_timeout_ms(timeout_ms, timeout_ms)
            command += ["--timeout", str(timeout_ms)]
        extra: list[str] = []
        for arg in argv:
            if not isinstance(arg, str):
                raise DriverUsageError("agent arguments must be strings")
            extra.append(arg)
        if extra:
            # Native agent arguments follow ``--``; flags there are safe.
            command.append("--")
            command.extend(extra)
        result = _read_envelope(self._run(command, timeout_ms), ("agent_started",))
        agent = _require_mapping(result, "agent")
        return {
            "agent_pane_id": _require_str(agent, "pane_id", "agent pane id"),
            "agent_workspace_id": agent.get("workspace_id"),
        }

    def agent_prompt(
        self,
        target: str,
        text: str,
        *,
        timeout_ms: int | None = None,
        wait: bool = False,
    ) -> dict[str, Any]:
        target = _require_text(target, "agent target")
        text = _require_text(text, "prompt text")
        command = ["agent", "prompt", target, text]
        if wait:
            # A bare ``agent_prompted`` only proves the terminal accepted the
            # write, not that the runtime consumed the prompt. Ask the runtime
            # to wait for native observable activity instead, bounded by the
            # caller's timeout.
            command.append("--wait")
            for state in PROMPT_ACTIVITY_STATES:
                command += ["--until", state]
            if timeout_ms is not None:
                timeout_ms = _bounded_timeout_ms(timeout_ms, timeout_ms)
                command += ["--timeout", str(timeout_ms)]
        result = _read_envelope(self._run(command, timeout_ms), ("agent_prompted",))
        agent = _require_mapping(result, "agent")
        acknowledged = {
            "agent_pane_id": _require_str(agent, "pane_id", "agent pane id"),
            "agent_workspace_id": agent.get("workspace_id"),
        }
        if not wait:
            return acknowledged
        # With ``--wait`` the runtime returns only after it observed one of the
        # requested activity states, so a reported allowed state is the activity
        # proof. Anything else -- an explicit idle/unknown state or no state at
        # all -- is refused instead of being rounded up: a bare acknowledgement
        # proves only that the terminal accepted the write.
        state = wait_state(result)
        if state not in PROMPT_ACTIVITY_STATES:
            raise DriverProtocolError(
                "runtime reported no prompt activity after a bounded wait"
            )
        acknowledged["observed_state"] = state
        acknowledged["activity_observed"] = True
        return acknowledged

    def agent_read(self, target: str, *, lines: int = 200, source: str = "recent") -> str:
        lines = _bounded_lines(lines)
        if source not in READ_SOURCES:
            raise DriverUsageError(f"unsupported read source {source!r}")
        command = [
            "agent",
            "read",
            _require_text(target, "agent target"),
            "--source",
            source,
            "--lines",
            str(lines),
        ]
        return self._runner(command)

    def agent_get(self, target: str) -> dict[str, Any]:
        command = ["agent", "get", _require_text(target, "agent target")]
        result = _read_envelope(self._runner(command), ("agent_info",))
        return dict(_require_mapping(result, "agent"))

    def agent_wait(
        self,
        target: str,
        *,
        until: Sequence[str] = (),
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        command = ["agent", "wait", _require_text(target, "agent target")]
        for state in until:
            if state not in WAIT_STATES:
                raise DriverUsageError(f"unsupported wait state {state!r}")
            command += ["--until", state]
        if timeout_ms is not None:
            timeout_ms = _bounded_timeout_ms(timeout_ms, timeout_ms)
            command += ["--timeout", str(timeout_ms)]
        result = _read_envelope(
            self._run(command, timeout_ms), ("agent_wait", "agent_waited", "agent_info")
        )
        return dict(result)
