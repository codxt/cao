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
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from . import agent_profiles, team_approval, state as state_module

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
    "TeamModelPolicy",
    "team_model_policy",
    "route_model",
    "effective_route_model",
    "safe_route_token",
    "model_policy_error",
    "SUPPORTED_TEAM_ROUTES",
    "NON_ZDR_ROUTES",
    "TASK_SECTION",
    "DEFAULT_WORKSPACE_LABEL_PREFIX",
    "DEFAULT_RUNNER_TIMEOUT",
    "DEFAULT_START_TIMEOUT_MS",
    "DEFAULT_PROMPT_TIMEOUT_MS",
    "DEFAULT_WAIT_TIMEOUT_MS",
    "DEFAULT_READ_LINES",
    "DEFAULT_MAX_OUTPUT_CHARS",
    "DEFAULT_RUN_ID",
    "DEFAULT_TEAM_STALL_TIMEOUT_MS",
    "MIN_TEAM_STALL_TIMEOUT_MS",
    "MAX_TEAM_STALL_TIMEOUT_MS",
    "team_stall_timeout_ms",
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
#: The liveness bound for one team task execution. It is deliberately far
#: longer than any single model response, is reported only when the task's own
#: worker is gone and its durable state has stopped changing, and never releases
#: a claim or re-prompts an agent.
DEFAULT_TEAM_STALL_TIMEOUT_MS = 7_200_000
MIN_TEAM_STALL_TIMEOUT_MS = 60_000
MAX_TEAM_STALL_TIMEOUT_MS = 604_800_000
#: The only registry attach policy: every product task attaches to the single
#: governed ledger (whose path ``registry_db_path`` resolves). A configuration
#: naming any other policy is refused rather than silently ignored.
REGISTRY_ATTACH_GOVERNED = "governed"

#: Optional, operator-local model policy. Its vocabulary is the owner's
#: authorized CAO/OpenCode dispatch routes (``provider/model``, optionally with
#: a ``#effort`` variant). An absent field preserves the existing OSS behavior;
#: a configured one is a deny gate applied to every NEW dispatch intent.
TEAM_MODEL_ALLOWLIST_FIELD = "team_model_allowlist"
_MAX_TEAM_MODELS = 16
#: Documented owner routes, retained as explicit supported-route examples.
SUPPORTED_TEAM_ROUTES = (
    "opencode/step-5-preview-free",
    "opencode-go/glm-5.3-flash",
    "opencode-go/deepseek-v4.1-flash",
)
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+/-]{0,150}\Z")
#: A dispatch route may carry one bounded ``#effort`` variant (``#high``,
#: ``#xhigh``, ...). The variant changes effort, never the base route, so the
#: resolved argv model is normalized to its base before allowlist comparison.
_MODEL_VARIANT = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:@+/-]{0,150}#[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


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


@dataclass(frozen=True)
class TeamModelPolicy:
    """A validated, operator-local allowlist of dispatch routes.

    ``models`` holds unique normalized ``provider/model`` ids. ``None`` means
    the operator has not configured the field, which preserves existing
    behavior exactly.
    """

    models: frozenset[str]

    def permits(self, model: str) -> bool:
        return _normalize_team_model(model) in self.models


def _normalize_team_model(value: str) -> str:
    """Normalize one dispatch route by dropping an optional ``#effort`` variant."""

    return value.split("#", 1)[0]


def _validate_team_model(value: Any) -> str:
    if ((type(value) is not str or not value
            or not _MODEL_ID.fullmatch(value)
            or ".." in value or "//" in value or "#" in value)):
        raise ProductConfigError(
            "team_model_allowlist must contain safe provider/model ids "
            "without an #effort variant")
    normalized = _normalize_team_model(value)
    if "/" not in normalized or normalized.startswith("/") or normalized.endswith("/"):
        raise ProductConfigError(
            "team_model_allowlist entries must be provider/model ids")
    return normalized


def _team_model_values(raw: Any) -> list[str]:
    """The field's string entries. Only a genuinely ABSENT key opts out.

    An explicit ``[]``, ``null`` or wrong-type value would otherwise create a
    deny-all or a silent bypass through ``team_model_policy``'s ``None``
    return, so every present value must be a bounded nonempty array — it is
    rejected here otherwise.
    """

    if not isinstance(raw, (list, tuple)) or isinstance(raw, (str, bytes)):
        raise ProductConfigError("team_model_allowlist must be an array of strings")
    if not raw:
        raise ProductConfigError(
            "team_model_allowlist must hold at least one entry; omit the field "
            "entirely to keep its behavior unconfigured")
    return list(raw)


