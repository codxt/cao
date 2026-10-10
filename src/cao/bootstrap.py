"""Source bootstrap, managed-dependency installation and readiness reporting.

``setup.sh`` owns the pinned managed-runtime coordinates and calls
``python3 -m cao.bootstrap install``. This module turns that spec into a
validated product configuration, manages the pinned runtime binary, and
answers ``./cao doctor`` with exact per-subsystem evidence.

It is deliberately runtime-neutral: the runtime id, its release URLs and its
pinned digests arrive as data (an environment spec or an existing product
configuration). No runtime name is a constant here, and no plugin package is
imported.

Managed-dependency installation (the mechanism only, never runtime-specific)
------------------------------------------------------------------------

* resolve the host platform to a stable key;
* build a validated :class:`Asset` from a product-configuration spec;
* fetch the asset over HTTPS from an allowed host with a timeout and size cap;
* verify the pinned SHA-256 *before* any executable is put in place;
* install atomically, so a refused or interrupted fetch cannot corrupt an
  existing executable; and
* adopt an already-installed compatible binary in place, without replacing,
  upgrading or stopping anything.

Every external effect (network, subprocess, PATH lookup) is injected, so tests
run with no network and no real binary.

Readiness is fail-closed. A subsystem that cannot be positively established is
reported ``UNKNOWN``/``UNAVAILABLE`` and never rounded up to ``READY``. When no
provider is configured, when its CLI is missing, or when its credentials cannot
be verified without reading a secret, the verdict stays ``NOT_READY``.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable
from urllib import parse as urlparse
from urllib import request as urlrequest

from . import product_config, product_state, registry
from .state import StateError

__all__ = [
    "INSTALL_SCHEMA",
    "DOCTOR_SCHEMA",
    "POLICIES",
    "CommandResult",
    "Outcome",
    "Asset",
    "ManagedDependencyError",
    "SpecError",
    "UnsupportedPlatform",
    "DownloadError",
    "ProbeError",
    "HashMismatch",
    "InstallError",
    "run_command",
    "platform_key",
    "validate_spec",
    "build_asset",
    "version_matches",
    "probe_version",
    "ensure",
    "managed_spec_from_env",
    "install",
    "detect_providers",
    "doctor",
    "main",
]

INSTALL_SCHEMA = "cao.product.install.v0"
DOCTOR_SCHEMA = "cao.product.doctor.v0"

MANAGED_SPEC_ENV = "CAO_MANAGED_SPEC"
RUNTIME_ID_ENV = "CAO_RUNTIME_ID"

#: Agent CLIs CAO recognises by executable name. Detection never reads a secret.
KNOWN_PROVIDER_CLIS = ("opencode", "claude")

#: Fixed, read-only probes whose *existence output* proves a provider is
#: authenticated. Each probe prints provider identifiers, never secret values.
#: A provider without a probe here is reported ``UNKNOWN``, which stays
#: ``NOT_READY``.
CREDENTIAL_PROBES: dict[str, tuple[str, ...]] = {
    "opencode": ("auth", "list"),
}

#: How many consecutive printed tokens may join into one identifier candidate
#: when matching a configured provider id against an authenticated list. A
#: multi-word display name ("OpenCode Go") needs two; nothing longer is a
#: provider name, so the window stays small and bounded.
_MAX_ID_TOKENS = 4

_READY_RUNTIME_STATES = frozenset({"INSTALLED", "ADOPTED", "ALREADY_INSTALLED"})

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3

DEFAULT_TIMEOUT = 60.0
DEFAULT_SIZE_CAP = 256 * 1024 * 1024
DEFAULT_CHUNK = 256 * 1024

#: Installation policies for :func:`ensure`.
#: ``auto`` installs a managed copy, preferring a compatible existing one;
#: ``adopt`` uses an existing compatible binary and never installs;
#: ``managed`` always installs a managed copy and never adopts;
#: ``verify`` never fetches or installs, only reports what is present.
POLICIES = frozenset({"auto", "adopt", "managed", "verify"})

_OUTCOMES = frozenset(
    {"ALREADY_INSTALLED", "ADOPTED", "INSTALLED", "PENDING", "INCOMPATIBLE", "MISSING"}
)

_MACHINE_ALIASES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "arm64": "aarch64",
    "aarch64": "aarch64",
}


# ---------------------------------------------------------------------------
# Managed-dependency installation (mechanism only)
# ---------------------------------------------------------------------------


class ManagedDependencyError(Exception):
    """Base class for managed-dependency failures."""

    code = "MANAGED_DEPENDENCY_ERROR"


class SpecError(ManagedDependencyError):
    """The managed-dependency spec is malformed or incomplete."""

    code = "MANAGED_SPEC_INVALID"


class UnsupportedPlatform(ManagedDependencyError):
    """No pinned asset is available for this host platform."""

    code = "MANAGED_PLATFORM_UNSUPPORTED"


class DownloadError(ManagedDependencyError):
    """The asset could not be fetched within the timeout and size cap."""

    code = "MANAGED_DOWNLOAD_FAILED"


class ProbeError(ManagedDependencyError):
    """An existing binary could not be probed for its version."""

    code = "MANAGED_PROBE_FAILED"


class HashMismatch(ManagedDependencyError):
    """The fetched bytes do not match the pinned digest."""

    code = "MANAGED_HASH_MISMATCH"


class InstallError(ManagedDependencyError):
    """The verified asset could not be installed atomically."""

    code = "MANAGED_INSTALL_FAILED"


@dataclass(frozen=True)
class CommandResult:
    """A sanitized process result. Raw OS error text is never surfaced."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str]], CommandResult]
