"""Read-only GitHub observer with an injected transport.

The observer never performs I/O itself. It builds a :class:`GitHubRequest` and
hands it to a caller-supplied transport, so importing this module has no side
effects and no module-level or import-time network access exists anywhere.

Credentials are explicit. A token is passed to the constructor or not at all;
there is no hardcoded value, no implicit environment read and no default. A
missing or unusable credential yields an explicit ``UNAVAILABLE`` observation.
There is no anonymous-success path.

Every failure mode is a distinct, qualified outcome and never a silent empty
or a positive result: rate limited, not found, unauthorized, forbidden,
malformed payload, transport error, timeout, truncated body and an exceeded
page cap are all separate. In particular a 404 is not an empty list, and a
rate limit is not an empty result.

The surface is deliberately small and read-only: a repository and its pull
requests and check runs. There are no writes, mutations, auth flows, webhooks
or token exchange. Standard library only; a real transport (for example one
built on ``urllib``) is constructed by the caller.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeVar

from ..evidence import (
    Clock,
    Evidence,
    EvidenceSource,
    Verdict,
    build_evidence,
    fingerprint,
)

__all__ = [
    "DEFAULT_API_BASE",
    "DEFAULT_TIMEOUT",
    "DEFAULT_PER_PAGE",
    "MAX_PER_PAGE",
    "DEFAULT_MAX_PAGES",
    "GitHubError",
    "GitHubTransportError",
    "GitHubTimeoutError",
    "GitHubOutcome",
    "GitHubRequest",
    "GitHubResponse",
    "GitHubObservation",
    "GitHubObserver",
]

DEFAULT_API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT = 10.0
DEFAULT_PER_PAGE = 100
MAX_PER_PAGE = 100
DEFAULT_MAX_PAGES = 10

_REPO_SEGMENT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98})")
_REF = re.compile(r"[A-Za-z0-9._/-]{1,255}")
_PULL_STATES = ("open", "closed", "all")


class GitHubError(Exception):
    """Raised for a caller mistake that must fail closed."""

    code = "GITHUB_ERROR"


class GitHubTransportError(GitHubError):
    """The injected transport could not complete a request."""

    code = "GITHUB_TRANSPORT"


class GitHubTimeoutError(GitHubTransportError):
    """The injected transport timed out."""

    code = "GITHUB_TIMEOUT"


class GitHubOutcome(str, Enum):
    """The distinct, explicit result of a GitHub observation."""

    OK = "OK"
    UNAVAILABLE = "UNAVAILABLE"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    RATE_LIMITED = "RATE_LIMITED"
    NOT_FOUND = "NOT_FOUND"
    MALFORMED = "MALFORMED"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    TIMEOUT = "TIMEOUT"
    TRUNCATED = "TRUNCATED"
    CAP_REACHED = "CAP_REACHED"
    SERVER_ERROR = "SERVER_ERROR"
    UNEXPECTED_STATUS = "UNEXPECTED_STATUS"


@dataclass(frozen=True)
class GitHubRequest:
    """One read-only request handed to the injected transport."""

    method: str
    url: str
    headers: tuple[tuple[str, str], ...]
    timeout: float


@dataclass(frozen=True)
class GitHubResponse:
    """One response returned by the injected transport."""

    status: int
    headers: tuple[tuple[str, str], ...] = ()
    body: str | bytes | None = None
    truncated: bool = False


T = TypeVar("T")

Transport = Callable[[GitHubRequest], GitHubResponse]


@dataclass(frozen=True)
class GitHubObservation(Generic[T]):
    """A qualified GitHub observation.

    ``ok`` is true only for ``OK``: every failure mode carries a distinct
    ``outcome`` and an explicit evidence verdict, and never a value.
    """

    operation: str
    outcome: GitHubOutcome
    evidence: Evidence
    value: T | None = None
    truncated: bool = False
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is GitHubOutcome.OK and self.value is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "outcome": self.outcome.value,
            "ok": self.ok,
            "truncated": self.truncated,
            "reason": self.reason,
            "evidence": self.evidence.as_dict(),
            "value": self.value,
        }


@dataclass(frozen=True)
class _Fetch:
    outcome: GitHubOutcome
    reason: str | None
    body: str | None
    headers: dict[str, str]


def _valid_repo(repo: Any) -> bool:
    if not isinstance(repo, str):
        return False
    parts = repo.split("/")
    if len(parts) != 2:
        return False
    return all(_REPO_SEGMENT.fullmatch(part or "") for part in parts)


def _valid_ref(ref: Any) -> bool:
    if not isinstance(ref, str) or not ref:
        return False
    if ".." in ref or ref.startswith("/") or ref.endswith("/"):
        return False
    return _REF.fullmatch(ref) is not None


def _normalize_headers(raw: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(raw, (tuple, list)):
        return out
    for pair in raw:
        if (
            isinstance(pair, (tuple, list))
            and len(pair) == 2
            and isinstance(pair[0], str)
            and isinstance(pair[1], str)
        ):
            out[pair[0].lower()] = pair[1]
    return out


class GitHubObserver:
    """Read-only observation through an injected transport.

    ``token`` is the only credential source. ``transport`` is the only I/O
    path. With either missing, every observation is an explicit
    ``UNAVAILABLE`` and no transport is called.
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        transport: Transport | None = None,
        api_base: str = DEFAULT_API_BASE,
        timeout: float = DEFAULT_TIMEOUT,
        clock: Clock | None = None,
    ) -> None:
        self._token = token if isinstance(token, str) and token.strip() else None
        self._transport = transport if callable(transport) else None
        if not isinstance(api_base, str) or not api_base.startswith(
            ("https://", "http://")
        ):
            raise GitHubError("api_base must be an absolute http(s) URL")
        self._api_base = api_base.rstrip("/")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
                or timeout <= 0:
            raise GitHubError("timeout must be a positive number")
        self._timeout = float(timeout)
        self._clock = clock

    @property
    def has_credential(self) -> bool:
        return self._token is not None

    # -- public read-only surface -----------------------------------------

    def observe_repository(self, repo: str) -> GitHubObservation[dict[str, Any]]:
        """Observe one repository's public metadata."""
        if not _valid_repo(repo):
            raise GitHubError(f"malformed repository name: {repo!r}")
        fetch = self._perform(f"/repos/{repo}")
        if fetch.outcome is not GitHubOutcome.OK:
            return self._failed("repository", fetch)
        try:
            payload = json.loads(fetch.body or "")
            value = _parse_repository(payload)
        except (ValueError, TypeError):
            return self._failed(
                "repository",
                _Fetch(GitHubOutcome.MALFORMED, "repository payload is malformed",
                       None, fetch.headers),
            )
        return self._ok("repository", value, fetch)

    def observe_pull_requests(
        self,
        repo: str,
        *,
        state: str = "open",
        per_page: int = DEFAULT_PER_PAGE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> GitHubObservation[tuple[dict[str, Any], ...]]:
        """Observe a repository's pull requests, within a hard page cap.

        The page count is capped at ``max_pages``. When every page is full at
        the cap the observation is ``CAP_REACHED``/truncated and never claims
        to be the complete list.
        """
        if not _valid_repo(repo):
            raise GitHubError(f"malformed repository name: {repo!r}")
        if state not in _PULL_STATES:
            raise GitHubError(f"invalid pull request state: {state!r}")
        if isinstance(per_page, bool) or not isinstance(per_page, int) \
                or not 1 <= per_page <= MAX_PER_PAGE:
            raise GitHubError(f"per_page must be within 1..{MAX_PER_PAGE}")
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) \
                or max_pages < 1:
            raise GitHubError("max_pages must be at least 1")

        collected: list[dict[str, Any]] = []
        page = 1
        while page <= max_pages:
            path = (
                f"/repos/{repo}/pulls?state={state}"
                f"&per_page={per_page}&page={page}"
            )
            fetch = self._perform(path)
            if fetch.outcome is not GitHubOutcome.OK:
                return self._failed("pull_requests", fetch)
            try:
                payload = json.loads(fetch.body or "")
                entries = _parse_pull_requests(payload)
            except (ValueError, TypeError):
                return self._failed(
                    "pull_requests",
                    _Fetch(GitHubOutcome.MALFORMED,
                           "pull request payload is malformed", None,
                           fetch.headers),
                )
            collected.extend(entries)
            if len(entries) < per_page:
                return self._ok("pull_requests", tuple(collected), fetch)
            if page >= max_pages:
                return GitHubObservation(
                    operation="pull_requests",
                    outcome=GitHubOutcome.CAP_REACHED,
                    evidence=build_evidence(
                        EvidenceSource.INTERNAL,
                        Verdict.UNKNOWN,
                        reason="pull request page cap reached",
                        fingerprint_value=fingerprint(len(collected)),
                        clock=self._clock,
                    ),
                    value=tuple(collected),
                    truncated=True,
                    reason="pull request page cap reached",
                )
            page += 1
        # Unreachable: the loop returns on the cap; retained as an explicit
        # fail-closed terminator that never claims a complete list.
        return GitHubObservation(  # pragma: no cover
            operation="pull_requests",
            outcome=GitHubOutcome.CAP_REACHED,
            evidence=build_evidence(
                EvidenceSource.INTERNAL,
                Verdict.UNKNOWN,
                reason="pull request pagination ended without a decision",
                clock=self._clock,
            ),
            value=None,
        )

    def observe_checks(self, repo: str, ref: str) -> GitHubObservation[dict[str, Any]]:
        """Observe the check runs for one commit ref, within one page."""
        if not _valid_repo(repo):
            raise GitHubError(f"malformed repository name: {repo!r}")
        if not _valid_ref(ref):
            raise GitHubError(f"malformed ref: {ref!r}")
        path = f"/repos/{repo}/commits/{ref}/check-runs?per_page={MAX_PER_PAGE}"
        fetch = self._perform(path)
        if fetch.outcome is not GitHubOutcome.OK:
            return self._failed("checks", fetch)
        try:
            payload = json.loads(fetch.body or "")
            value, truncated = _parse_checks(payload)
        except (ValueError, TypeError):
            return self._failed(
                "checks",
                _Fetch(GitHubOutcome.MALFORMED, "checks payload is malformed",
                       None, fetch.headers),
            )
        if truncated:
            return GitHubObservation(
                operation="checks",
                outcome=GitHubOutcome.TRUNCATED,
                evidence=build_evidence(
                    EvidenceSource.INTERNAL,
                    Verdict.UNKNOWN,
                    reason="more check runs exist than one page holds",
                    fingerprint_value=fingerprint(value.get("total_count")),
                    clock=self._clock,
                ),
                value=value,
                truncated=True,
                reason="more check runs exist than one page holds",
            )
        return self._ok("checks", value, fetch)

    # -- request pipeline --------------------------------------------------

    def _perform(self, path: str) -> _Fetch:
        if self._token is None:
            return _Fetch(
                GitHubOutcome.UNAVAILABLE,
                "no credential configured",
                None,
                {},
            )
        if self._transport is None:
            return _Fetch(
                GitHubOutcome.UNAVAILABLE,
                "no transport configured",
                None,
                {},
            )
        request = GitHubRequest(
            method="GET",
            url=f"{self._api_base}{path}",
            headers=self._headers(),
            timeout=self._timeout,
        )
        try:
            response = self._transport(request)
        except GitHubTimeoutError:
            return _Fetch(GitHubOutcome.TIMEOUT, "transport timed out", None, {})
        except TimeoutError:
            return _Fetch(GitHubOutcome.TIMEOUT, "transport timed out", None, {})
        except GitHubTransportError:
            return _Fetch(
                GitHubOutcome.TRANSPORT_ERROR, "transport failed", None, {}
            )
        except Exception as exc:  # noqa: BLE001 - a transport failure is not success
            return _Fetch(
                GitHubOutcome.TRANSPORT_ERROR,
                f"transport raised {type(exc).__name__}",
                None,
                {},
            )
        if type(response) is not GitHubResponse:
            return _Fetch(
                GitHubOutcome.TRANSPORT_ERROR,
                "transport returned a malformed response",
                None,
                {},
            )
        if isinstance(response.status, bool) or not isinstance(response.status, int):
            return _Fetch(
                GitHubOutcome.TRANSPORT_ERROR,
                "transport returned a malformed status",
                None,
                {},
            )
        headers = _normalize_headers(response.headers)
        if response.truncated:
            return _Fetch(
                GitHubOutcome.TRUNCATED, "response body is truncated", None, headers
            )
        body = response.body
        if isinstance(body, (bytes, bytearray)):
            try:
                body = bytes(body).decode("utf-8")
            except UnicodeDecodeError:
                return _Fetch(
                    GitHubOutcome.MALFORMED, "response body is not UTF-8", None,
                    headers,
                )
        if body is not None and not isinstance(body, str):
            return _Fetch(
                GitHubOutcome.MALFORMED, "response body is malformed", None, headers
            )

        status = response.status
        if status == 200:
            return _Fetch(GitHubOutcome.OK, None, body, headers)
        if status == 401:
            return _Fetch(GitHubOutcome.UNAUTHORIZED, "not authorized", None, headers)
        if status == 403:
            if headers.get("x-ratelimit-remaining") == "0":
                return _Fetch(
                    GitHubOutcome.RATE_LIMITED, "rate limit exhausted", None, headers
                )
            return _Fetch(GitHubOutcome.FORBIDDEN, "forbidden", None, headers)
        if status == 404:
            return _Fetch(GitHubOutcome.NOT_FOUND, "not found", None, headers)
        if status == 429:
            return _Fetch(
                GitHubOutcome.RATE_LIMITED, "rate limit exceeded", None, headers
            )
        if 500 <= status < 600:
            return _Fetch(
                GitHubOutcome.SERVER_ERROR, "server error", None, headers
            )
        return _Fetch(
            GitHubOutcome.UNEXPECTED_STATUS,
            f"unexpected status {status}",
            None,
            headers,
        )

    def _headers(self) -> tuple[tuple[str, str], ...]:
        headers = [
            ("Accept", "application/vnd.github+json"),
            ("X-GitHub-Api-Version", "2022-11-28"),
            ("User-Agent", "cao-observer"),
        ]
        # The credential is placed only on the outbound request and is never
        # recorded in an observation, evidence or reason.
        if self._token is not None:
            headers.append(("Authorization", f"Bearer {self._token}"))
        return tuple(headers)

    # -- result construction ----------------------------------------------

    def _ok(self, operation: str, value: Any, fetch: _Fetch) -> GitHubObservation:
        return GitHubObservation(
            operation=operation,
            outcome=GitHubOutcome.OK,
            evidence=build_evidence(
                EvidenceSource.INTERNAL,
                Verdict.SUPPORTED,
                reason="observed",
                fingerprint_value=fingerprint(value),
                clock=self._clock,
            ),
            value=value,
        )

    def _failed(self, operation: str, fetch: _Fetch) -> GitHubObservation:
        return GitHubObservation(
            operation=operation,
            outcome=fetch.outcome,
            evidence=build_evidence(
                EvidenceSource.INTERNAL,
                _verdict_for(fetch.outcome),
                reason=fetch.reason,
                clock=self._clock,
            ),
            value=None,
            truncated=fetch.outcome is GitHubOutcome.TRUNCATED,
            reason=fetch.reason,
        )