def team_model_policy(config: ProductConfig | None = None) -> TeamModelPolicy | None:
    """The configured model allowlist, or ``None`` when the field is ABSENT.

    ``None`` is only ever produced by a genuinely absent field, never by an
    explicit ``[]`` or ``null`` value: those raise ``ProductConfigError``
    above so strict enforcement can never be silently disabled. Fail closed:
    a malformed field raises rather than being silently ignored.
    """

    if config is None or TEAM_MODEL_ALLOWLIST_FIELD not in config.raw:
        return None
    values = _team_model_values(config.raw[TEAM_MODEL_ALLOWLIST_FIELD])
    if len(values) > _MAX_TEAM_MODELS:
        raise ProductConfigError(
            f"team_model_allowlist must hold at most {_MAX_TEAM_MODELS} entries")
    normalized = [_validate_team_model(item) for item in values]
    if len(set(normalized)) != len(normalized):
        raise ProductConfigError(
            "team_model_allowlist entries must be unique after normalization")
    return TeamModelPolicy(models=frozenset(normalized))


def safe_route_token(value: Any) -> str | None:
    return value if type(value) is str and _MODEL_ID.fullmatch(value) else None


def safe_route_variant(value: Any) -> str | None:
    """A safe bounded route token, tolerating one trailing ``#effort`` variant."""

    return (value if type(value) is str and (
        _MODEL_ID.fullmatch(value) or _MODEL_VARIANT.fullmatch(value))
        else None)


def _argv_model(arguments: Any) -> tuple[str | None, int, bool]:
    """One agent argv's declared ``--model`` switch, without echoing raw argv.

    Returns ``(value, mentions, trap)``: ``value`` is the bounded token of the
    single ``--model`` value (``None`` when that value is missing, unsafe or in
    ``--model=value`` form), ``mentions`` counts ``--model`` switches, and
    ``trap`` is set when a ``--model`` token is really the value of a preceding
    flag. A caller treats more than one mention, an unusable value or a trap as
    ambiguous rather than guessing which token is the model.
    """

    if type(arguments) is not list:
        return None, 0, False
    mentions = 0
    value: str | None = None
    trap = False
    for index, argument in enumerate(arguments):
        if type(argument) is not str:
            continue
        if argument.startswith("--model="):
            mentions += 1
            value = None
        elif argument == "--model":
            mentions += 1
            previous = arguments[index - 1] if index else None
            # ``--standalone`` is a value-less switch CAO's own dispatch emits
            # immediately before its ``--model`` selection, so it never
            # consumes the switch; every other preceding flag may own the
            # ``--model`` token as its value and stays ambiguous.
            if (type(previous) is str and previous.startswith("-")
                    and previous != "--standalone"):
                trap = True
            value = (safe_route_variant(arguments[index + 1])
                     if index + 1 < len(arguments) else None)
    return value, mentions, trap


def effective_route_model(provider_model: Any, value: Any,
                          mentions: int, trap: bool) -> str | None:
    """The one model the provider entry and the verified argv agree on, else None.

    A resolved model must never misreport what dispatch would select: an
    ambiguous, invalid or divergent selection is reported unresolved. A
    bounded ``#effort`` variant is the same base route as its provider
    selection, so agreement is compared on normalized base ids and the
    effective argv selection is reported.
    """

    provider_model = safe_route_variant(provider_model)
    if mentions == 0:
        return provider_model
    if mentions > 1 or value is None or trap:
        return None
    if provider_model is not None:
        return (value
                if _normalize_team_model(value)
                == _normalize_team_model(provider_model) else None)
    return value


def route_model(route: Any) -> tuple[str, str | None]:
    """The one model a resolved route would dispatch, else ``('unresolved', None)``.

    The provider ``model`` and any single ``--model`` value in the verified
    agent argv must agree; a repeated, missing, flag-trapped or divergent
    selection is ambiguous and never reported as one resolved model.
    """

    if not isinstance(route, Mapping):
        return "unresolved", None
    provider = route.get("provider")
    provider_model = (provider.get("model")
                      if isinstance(provider, Mapping) else None)
    value, mentions, trap = _argv_model(route.get("agent_args"))
    model = effective_route_model(provider_model, value, mentions, trap)
    return ("resolved", model) if model is not None else ("unresolved", None)