Opener = Callable[[str, float], Any]
Which = Callable[[str], "str | None"]


def run_command(argv: Sequence[str], *, timeout: float = 10.0) -> CommandResult:
    """Run one command, returning a sanitized result instead of raising."""

    try:
        proc = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return CommandResult(127, "", "")
    return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def platform_key(system: str | None = None, machine: str | None = None) -> str:
    """Map the host to a stable ``<system>-<arch>`` key.

    An unrecognized architecture raises rather than guessing a compatible one.
    """

    import platform as _platform

    resolved_system = (system or _platform.system()).strip().lower()
    resolved_machine = (machine or _platform.machine()).strip().lower()
    mapped = _MACHINE_ALIASES.get(resolved_machine)
    if mapped is None:
        raise UnsupportedPlatform(
            f"no managed dependency is available for machine {resolved_machine!r}"
        )
    if not resolved_system:
        raise UnsupportedPlatform("host system could not be determined")
    return f"{resolved_system}-{mapped}"


@dataclass(frozen=True)
class Asset:
    """A pinned, validated release asset for one platform."""

    name: str
    version: str
    filename: str
    url: str
    sha256: str
    allowed_hosts: tuple[str, ...]
    size_cap: int
    timeout: float
    probe_argv: tuple[str, ...]


def _is_safe_component(value: str) -> bool:
    """A single path component with no separators and no traversal."""

    if not value or value in {".", ".."}:
        return False
    if os.sep in value or "/" in value:
        return False
    if os.altsep and os.altsep in value:
        return False
    return os.path.basename(value) == value