def _verdict_for(outcome: GitHubOutcome) -> Verdict:
    if outcome is GitHubOutcome.OK:
        return Verdict.SUPPORTED
    if outcome in (GitHubOutcome.UNAUTHORIZED, GitHubOutcome.FORBIDDEN):
        return Verdict.REFUSED
    if outcome is GitHubOutcome.MALFORMED:
        return Verdict.INVALID
    if outcome in (GitHubOutcome.TRUNCATED, GitHubOutcome.CAP_REACHED,
                   GitHubOutcome.UNEXPECTED_STATUS):
        return Verdict.UNKNOWN
    # UNAVAILABLE, NOT_FOUND, RATE_LIMITED, TRANSPORT_ERROR, TIMEOUT,
    # SERVER_ERROR: the fact could not be usefully established and is never a
    # positive or empty result.
    return Verdict.UNAVAILABLE


def _require(condition: bool, what: str) -> None:
    if not condition:
        raise ValueError(what)


def _mapping(value: Any, what: str) -> dict[str, Any]:
    _require(isinstance(value, dict), f"{what} must be an object")
    return value


def _str_field(obj: dict[str, Any], key: str, what: str) -> str:
    value = obj.get(key)
    _require(isinstance(value, str) and bool(value), f"{what}.{key} must be a string")
    return value


