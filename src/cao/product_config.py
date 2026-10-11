"""Product configuration for a source-installed CAO.

The source bootstrap (``setup.sh``) writes one explicit product configuration
into the XDG config directory. This module owns that document's vocabulary and
its XDG path resolution; the atomic, fail-loud read/write mechanics come from
:mod:`cao.state`.

No credential is ever read, stored or echoed here. A provider entry records
only the agent CLI to use, the provider id it must be authenticated for, and
the explicit model name the operator chose.

The module is deliberately runtime-neutral: the managed runtime's identity and
its pinned release coordinates are *data* written by the bootstrap, never
constants baked into this code.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from . import agent_profiles, state as state_module

__all__ = [
    "SCHEMA",
    "APP_DIR",
    "CONFIG_FILENAME",
    "ProductConfigError",
    "ProviderConfig",
    "ProductConfig",
    "config_home",
    "config_dir",
    "config_path",
    "read_config",
    "validate_config",
    "load_config",
    "write_config",
    "initialize_config",
    "TASK_SECTION",
    "DEFAULT_WORKSPACE_LABEL_PREFIX",
    "DEFAULT_RUNNER_TIMEOUT",
    "DEFAULT_START_TIMEOUT_MS",
    "DEFAULT_PROMPT_TIMEOUT_MS",
    "DEFAULT_WAIT_TIMEOUT_MS",
    "DEFAULT_READ_LINES",
    "DEFAULT_MAX_OUTPUT_CHARS",
    "DEFAULT_RUN_ID",
    "REGISTRY_ATTACH_GOVERNED",
    "TaskDefaults",
    "task_defaults",
    "registry_db_path",
    "DAEMON_SECTION",
    "DEFAULT_TICK_INTERVAL",
    "MIN_TICK_INTERVAL",
    "MAX_TICK_INTERVAL",
    "DEFAULT_WAKE_BUDGET",
    "DEFAULT_RETRY_BOUND",
    "MAX_RETRY_BOUND",
    "DEFAULT_STOP_TIMEOUT",
    "DaemonDefaults",
    "daemon_defaults",
]

SCHEMA = "cao.product.config.v0"
APP_DIR = "cao"
CONFIG_FILENAME = "config.json"

#: The optional ``task`` section of the product configuration. It carries the
#: operator's explicit task defaults; every field has a safe fallback.
TASK_SECTION = "task"
DEFAULT_WORKSPACE_LABEL_PREFIX = "cao-review-"
DEFAULT_RUNNER_TIMEOUT = 30.0
DEFAULT_START_TIMEOUT_MS = 120_000
DEFAULT_PROMPT_TIMEOUT_MS = 300_000
DEFAULT_WAIT_TIMEOUT_MS = 300_000
DEFAULT_READ_LINES = 200
DEFAULT_MAX_OUTPUT_CHARS = 20_000
DEFAULT_RUN_ID = "cao-product"
#: The only registry attach policy: every product task attaches to the single
#: governed ledger (whose path ``registry_db_path`` resolves). A configuration
#: naming any other policy is refused rather than silently ignored.
REGISTRY_ATTACH_GOVERNED = "governed"


class ProductConfigError(Exception):
    """The product configuration is malformed or of an unsupported schema."""

    code = "PRODUCT_CONFIG_INVALID"


def _ensure_dir(path: str) -> None:
    """Create a private directory if it is missing."""

    if path and not os.path.isdir(path):
        os.makedirs(path, mode=0o700, exist_ok=True)


def config_home(env: Mapping[str, str] | None = None) -> str:
    """The XDG config root (``XDG_CONFIG_HOME`` or ``~/.config``)."""

    source = os.environ if env is None else env
    base = source.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.abspath(base)


def config_dir(env: Mapping[str, str] | None = None) -> str:
    return os.path.join(config_home(env), APP_DIR)


def config_path(env: Mapping[str, str] | None = None) -> str:
    return os.path.join(config_dir(env), CONFIG_FILENAME)


@dataclass(frozen=True)
class ProviderConfig:
    """The explicitly chosen agent provider. Never carries a secret value."""

    cli: str | None = None
    provider_id: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class ProductConfig:
    """A validated product configuration document."""

    schema: str
    runtime_id: str | None
    provider: ProviderConfig | None
    managed: Mapping[str, Any] | None
    raw: Mapping[str, Any] = field(repr=False)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.raw)


def _optional_nonempty_str(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ProductConfigError(f"{field_name} must be a non-empty string")
    return value


def validate_config(payload: Mapping[str, Any]) -> ProductConfig:
    """Validate a raw configuration document, failing closed on any defect."""

    if not isinstance(payload, Mapping):
        raise ProductConfigError(
            f"configuration must be an object, got {type(payload).__name__}"
        )
    schema = payload.get("schema")
    if schema != SCHEMA:
        raise ProductConfigError(
            f"unsupported configuration schema {schema!r}; expected {SCHEMA!r}"
        )
    runtime_id = _optional_nonempty_str(payload.get("runtime_id"), "runtime_id")
    ledger = payload.get("registry_db")
    if ledger is not None and (
        type(ledger) is not str or not ledger or len(ledger) > 2048
        or not os.path.isabs(ledger) or os.path.normpath(ledger) != ledger
    ):
        raise ProductConfigError("registry_db must be a normalized absolute path")

    provider: ProviderConfig | None = None
    raw_provider = payload.get("provider")
    if raw_provider is not None:
        if not isinstance(raw_provider, Mapping):
            raise ProductConfigError("provider must be an object")
        provider = ProviderConfig(
            cli=_optional_nonempty_str(raw_provider.get("cli"), "provider.cli"),
            provider_id=_optional_nonempty_str(
                raw_provider.get("id"), "provider.id"
            ),
            model=_optional_nonempty_str(raw_provider.get("model"), "provider.model"),
        )

    # Agent launch preferences are operator-local data; reject unknown flags,
    # executable identifiers and unsafe forms before task admission.
    try:
        agent_profiles.validate_profiles(payload.get("agent_profiles"))
    except agent_profiles.AgentProfileError as exc:
        raise ProductConfigError(str(exc)) from exc

    managed = payload.get("managed")
    if managed is not None and not isinstance(managed, Mapping):
        raise ProductConfigError("managed must be an object")

    return ProductConfig(
        schema=schema,
        runtime_id=runtime_id,
        provider=provider,
        managed=managed,
        raw=dict(payload),
    )


def read_config(path: str | os.PathLike[str] | None = None) -> dict[str, Any] | None:
    """Read the raw document, or ``None`` when it does not exist yet.

    An unreadable or malformed file raises; it is never treated as absent.
    """

    return state_module.read_state(path if path is not None else config_path())


def load_config(path: str | os.PathLike[str] | None = None) -> ProductConfig | None:
    """Read and validate the configuration, or ``None`` when absent."""

    raw = read_config(path)
    if raw is None:
        return None
    return validate_config(raw)


def write_config(
    config: Mapping[str, Any], path: str | os.PathLike[str] | None = None
) -> dict[str, Any]:
    """Atomically write a validated configuration document."""

    validate_config(config)
    resolved = os.fspath(path) if path is not None else config_path()
    _ensure_dir(os.path.dirname(resolved))
    return state_module.write_state(resolved, config)


def initialize_config(
    managed: Mapping[str, Any] | None,
    runtime_id: str | None,
    *,
    path: str | os.PathLike[str] | None = None,
    force: bool = False,
) -> tuple[ProductConfig, bool]:
    """Idempotently install the managed-runtime fields into the config.

    The operator's provider selection is never overwritten. An existing file is
    validated first and, if it is malformed, the write is refused rather than
    clobbering bytes this code did not understand. Returns the validated
    configuration and whether the file was created.
    """

    resolved = os.fspath(path) if path is not None else config_path()
    existing = read_config(resolved)
    if existing is not None:
        validate_config(existing)

    updated: dict[str, Any] = dict(existing) if existing is not None else {}
    updated["schema"] = SCHEMA
    if runtime_id is not None:
        updated["runtime_id"] = runtime_id
    if managed is not None:
        updated["managed"] = dict(managed)

    if existing is None or force or updated != existing:
        write_config(validate_config(updated).as_dict(), resolved)

    loaded = load_config(resolved)
    assert loaded is not None  # just written or already present
    return loaded, existing is None


# ---------------------------------------------------------------------------
# Task defaults (task-orchestration lane)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TaskDefaults:
    """Validated task defaults for the product flow.

    ``agent_kind`` is the runtime's canonical agent kind to start; it falls
    back to the configured provider CLI. It is ``None`` when neither is set,
    and the task layer then reports the task layer unavailable rather than
    guessing a kind.
    """

    agent_kind: str | None
    agent_args: tuple[str, ...]
    workspace_label_prefix: str
    runner_timeout: float
    start_timeout_ms: int
    prompt_timeout_ms: int
    wait_timeout_ms: int
    read_lines: int
    max_output_chars: int
    run_id: str
    registry_attach: str


def _positive_number(value: Any, what: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ProductConfigError(f"{what} must be a positive number")
    return float(value)


def _positive_int(value: Any, what: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProductConfigError(f"{what} must be a positive integer")
    return value


def _string_tuple(value: Any, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ProductConfigError(f"{what} must be an array of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ProductConfigError(f"{what} must contain non-empty strings")
        out.append(item)
    return tuple(out)


def task_defaults(
    config: ProductConfig | None = None,
    env: Mapping[str, str] | None = None,
) -> TaskDefaults:
    """Read and validate the optional task defaults, failing closed on defect."""

    raw: dict[str, Any] = {}
    if config is not None:
        section = config.raw.get(TASK_SECTION)
        if section is not None:
            if not isinstance(section, Mapping):
                raise ProductConfigError("task must be an object")
            raw = dict(section)

    provider_cli = None
    if config is not None and config.provider is not None:
        provider_cli = config.provider.cli
    agent_kind = raw.get("agent_kind") if raw.get("agent_kind") is not None else provider_cli
    agent_kind = _optional_nonempty_str(agent_kind, "task.agent_kind")

    prefix = (
        _optional_nonempty_str(
            raw.get("workspace_label_prefix"), "task.workspace_label_prefix"
        )
        or DEFAULT_WORKSPACE_LABEL_PREFIX
    )
    if prefix.startswith("-"):
        raise ProductConfigError(
            "task.workspace_label_prefix must not start with '-'"
        )

    attach = (
        _optional_nonempty_str(raw.get("registry_attach"), "task.registry_attach")
        or REGISTRY_ATTACH_GOVERNED
    )
    if attach != REGISTRY_ATTACH_GOVERNED:
        raise ProductConfigError(
            f"task.registry_attach must be {REGISTRY_ATTACH_GOVERNED!r}; no other "
            f"ledger policy is supported"
        )

    agent_args = _string_tuple(raw.get("agent_args"), "task.agent_args")
    if (
        raw.get("agent_args") is None
        and agent_kind == "opencode"
        and provider_cli == "opencode"
        and config is not None
        and config.provider is not None
        and config.provider.model is not None
    ):
        # The selected model must reach the actual execution process. Starting
        # bare OpenCode launches an interactive TUI with its own default model,
        # which can be reported idle before any governed task is performed.
        agent_args = ("mini", "--standalone", "--model", config.provider.model)
    elif (agent_kind == "opencode" and provider_cli == "opencode"
          and config is not None and config.provider is not None
          and config.provider.model is not None):
        # An advanced argv override must still identify the configured model.
        # A bare OpenCode TUI or silent model substitution can otherwise report
        # an idle worker without performing the intended task.
        matches = [i for i, arg in enumerate(agent_args) if arg == "--model"]
        if (not agent_args or agent_args[0] != "mini"
                or len(matches) != 1
                or matches[0] + 1 >= len(agent_args)
                or agent_args[matches[0] + 1] != config.provider.model):
            raise ProductConfigError(
                "task.agent_args must select opencode mini and exactly provider.model"
            )

    return TaskDefaults(
        agent_kind=agent_kind,
        agent_args=agent_args,
        workspace_label_prefix=prefix,
        runner_timeout=_positive_number(
            raw.get("runner_timeout"), "task.runner_timeout", DEFAULT_RUNNER_TIMEOUT
        ),
        start_timeout_ms=_positive_int(
            raw.get("start_timeout_ms"),
            "task.start_timeout_ms",
            DEFAULT_START_TIMEOUT_MS,
        ),
        prompt_timeout_ms=_positive_int(
            raw.get("prompt_timeout_ms"),
            "task.prompt_timeout_ms",
            DEFAULT_PROMPT_TIMEOUT_MS,
        ),
        wait_timeout_ms=_positive_int(
            raw.get("wait_timeout_ms"), "task.wait_timeout_ms", DEFAULT_WAIT_TIMEOUT_MS
        ),
        read_lines=_positive_int(
            raw.get("read_lines"), "task.read_lines", DEFAULT_READ_LINES
        ),
        max_output_chars=_positive_int(
            raw.get("max_output_chars"),
            "task.max_output_chars",
            DEFAULT_MAX_OUTPUT_CHARS,
        ),
        run_id=_optional_nonempty_str(raw.get("run_id"), "task.run_id")
        or DEFAULT_RUN_ID,
        registry_attach=attach,
    )


def registry_db_path(env: Mapping[str, str] | None = None) -> str:
    """The Work Registry ledger the product task layer attaches to.

    ``CAO_REGISTRY_DB`` overrides the path. Otherwise an explicit
    ``registry_db`` in the CAO product config wins over the isolated
    XDG default. An existing shared dispatcher must be configured explicitly;
    merely using CAO on the same host does not discover the shared ledger.
    """

    source = os.environ if env is None else env
    override = source.get("CAO_REGISTRY_DB")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    configured = read_config(config_path(source))
    if configured is not None:
        validated = validate_config(configured)
        selected = validated.raw.get("registry_db")
        if selected is not None:
            return selected
    from . import registry as _registry

    base = source.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(os.path.abspath(base), _registry.APP_DIR, _registry.DB_NAME)


# ---------------------------------------------------------------------------
# Daemon defaults (daemon / autonomous-orchestration lane)
# ---------------------------------------------------------------------------

#: The optional ``daemon`` section of the product configuration. Its fields
#: bound the orchestration loop; every one has a safe fallback.
DAEMON_SECTION = "daemon"
DEFAULT_TICK_INTERVAL = 30.0
MIN_TICK_INTERVAL = 1.0
MAX_TICK_INTERVAL = 3600.0
DEFAULT_WAKE_BUDGET = 60
DEFAULT_RETRY_BOUND = 3
MAX_RETRY_BOUND = 100
DEFAULT_STOP_TIMEOUT = 10.0


@dataclass(frozen=True)
class DaemonDefaults:
    """Validated daemon bounds; a defect is refused, never silently relaxed."""

    tick_interval: float
    min_tick_interval: float
    max_tick_interval: float
    wake_budget: int
    retry_bound: int
    stop_timeout: float
    feedback_watch_file: str | None = None
    engineering_watch_file: str | None = None
    engineering_queue_dir: str | None = None


def daemon_defaults(
    config: ProductConfig | None = None,
    env: Mapping[str, str] | None = None,
) -> DaemonDefaults:
    """Read and validate the optional daemon bounds, failing closed."""

    raw: dict[str, Any] = {}
    if config is not None:
        section = config.raw.get(DAEMON_SECTION)
        if section is not None:
            if not isinstance(section, Mapping):
                raise ProductConfigError("daemon must be an object")
            raw = dict(section)

    min_interval = _positive_number(
        raw.get("min_tick_interval"), "daemon.min_tick_interval", MIN_TICK_INTERVAL
    )
    max_interval = _positive_number(
        raw.get("max_tick_interval"), "daemon.max_tick_interval", MAX_TICK_INTERVAL
    )
    if min_interval > max_interval:
        raise ProductConfigError(
            "daemon.min_tick_interval must not exceed daemon.max_tick_interval"
        )
    interval = _positive_number(
        raw.get("tick_interval"), "daemon.tick_interval", DEFAULT_TICK_INTERVAL
    )
    if not min_interval <= interval <= max_interval:
        raise ProductConfigError(
            f"daemon.tick_interval must be within "
            f"[{min_interval}, {max_interval}] seconds"
        )
    retry_bound = _positive_int(
        raw.get("retry_bound"), "daemon.retry_bound", DEFAULT_RETRY_BOUND
    )
    if retry_bound > MAX_RETRY_BOUND:
        raise ProductConfigError(
            f"daemon.retry_bound must be at most {MAX_RETRY_BOUND}"
        )
    watch = raw.get("feedback_watch_file")
    if watch is not None and (
        type(watch) is not str or not watch or len(watch) > 2048
        or not os.path.isabs(watch) or os.path.normpath(watch) != watch
    ):
        raise ProductConfigError("daemon.feedback_watch_file must be a normalized absolute path")
    engineering_watch = raw.get("engineering_watch_file")
    if engineering_watch is not None and (
        type(engineering_watch) is not str or not engineering_watch
        or len(engineering_watch) > 2048 or not os.path.isabs(engineering_watch)
        or os.path.normpath(engineering_watch) != engineering_watch
    ):
        raise ProductConfigError("daemon.engineering_watch_file must be a normalized absolute path")
    queue_dir = raw.get("engineering_queue_dir")
    if queue_dir is not None and (
        type(queue_dir) is not str or not queue_dir or len(queue_dir) > 2048
        or not os.path.isabs(queue_dir) or os.path.normpath(queue_dir) != queue_dir
    ):
        raise ProductConfigError("daemon.engineering_queue_dir must be a normalized absolute path")
    if queue_dir is not None and engineering_watch is not None:
        raise ProductConfigError("choose one engineering watch or engineering queue")
    return DaemonDefaults(
        engineering_queue_dir=queue_dir,
        engineering_watch_file=engineering_watch,
        feedback_watch_file=watch,
        tick_interval=interval,
        min_tick_interval=min_interval,
        max_tick_interval=max_interval,
        wake_budget=_positive_int(
            raw.get("wake_budget"), "daemon.wake_budget", DEFAULT_WAKE_BUDGET
        ),
        retry_bound=retry_bound,
        stop_timeout=_positive_number(
            raw.get("stop_timeout"), "daemon.stop_timeout", DEFAULT_STOP_TIMEOUT
        ),
    )