#: Allowed ``opencode/``, ``opencode-go/`` and ``codex/`` model ids name the
#: dispatch routes themselves, so a resolved route that lists one must carry
#: the actual agent kind and provider CLI. A model string alone never proves a
#: runtime identity or a paid provider, so a foreign or missing agent kind or
#: provider CLI is refused before any effect instead of substituting another
#: runtime. Routes for other provider prefixes keep generic behavior.
_TEAM_ROUTE_RUNTIME = {
    "opencode": ("opencode", "opencode"),
    "opencode-go": ("opencode", "opencode"),
    "codex": ("codex", "codex"),
}

#: Runtime identities whose allowlisted routes are authorized for READ only.
#: A Codex profile is a read-only reader/reviewer in this policy: a WRITE
#: dispatch bound to the Codex runtime — by its canonical identity prefix or by
#: its resolved agent kind / provider CLI — is refused even when its model id is
#: allowlisted, so the read-only role can never be escalated into a writer.
_READ_ONLY_ROUTE_RUNTIMES = frozenset({"codex"})

#: Route identities the canonical owner policy restricts to privacy-qualified
#: READ. The Step-5 Preview Free route may read its own bounded checkout but
#: must never become a writer, so a WRITE dispatch bound to it — by its
#: canonical identity, by the base model id behind any provider prefix, or by a
#: bounded ``#effort`` variant — is refused before any claim or agent effect.
#: The bare model id and case-folded copies are matched so neither an alias nor
#: a non-canonical spelling can restore write access.
READ_ONLY_ROUTE_MODELS = frozenset({"opencode/step-5-preview-free"})
_READ_ONLY_MODEL_IDS = frozenset(
    route.rsplit("/", 1)[-1] for route in READ_ONLY_ROUTE_MODELS)
_READ_ONLY_ROUTE_MODELS_FOLDED = frozenset(
    route.lower() for route in READ_ONLY_ROUTE_MODELS)
_READ_ONLY_MODEL_IDS_FOLDED = frozenset(
    route.lower() for route in _READ_ONLY_MODEL_IDS)

#: Route identities whose upstream provider is known not to offer
#: zero-data-retention for submitted input (it may retain or train on it).
#: CAO performs no automated provider-privacy classification and this product
#: adapter cannot technically prove that a checkout or prompt is clean public
#: OSS or sanitized, so these identities are refused at every NEW dispatch.
#: The public-input admission gate is documented but unfulfilled by this build;
#: a route is never silently substituted for them.
#:
#: Ceiling: this is a conservative build-time classification. A non-ZDR route
#: that is not listed here, but is allowlisted, is governed only by the exact
#: allowlist and the READ/WRITE and identity checks. Replacing it with an
#: operator-declared, technically verified admission is the upgrade path.
NON_ZDR_ROUTES = frozenset({"opencode-go/gemini-4-argon-contributor"})
#: The bare model ids behind :data:`NON_ZDR_ROUTES`, matched regardless of a
#: provider prefix so a foreign runtime cannot launder the same model name.
_NON_ZDR_MODEL_IDS = frozenset(
    route.rsplit("/", 1)[-1] for route in NON_ZDR_ROUTES)
#: Case-folded copies used by the refusal comparison: a non-canonical spelling
#: of the same route id must not launder the provider past the gate.
_NON_ZDR_ROUTES_FOLDED = frozenset(route.lower() for route in NON_ZDR_ROUTES)
_NON_ZDR_MODEL_IDS_FOLDED = frozenset(
    route.lower() for route in _NON_ZDR_MODEL_IDS)


def _canonical_route_identity(model: str, cli: Any) -> str:
    """The canonical allowlist identity of one resolved route.

    A bare ``--model`` token resolved through a named provider CLI is qualified
    with that exact provider prefix, so a native Codex ``--model gpt-6.1-sol``
    has the canonical allowlist identity ``codex/gpt-6.1-sol`` and a Medium or
    High reasoning-effort variant of the same model is the same base identity.
    A model that already carries a provider prefix is its own identity; a route
    with no trustworthy CLI keeps the bare token, which cannot match the
    ``provider/model`` allowlist shape and therefore fails closed.
    """

    if "/" in model:
        return model
    token = safe_route_token(cli)
    return model if token is None else f"{token}/{model}"