def _int_field(obj: dict[str, Any], key: str, what: str) -> int:
    value = obj.get(key)
    _require(
        isinstance(value, int) and not isinstance(value, bool),
        f"{what}.{key} must be an integer",
    )
    return value


def _bool_field(obj: dict[str, Any], key: str, what: str) -> bool:
    value = obj.get(key)
    _require(isinstance(value, bool), f"{what}.{key} must be a boolean")
    return value


def _parse_repository(payload: Any) -> dict[str, Any]:
    obj = _mapping(payload, "repository")
    return {
        "id": _int_field(obj, "id", "repository"),
        "full_name": _str_field(obj, "full_name", "repository"),
        "default_branch": _str_field(obj, "default_branch", "repository"),
        "private": _bool_field(obj, "private", "repository"),
    }


def _parse_pull_request(entry: Any) -> dict[str, Any]:
    obj = _mapping(entry, "pull request")
    user = _mapping(obj.get("user"), "pull request.user")
    head = _mapping(obj.get("head"), "pull request.head")
    base = _mapping(obj.get("base"), "pull request.base")
    return {
        "number": _int_field(obj, "number", "pull request"),
        "title": _str_field(obj, "title", "pull request"),
        "state": _str_field(obj, "state", "pull request"),
        "draft": _bool_field(obj, "draft", "pull request"),
        "author": _str_field(user, "login", "pull request.user"),
        "head_sha": _str_field(head, "sha", "pull request.head"),
        "base_ref": _str_field(base, "ref", "pull request.base"),
    }


def _parse_pull_requests(payload: Any) -> list[dict[str, Any]]:
    _require(isinstance(payload, list), "pull request payload must be a list")
    return [_parse_pull_request(entry) for entry in payload]


def _parse_checks(payload: Any) -> tuple[dict[str, Any], bool]:
    obj = _mapping(payload, "checks")
    total = _int_field(obj, "total_count", "checks")
    runs = obj.get("check_runs")
    _require(isinstance(runs, list), "checks.check_runs must be a list")
    parsed: list[dict[str, Any]] = []
    for run in runs:
        item = _mapping(run, "check run")
        conclusion = item.get("conclusion")
        _require(
            conclusion is None or isinstance(conclusion, str),
            "check run.conclusion must be a string or null",
        )
        parsed.append(
            {
                "name": _str_field(item, "name", "check run"),
                "status": _str_field(item, "status", "check run"),
                "conclusion": conclusion,
            }
        )
    return {"total_count": total, "check_runs": parsed}, total > len(parsed)