def _require_str(mapping: Mapping[str, Any], key: str, where: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise SpecError(f"{where}.{key} must be a non-empty string")
    return value


def _positive_int(mapping: Mapping[str, Any], key: str, where: str, default: int) -> int:
    value = mapping.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SpecError(f"{where}.{key} must be a positive integer")
    return value


def _positive_number(
    mapping: Mapping[str, Any], key: str, where: str, default: float
) -> float:
    value = mapping.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise SpecError(f"{where}.{key} must be a positive number")
    return float(value)


def _hosts(mapping: Mapping[str, Any], where: str) -> tuple[str, ...]:
    raw = mapping.get("allowed_hosts")
    if not isinstance(raw, (list, tuple)) or not raw:
        raise SpecError(f"{where}.allowed_hosts must be a non-empty list")
    hosts: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item:
            raise SpecError(f"{where}.allowed_hosts entries must be strings")
        hosts.append(item.strip().lower())
    return tuple(hosts)


def _probe_argv(mapping: Mapping[str, Any], where: str) -> tuple[str, ...]:
    raw = mapping.get("probe_argv", ("version",))
    if not isinstance(raw, (list, tuple)) or not all(
        isinstance(item, str) for item in raw
    ):
        raise SpecError(f"{where}.probe_argv must be a list of strings")
    return tuple(raw)


def validate_spec(spec: Mapping[str, Any]) -> None:
    """Validate a managed-dependency spec independently of any platform."""

    if not isinstance(spec, Mapping):
        raise SpecError("managed spec must be an object")
    name = _require_str(spec, "name", "managed")
    if not _is_safe_component(name):
        raise SpecError("managed.name must be a single safe path component")
    _require_str(spec, "version", "managed")
    _positive_int(spec, "size_cap_bytes", "managed", DEFAULT_SIZE_CAP)
    _positive_number(spec, "timeout_seconds", "managed", DEFAULT_TIMEOUT)
    _hosts(spec, "managed")
    _probe_argv(spec, "managed")
    platforms = spec.get("platforms")
    if not isinstance(platforms, Mapping) or not platforms:
        raise SpecError("managed.platforms must be a non-empty object")
    for key, entry in platforms.items():
        where = f"managed.platforms[{key!r}]"
        if not isinstance(key, str) or not key:
            raise SpecError("managed.platforms keys must be non-empty strings")
        if not isinstance(entry, Mapping):
            raise SpecError(f"{where} must be an object")
        filename = _require_str(entry, "filename", where)
        if not _is_safe_component(filename):
            raise SpecError(f"{where}.filename must be a single safe path component")
        url = _require_str(entry, "url", where)
        parsed = urlparse.urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise SpecError(f"{where}.url must be an absolute https URL")
        _require_str(entry, "sha256", where)


def build_asset(spec: Mapping[str, Any], key: str) -> Asset:
    """Build the validated :class:`Asset` for one platform key."""

    validate_spec(spec)
    platforms = spec["platforms"]
    entry = platforms.get(key)
    if not isinstance(entry, Mapping):
        raise UnsupportedPlatform(
            f"no managed dependency is pinned for platform {key!r}"
        )
    allowed_hosts = _hosts(spec, "managed")
    url = _require_str(entry, "url", f"managed.platforms[{key!r}]")
    host = (urlparse.urlparse(url).hostname or "").lower()
    if host not in allowed_hosts:
        raise SpecError(
            f"managed.platforms[{key!r}].url host {host!r} is not an allowed host"
        )
    sha256 = _require_str(entry, "sha256", f"managed.platforms[{key!r}]").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise SpecError(
            f"managed.platforms[{key!r}].sha256 must be 64 lowercase hex digits"
        )
    return Asset(
        name=_require_str(spec, "name", "managed"),
        version=_require_str(spec, "version", "managed"),
        filename=_require_str(entry, "filename", f"managed.platforms[{key!r}]"),
        url=url,
        sha256=sha256,
        allowed_hosts=allowed_hosts,
        size_cap=_positive_int(spec, "size_cap_bytes", "managed", DEFAULT_SIZE_CAP),
        timeout=_positive_number(spec, "timeout_seconds", "managed", DEFAULT_TIMEOUT),
        probe_argv=_probe_argv(spec, "managed"),
    )


@dataclass(frozen=True)
class Outcome:
    """The result of one :func:`ensure` call."""

    state: str
    path: str | None = None
    version: str | None = None
    sha256: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.state not in _OUTCOMES:
            raise ValueError(f"unknown outcome state {self.state!r}")

    @property
    def usable(self) -> bool:
        return self.state in {"ALREADY_INSTALLED", "ADOPTED", "INSTALLED"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "path": self.path,
            "version": self.version,
            "sha256": self.sha256,
            "detail": self.detail,
        }


def version_matches(reported: str, expected: str) -> bool:
    """True when ``expected`` appears as a whole token in ``reported``.

    Boundary characters deliberately include ``.`` and alphanumerics, so
    ``0.9.3`` does not spuriously match ``0.9.30``.
    """

    if not isinstance(reported, str) or not expected:
        return False
    pattern = r"(?<![0-9A-Za-z_.])" + re.escape(expected) + r"(?![0-9A-Za-z_.])"
    return re.search(pattern, reported) is not None


def probe_version(
    executable: str, probe_argv: Sequence[str], *, runner: Runner, timeout: float = 10.0
) -> str:
    """Run a binary's version probe and return its sanitized stdout."""

    result = runner((executable, *probe_argv))
    if result.returncode != 0:
        raise ProbeError("version probe did not exit successfully")
    return result.stdout.strip()


def _default_opener(url: str, timeout: float) -> Any:
    return urlrequest.urlopen(url, timeout=timeout)


def _content_length(response: Any) -> int | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("Content-Length")
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _safe_unlink(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def download(asset: Asset, dest_dir: str, *, opener: Opener | None = None) -> str:
    """Fetch ``asset`` into a private temp file under ``dest_dir``.

    The URL is re-validated (https and allowed host) before the opener runs.
    ``Content-Length`` is honoured when present; otherwise the stream is read
    in chunks and aborted the moment the size cap is exceeded. Returns the temp
    path; the caller owns its lifecycle.
    """

    parsed = urlparse.urlparse(asset.url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in asset.allowed_hosts:
        raise DownloadError("asset URL is not an allowed https location")

    fetch = opener or _default_opener
    os.makedirs(dest_dir, mode=0o700, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".managed-", suffix=".part", dir=dest_dir)
    try:
        with os.fdopen(fd, "wb") as handle:
            response = fetch(asset.url, asset.timeout)
            try:
                declared = _content_length(response)
                if declared is not None and declared > asset.size_cap:
                    raise DownloadError(
                        f"asset declares {declared} bytes, over the size cap"
                    )
                total = 0
                while True:
                    chunk = response.read(DEFAULT_CHUNK)
                    if not chunk:
                        break
                    if not isinstance(chunk, (bytes, bytearray)):
                        raise DownloadError("asset stream did not yield bytes")
                    total += len(chunk)
                    if total > asset.size_cap:
                        raise DownloadError("asset exceeded the size cap")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        return temp_path
    except Exception:
        _safe_unlink(temp_path)
        raise


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: str, expected: str) -> str:
    """Return the digest, or raise :class:`HashMismatch` without touching dest."""

    actual = sha256_file(path)
    if not hmac.compare_digest(actual, expected.lower()):
        raise HashMismatch("downloaded asset does not match the pinned digest")
    return actual


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_install(temp_path: str, dest_path: str, *, mode: int = 0o755) -> str:
    """Atomically move a verified temp file into place and make it executable."""

    try:
        os.makedirs(os.path.dirname(dest_path) or ".", mode=0o700, exist_ok=True)
        os.chmod(temp_path, mode)
        fd = os.open(temp_path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp_path, dest_path)
        _fsync_dir(os.path.dirname(dest_path) or ".")
    except OSError as exc:
        raise InstallError(f"could not install managed binary: {exc}") from exc
    return dest_path


def _probe(path: str, asset: Asset, runner: Runner) -> str | None:
    try:
        return probe_version(
            path, asset.probe_argv, runner=runner, timeout=asset.timeout
        )
    except (ProbeError, OSError):
        return None


def ensure(
    asset: Asset,
    *,
    install_dir: str,
    policy: str = "auto",
    opener: Opener | None = None,
    runner: Runner | None = None,
    which: Which | None = None,
) -> Outcome:
    """Resolve the managed dependency under the requested policy.

    Never replaces or stops an adopted external binary. A refused download
    leaves any existing managed copy byte-for-byte intact, because the digest is
    verified before the atomic replace.
    """

    if policy not in POLICIES:
        raise SpecError(f"unknown policy {policy!r}")
    resolved_runner: Runner = runner if runner is not None else run_command
    path_lookup: Which = which if which is not None else shutil.which
    install_dir = os.path.abspath(install_dir)
    managed_path = os.path.join(install_dir, asset.name)

    # 1. A managed copy CAO installed earlier.
    if os.path.isfile(managed_path):
        reported = _probe(managed_path, asset, resolved_runner)
        if reported is not None and version_matches(reported, asset.version):
            return Outcome(
                "ALREADY_INSTALLED",
                managed_path,
                asset.version,
                None,
                "managed copy is already present at the pinned version",
            )

    # 2. An existing binary elsewhere on PATH.
    external = path_lookup(asset.name)
    if external and os.path.abspath(external) != managed_path:
        reported = _probe(external, asset, resolved_runner)
        compatible = reported is not None and version_matches(reported, asset.version)
        if compatible and policy in {"auto", "adopt", "verify"}:
            return Outcome(
                "ADOPTED",
                external,
                asset.version,
                None,
                "adopted the existing compatible binary in place",
            )
        if policy == "adopt":
            return Outcome(
                "INCOMPATIBLE",
                external,
                reported,
                None,
                "existing binary is not the pinned version; refusing to replace "
                "or stop it",
            )

    # 3. Verify-only policy never fetches.
    if policy == "verify":
        return Outcome(
            "MISSING" if external is None else "INCOMPATIBLE",
            external,
            None,
            None,
            "no compatible managed dependency is installed and policy is verify",
        )

    # 4. Fetch, verify, and atomically install a private managed copy.
    temp_path = download(asset, install_dir, opener=opener)
    try:
        digest = verify_sha256(temp_path, asset.sha256)
        atomic_install(temp_path, managed_path)
        temp_path = None
    finally:
        _safe_unlink(temp_path)
    return Outcome(
        "INSTALLED",
        managed_path,
        asset.version,
        digest,
        "verified the pinned digest and installed a managed copy",
    )


# ---------------------------------------------------------------------------
# Source bootstrap orchestration
# ---------------------------------------------------------------------------


def _which_default() -> Any:
    return shutil.which


def _runner_default() -> Any:
    return run_command


def managed_spec_from_env(env: Mapping[str, str]) -> dict[str, Any] | None:
    """Parse the managed-dependency spec the bootstrap script exports."""

    raw = env.get(MANAGED_SPEC_ENV)
    if raw is None or raw == "":
        return None
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise SpecError(f"{MANAGED_SPEC_ENV} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, Mapping):
        raise SpecError(f"{MANAGED_SPEC_ENV} must be a JSON object")
    return dict(parsed)


def _outcome_status(state: str) -> str:
    if state in _READY_RUNTIME_STATES:
        return "OK"
    if state == "PENDING":
        return "PENDING"
    return "NOT_READY"


def _install_error(code: str, message: str) -> dict[str, Any]:
    return {
        "schema": INSTALL_SCHEMA,
        "status": "NOT_READY",
        "error": {"code": code, "verdict": "UNAVAILABLE", "message": message},
    }


def install(
    *,
    policy: str = "auto",
    path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    opener: Any = None,
    runner: Any = None,
    which: Any = None,
    force: bool = False,
) -> dict[str, Any]:
    """Install or adopt the pinned runtime dependency and record the outcome.

    Returns a structured payload; failures are reported, never raised to the
    caller, so a bootstrap script always receives machine-readable output.
    """

    source = os.environ if env is None else env
    runner = runner if runner is not None else _runner_default()
    which = which if which is not None else _which_default()
    resolved_config = (
        os.fspath(path) if path is not None else product_config.config_path(source)
    )
    try:
        spec = managed_spec_from_env(source)
        runtime_id = source.get(RUNTIME_ID_ENV) or None
        existing = product_config.read_config(resolved_config)
        if spec is None and existing is None:
            return _install_error(
                "NO_SPEC",
                "no managed-dependency spec was supplied and no product "
                "configuration exists; run setup.sh",
            )
        if spec is not None:
            config, created = product_config.initialize_config(
                spec, runtime_id, path=resolved_config, force=force
            )
        else:
            config = product_config.load_config(resolved_config)
            created = False
            if config is None or config.managed is None:
                return _install_error(
                    "NO_SPEC",
                    "the product configuration does not pin a managed dependency",
                )
        key = platform_key()
        asset = build_asset(config.managed, key)
        install_dir = product_state.managed_bin_dir(source)
        outcome = ensure(
            asset,
            install_dir=install_dir,
            policy=policy,
            opener=opener,
            runner=runner,
            which=which,
        )
        record = {
            "schema": product_state.MANAGED_SCHEMA,
            "name": asset.name,
            "version": asset.version,
            "state": outcome.state,
            "path": outcome.path,
            "sha256": outcome.sha256,
            "policy": policy,
        }
        product_state.write_managed(record, source)
        return {
            "schema": INSTALL_SCHEMA,
            "status": _outcome_status(outcome.state),
            "platform": key,
            "config_path": resolved_config,
            "config_created": created,
            "runtime": {
                "name": asset.name,
                "version": asset.version,
                "policy": policy,
                **outcome.as_dict(),
            },
        }
    except (product_config.ProductConfigError, ManagedDependencyError, StateError) as exc:
        return _install_error(
            getattr(exc, "code", "MANAGED_DEPENDENCY_ERROR"),
            f"{type(exc).__name__}: {exc}",
        )


def detect_providers(*, which: Any = None) -> list[dict[str, Any]]:
    """Report which recognised agent CLIs are on PATH (never reading secrets)."""

    lookup = which if which is not None else _which_default()
    detected: list[dict[str, Any]] = []
    for cli in KNOWN_PROVIDER_CLIS:
        path = lookup(cli)
        detected.append({"cli": cli, "present": path is not None, "path": path})
    return detected


def _provider_section(
    config: product_config.ProductConfig | None,
    *,
    which: Any,
    runner: Any,
) -> dict[str, Any]:
    detected = detect_providers(which=which)
    if config is None or config.provider is None or not config.provider.cli:
        return {
            "status": "NOT_CONFIGURED",
            "reason": "no provider is configured; select a provider CLI and model",
            "cli": None,
            "model": None,
            "detected": detected,
        }
    provider = config.provider
    path = which(provider.cli)
    if path is None:
        return {
            "status": "MISSING",
            "reason": f"configured provider CLI {provider.cli!r} is not on PATH",
            "cli": provider.cli,
            "model": provider.model,
            "detected": detected,
        }
    version = None
    result = runner((provider.cli, "--version"))
    if result.returncode == 0 and result.stdout:
        version = result.stdout.strip().splitlines()[0] if result.stdout.strip() else None
    return {
        "status": "FOUND",
        "reason": None,
        "cli": provider.cli,
        "path": path,
        "provider_id": provider.provider_id,
        "model": provider.model,
        "version": version,
        "detected": detected,
    }


def _normalized_token(value: str) -> str:
    """Lowercase and keep only alphanumerics, so ``OpenCode-Go`` == ``opencodego``."""

    return "".join(ch for ch in value.lower() if ch.isalnum())


def _authenticated_ids(stdout: str) -> set[str]:
    """Every identifier the probe's own output can honestly name.

    A provider's entry in an authenticated list can print a display name with
    spaces (``OpenCode Go``) while the operator configures the CLI's own id
    (``opencode-go``). Each printed token is a candidate, and so are the joins
    of up to :data:`_MAX_ID_TOKENS` consecutive tokens, because that is how a
    multi-word display name reads as one identifier. Nothing outside the
    printed output is ever a candidate, so a configured id that the CLI did not
    print cannot match.
    """

    tokens = [_normalized_token(token) for token in stdout.split()]
    tokens = [token for token in tokens if token]
    candidates = set(tokens)
    for size in range(2, _MAX_ID_TOKENS + 1):
        for start in range(0, max(len(tokens) - size + 1, 0)):
            candidates.add("".join(tokens[start:start + size]))
    return candidates


def _credentials_section(
    config: product_config.ProductConfig | None,
    *,
    which: Any,
    runner: Any,
) -> dict[str, Any]:
    if config is None or config.provider is None or not config.provider.cli:
        return {"status": "UNKNOWN", "reason": "no provider is configured"}
    provider = config.provider
    probe = CREDENTIAL_PROBES.get(provider.cli)
    if probe is None:
        return {
            "status": "UNKNOWN",
            "reason": "no non-secret credential probe is defined for this provider",
        }
    if which(provider.cli) is None:
        return {"status": "MISSING", "reason": "provider CLI is not on PATH"}
    if not provider.provider_id:
        return {
            "status": "UNKNOWN",
            "reason": "no provider id is configured to verify authentication",
        }
    result = runner((provider.cli, *probe))
    if result.returncode != 0:
        return {
            "status": "UNVERIFIED",
            "reason": "the credential probe did not exit successfully",
        }
    configured = _normalized_token(provider.provider_id)
    if configured and configured in _authenticated_ids(result.stdout):
        return {
            "status": "VERIFIED",
            "reason": "the configured provider id is present in the authenticated list",
        }
    return {
        "status": "UNVERIFIED",
        "reason": "the configured provider id is not present in the authenticated list",
    }


def _runtime_section(
    config: product_config.ProductConfig | None,
    *,
    env: Mapping[str, str],
    runner: Any,
    which: Any,
) -> dict[str, Any]:
    if config is None:
        return {
            "status": "UNCONFIGURED",
            "reason": "no product configuration; run setup.sh",
        }
    if config.managed is None:
        return {
            "status": "UNCONFIGURED",
            "reason": "the product configuration does not pin a managed dependency",
        }
    try:
        key = platform_key()
        asset = build_asset(config.managed, key)
    except UnsupportedPlatform as exc:
        return {"status": "UNSUPPORTED", "reason": str(exc)}
    except ManagedDependencyError as exc:
        return {"status": "INVALID", "reason": f"{type(exc).__name__}: {exc}"}
    outcome = ensure(
        asset,
        install_dir=product_state.managed_bin_dir(env),
        policy="verify",
        runner=runner,
        which=which,
    )
    return {
        "status": outcome.state,
        "reason": outcome.detail,
        "name": asset.name,
        "version": asset.version,
        "path": outcome.path,
    }


def _registry_section(env: Mapping[str, str]) -> dict[str, Any]:
    path = registry.default_db_path()
    present = os.path.isfile(path)
    return {
        "status": "PRESENT" if present else "ABSENT",
        "path": path,
        "reason": None if present else "no Work Registry ledger has been initialized yet",
    }


def doctor(
    *,
    path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    which: Any = None,
    runner: Any = None,
) -> tuple[dict[str, Any], int]:
    """Return the readiness payload and its exit code."""

    source = os.environ if env is None else env
    which = which if which is not None else _which_default()
    runner = runner if runner is not None else _runner_default()
    resolved_config = (
        os.fspath(path) if path is not None else product_config.config_path(source)
    )

    try:
        config = product_config.load_config(resolved_config)
    except (product_config.ProductConfigError, StateError) as exc:
        payload = {
            "schema": DOCTOR_SCHEMA,
            "status": "NOT_READY",
            "sections": {
                "configuration": {
                    "status": "INVALID",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            },
            "gates": {},
            "reasons": ["the product configuration is invalid or unreadable"],
        }
        return payload, EXIT_UNAVAILABLE

    runtime = _runtime_section(config, env=source, runner=runner, which=which)
    provider = _provider_section(config, which=which, runner=runner)
    credentials = _credentials_section(config, which=which, runner=runner)

    gates = {
        "managed_runtime": runtime["status"] in _READY_RUNTIME_STATES,
        "provider_found": provider["status"] == "FOUND",
        "model_configured": bool(
            config is not None
            and config.provider is not None
            and config.provider.model
        ),
        "credentials_verified": credentials["status"] == "VERIFIED",
    }
    reasons: list[str] = []
    if not gates["managed_runtime"]:
        reasons.append(f"managed runtime is {runtime['status']}")
    if not gates["provider_found"]:
        reasons.append(f"provider is {provider['status']}")
    if not gates["model_configured"]:
        reasons.append("no explicit model is configured")
    if not gates["credentials_verified"]:
        reasons.append(f"credentials are {credentials['status']}")

    ready = all(gates.values())
    payload = {
        "schema": DOCTOR_SCHEMA,
        "status": "READY" if ready else "NOT_READY",
        "runtime_id": config.runtime_id if config else None,
        "sections": {
            "runtime": runtime,
            "runtime_server": {
                "status": "UNAVAILABLE",
                "reason": "live server observation is owned by the runtime plugin "
                "and is not part of the source bootstrap",
            },
            "provider": provider,
            "credentials": credentials,
            "daemon": {
                "status": "UNAVAILABLE",
                "reason": "the service lifecycle is owned by the orchestration "
                "lane and is not part of the source bootstrap",
            },
            "registry": _registry_section(source),
        },
        "gates": gates,
        "reasons": reasons,
    }
    return payload, (EXIT_OK if ready else EXIT_UNAVAILABLE)


def _emit(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cao.bootstrap",
        description="Source bootstrap for CAO (managed runtime dependency).",
    )
    subcommands = parser.add_subparsers(dest="command", metavar="COMMAND")

    install_parser = subcommands.add_parser(
        "install", help="install or adopt the pinned managed runtime dependency"
    )
    install_parser.add_argument(
        "--policy",
        choices=sorted(POLICIES),
        default="auto",
        help="auto (adopt or install), adopt, managed (always install), verify",
    )
    install_parser.add_argument(
        "--config", default=None, metavar="PATH", help="product configuration path"
    )
    install_parser.add_argument(
        "--force", action="store_true", help="rewrite the managed fields"
    )
    install_parser.set_defaults(handler=_cmd_install)
    return parser


def _cmd_install(args: argparse.Namespace) -> int:
    payload = install(policy=args.policy, path=args.config, force=args.force)
    _emit(payload)
    return EXIT_OK if payload.get("status") == "OK" else EXIT_UNAVAILABLE


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_OK
    return handler(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