def _route_mode(value: Any) -> str | None:
    return value.strip().lower() if type(value) is str else None


def model_policy_error(route: Any, policy: TeamModelPolicy | None, *,
                       mode: Any = None) -> str | None:
    """The refusal reason when a configured policy forbids this route.

    The model is read only from the provider entry and the verified agent argv,
    never from an untrusted prompt. With a configured policy an ambiguous,
    missing, malformed or unlisted selection is denied rather than silently
    substituted, and a ``#effort`` variant is accepted as the same base route.
    A listed ``opencode/``, ``opencode-go/`` or ``codex/`` model id additionally
    binds the route identity: the resolved agent kind and the provider CLI must
    both be the actual authorized runtime, so a spoofed foreign CLI can never
    pass on an allowed-looking model id.

    ``mode`` is the declared task mode (``read``/``write``) when the caller
    knows it. A route bound to a read-only runtime (Codex) — through its
    canonical identity prefix or through its resolved agent kind / provider CLI
    — is refused unless the mode is exactly READ; an unknown mode is refused
    rather than assumed safe. A route the canonical owner policy restricts to
    privacy-qualified READ (the Step-5 Preview Free base model id, matched
    through its canonical identity, any provider prefix or a bounded
    ``#effort`` variant) is likewise refused unless the mode is exactly READ. A
    route whose provider is known not to be zero-data-retention is always
    refused: the public-input admission gate this build would require is not
    technically verifiable.
    """

    if policy is None:
        return None
    status, model = route_model(route)
    if status != "resolved" or model is None:
        return "TEAM_MODEL_UNRESOLVED"
    provider = route.get("provider")
    cli = (provider.get("cli")
           if isinstance(provider, Mapping) else None)
    identity = _canonical_route_identity(model, cli)
    prefix = identity.split("/", 1)[0]
    required = _TEAM_ROUTE_RUNTIME.get(prefix)
    # A route whose canonical identity binds a known runtime is matched ONLY as
    # that exact canonical identity, so a bare or foreign alias can never stand
    # in for it. Routes for other provider prefixes keep the generic behavior
    # (the resolved model or its qualified identity may be listed).
    permitted = (policy.permits(identity) if required is not None
                 else (policy.permits(model) or policy.permits(identity)))
    if not permitted:
        return "TEAM_MODEL_NOT_ALLOWED"
    if required is not None:
        if (safe_route_token(route.get("agent_kind")) != required[0]
                or safe_route_token(cli) != required[1]):
            return "TEAM_ROUTE_IDENTITY_MISMATCH"
    # Compare on the base route, case-folded: a bounded ``#effort`` variant and
    # a non-canonical spelling never change the upstream provider, so neither
    # may slip past the non-zero-data-retention refusal the canonical id is
    # subject to.
    base_model = _normalize_team_model(model)
    base_identity = _normalize_team_model(identity).lower()
    base_model_id = base_model.rsplit("/", 1)[-1].lower()
    if (base_identity in _NON_ZDR_ROUTES_FOLDED
            or base_model_id in _NON_ZDR_MODEL_IDS_FOLDED):
        return "TEAM_ROUTE_PRIVACY_UNVERIFIED"
    # A Codex route binds its runtime either through its canonical identity
    # prefix or through its resolved agent kind / provider CLI, so a
    # foreign-prefixed model id (which would otherwise take the generic path)
    # still counts as Codex.
    kind_token = safe_route_token(route.get("agent_kind"))
    cli_token = safe_route_token(cli)
    codex_bound = (prefix in _READ_ONLY_ROUTE_RUNTIMES
                   or kind_token in _READ_ONLY_ROUTE_RUNTIMES
                   or cli_token in _READ_ONLY_ROUTE_RUNTIMES)
    # A Codex route must name its model through exactly one unambiguous native
    # ``--model`` argument. A model that appears only in the provider entry
    # would launch Codex with no explicit selection, letting its implicit
    # default stand in for the allowlisted model, so it is unresolved.
    if codex_bound:
        _, mentions, trap = _argv_model(route.get("agent_args"))
        if mentions != 1 or trap:
            return "TEAM_MODEL_UNRESOLVED"
    # A route the canonical owner policy restricts to privacy-qualified READ is
    # refused unless the mode is exactly READ, so the reader can never be
    # escalated into a writer. The restriction follows the base model id
    # (case-folded, behind any provider prefix and any bounded ``#effort``
    # variant), so an alias or a foreign prefix cannot restore write access.
    if ((codex_bound
            or base_identity in _READ_ONLY_ROUTE_MODELS_FOLDED
            or base_model_id in _READ_ONLY_MODEL_IDS_FOLDED)
            and _route_mode(mode) != "read"):
        return "TEAM_ROUTE_READ_ONLY"
    return None


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

    # Portable operator governance. Absence preserves legacy OSS behavior;
    # a configured signed gate is enforced on every new team initialization.
    try:
        team_approval.policy(SimpleNamespace(raw=payload))
    except team_approval.ApprovalError as exc:
        raise ProductConfigError(str(exc)) from exc

    # Optional operator-local model allowlist. ABSENCE (no such key)
    # preserves existing behavior; a present empty [], null, wrong type,
    # malformed, duplicate or over-long list is refused here rather than
    # silently disabling the gate or bypassing it at dispatch time.
    team_model_policy(ProductConfig(
        schema=schema, runtime_id=runtime_id, provider=provider,
        managed=None, raw=dict(payload)))

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
        # an idle worker without performing the intended task. A bounded
        # ``#effort`` variant of the same base route is still the configured
        # selection; a different base id is refused.
        matches = [i for i, arg in enumerate(agent_args) if arg == "--model"]
        if (not agent_args or agent_args[0] != "mini"
                or len(matches) != 1
                or matches[0] + 1 >= len(agent_args)
                or _normalize_team_model(agent_args[matches[0] + 1])
                != _normalize_team_model(config.provider.model)
                or safe_route_variant(agent_args[matches[0] + 1]) is None):
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


