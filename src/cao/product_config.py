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

from . import state as state_module

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
]

SCHEMA = "cao.product.config.v0"
APP_DIR = "cao"
CONFIG_FILENAME = "config.json"


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