def team_stall_timeout_ms(config: ProductConfig | None = None) -> int:
    """The conservative liveness bound for one team task execution.

    Read from the same optional ``task`` section as the other task timeouts and
    deliberately without requiring an agent kind: a liveness bound must stay
    readable for any configuration, including one that cannot launch an agent.
    """

    raw: dict[str, Any] = {}
    if config is not None:
        section = config.raw.get(TASK_SECTION)
        if section is not None:
            if not isinstance(section, Mapping):
                raise ProductConfigError("task must be an object")
            raw = dict(section)
    value = raw.get("team_stall_timeout_ms")
    if value is None:
        return DEFAULT_TEAM_STALL_TIMEOUT_MS
    if (isinstance(value, bool) or not isinstance(value, int)
            or not MIN_TEAM_STALL_TIMEOUT_MS <= value <= MAX_TEAM_STALL_TIMEOUT_MS):
        raise ProductConfigError(
            "task.team_stall_timeout_ms must be an integer between "
            f"{MIN_TEAM_STALL_TIMEOUT_MS} and {MAX_TEAM_STALL_TIMEOUT_MS}")
    return value


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
    #: Opt-in, default-disabled: the existing daemon reconciles the B7 durable
    #: Intake execution queue on its own tick. It defaults to ``False`` so a
    #: fresh or unconfigured product performs no Intake reconciliation at all.
    intake_queue: bool = False
    #: Opt-in, default-disabled B8 flag: the existing daemon runs the bounded
    #: deterministic Intake pipeline (discovery/triage/enqueue) and supplies the
    #: verified standing authority when the host has provisioned one. It defaults
    #: to ``False`` so a fresh or unconfigured product discovers nothing.
    intake_pipeline: bool = False


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
    intake_queue = raw.get("intake_queue", False)
    if type(intake_queue) is not bool:
        raise ProductConfigError("daemon.intake_queue must be true or false")
    intake_pipeline = raw.get("intake_pipeline", False)
    if type(intake_pipeline) is not bool:
        raise ProductConfigError("daemon.intake_pipeline must be true or false")
    return DaemonDefaults(
        engineering_queue_dir=queue_dir,
        engineering_watch_file=engineering_watch,
        feedback_watch_file=watch,
        intake_queue=intake_queue,
        intake_pipeline=intake_pipeline,
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
