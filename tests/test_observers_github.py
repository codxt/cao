"""GitHub observer tests: every failure mode is distinct and never a positive.

The transport is a fake recorded in memory. No test makes a real network call
and no test relies on wall-clock timing.
"""

from __future__ import annotations

import json
import unittest

from cao.observers.github import (
    GitHubError,
    GitHubObservation,
    GitHubObserver,
    GitHubOutcome,
    GitHubResponse,
    GitHubTimeoutError,
    GitHubTransportError,
)

TOKEN = "secret-token-value-xyz"


def response(status, body=None, headers=(), truncated=False):
    return GitHubResponse(
        status=status, headers=tuple(headers), body=body, truncated=truncated
    )


def repo_payload():
    return {
        "id": 1,
        "full_name": "octo/repo",
        "default_branch": "main",
        "private": False,
    }


def pull(number):
    return {
        "number": number,
        "title": f"PR {number}",
        "state": "open",
        "draft": False,
        "user": {"login": "octo"},
        "head": {"sha": "a" * 40, "ref": "feature"},
        "base": {"ref": "main"},
    }


class FakeTransport:
    """A recorded, in-memory transport. Never touches the network."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("no response configured")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class CredentialTests(unittest.TestCase):
    def test_missing_credential_is_unavailable_and_transport_not_called(self) -> None:
        for token in (None, "", "   ", 123):
            with self.subTest(token=token):
                transport = FakeTransport(response(200, json.dumps(repo_payload())))
                observer = GitHubObserver(token=token, transport=transport)
                self.assertFalse(observer.has_credential)
                observation = observer.observe_repository("octo/repo")
                self.assertIs(observation.outcome, GitHubOutcome.UNAVAILABLE)
                self.assertIsNone(observation.value)
                self.assertFalse(observation.ok)
                # No anonymous-success path: the transport was never called.
                self.assertEqual(transport.requests, [])

    def test_no_transport_is_unavailable(self) -> None:
        observer = GitHubObserver(token=TOKEN, transport=None)
        observation = observer.observe_repository("octo/repo")
        self.assertIs(observation.outcome, GitHubOutcome.UNAVAILABLE)
        self.assertIsNone(observation.value)

    def test_credential_is_used_but_never_recorded(self) -> None:
        transport = FakeTransport(response(200, json.dumps(repo_payload())))
        observer = GitHubObserver(token=TOKEN, transport=transport)
        observation = observer.observe_repository("octo/repo")
        self.assertTrue(observation.ok)
        self.assertEqual(observation.value["full_name"], "octo/repo")
        # The outbound request carries the credential...
        header_names = dict(transport.requests[0].headers)
        self.assertEqual(header_names["Authorization"], f"Bearer {TOKEN}")
        # ...but no observation, evidence or reason contains it.
        serialized = json.dumps(observation.as_dict())
        self.assertNotIn(TOKEN, serialized)
        self.assertNotIn(TOKEN, observation.evidence.reason or "")


class StatusOutcomeTests(unittest.TestCase):
    def _observer(self, transport):
        return GitHubObserver(token=TOKEN, transport=transport)

    def test_404_is_not_found_and_not_an_empty_list(self) -> None:
        observer = self._observer(FakeTransport(response(404, b"{}")))
        observation = observer.observe_pull_requests("octo/repo")
        self.assertIs(observation.outcome, GitHubOutcome.NOT_FOUND)
        self.assertIsNone(observation.value)
        self.assertFalse(observation.ok)

    def test_rate_limit_is_not_an_empty_result(self) -> None:
        for status, headers in ((429, ()), (403, (("x-ratelimit-remaining", "0"),))):
            with self.subTest(status=status):
                observer = self._observer(FakeTransport(response(status, b"{}", headers)))
                observation = observer.observe_pull_requests("octo/repo")
                self.assertIs(observation.outcome, GitHubOutcome.RATE_LIMITED)
                self.assertIsNone(observation.value)
                self.assertFalse(observation.ok)

    def test_forbidden_without_rate_limit_is_forbidden(self) -> None:
        observer = self._observer(FakeTransport(response(403, b"{}")))
        observation = observer.observe_pull_requests("octo/repo")
        self.assertIs(observation.outcome, GitHubOutcome.FORBIDDEN)

    def test_unauthorized_is_refused(self) -> None:
        observer = self._observer(FakeTransport(response(401, b"{}")))
        observation = observer.observe_repository("octo/repo")
        self.assertIs(observation.outcome, GitHubOutcome.UNAUTHORIZED)
        self.assertEqual(observation.evidence.verdict.value, "REFUSED")

    def test_server_error(self) -> None:
        observer = self._observer(FakeTransport(response(503, b"{}")))
        observation = observer.observe_repository("octo/repo")
        self.assertIs(observation.outcome, GitHubOutcome.SERVER_ERROR)

    def test_unexpected_status(self) -> None:
        observer = self._observer(FakeTransport(response(418, b"{}")))
        observation = observer.observe_repository("octo/repo")
        self.assertIs(observation.outcome, GitHubOutcome.UNEXPECTED_STATUS)

    def test_truncated_body_is_explicit(self) -> None:
        observer = self._observer(
            FakeTransport(response(200, b'{"id": 1}', truncated=True))
        )
        observation = observer.observe_repository("octo/repo")
        self.assertIs(observation.outcome, GitHubOutcome.TRUNCATED)
        self.assertTrue(observation.truncated)
        self.assertIsNone(observation.value)


class PayloadTests(unittest.TestCase):
    def test_malformed_json_is_malformed(self) -> None:
        for body in ("not json", b"\xff\xfe", ""):
            with self.subTest(body=body):
                observer = GitHubObserver(
                    token=TOKEN, transport=FakeTransport(response(200, body))
                )
                observation = observer.observe_repository("octo/repo")
                self.assertIs(observation.outcome, GitHubOutcome.MALFORMED)
                self.assertIsNone(observation.value)

    def test_wrong_shape_is_malformed(self) -> None:
        # Pull requests must be a list; a valid JSON object is the wrong shape.
        observer = GitHubObserver(
            token=TOKEN, transport=FakeTransport(response(200, json.dumps({"x": 1})))
        )
        observation = observer.observe_pull_requests("octo/repo")
        self.assertIs(observation.outcome, GitHubOutcome.MALFORMED)

    def test_missing_field_is_malformed(self) -> None:
        broken = pull(1)
        del broken["user"]
        observer = GitHubObserver(
            token=TOKEN, transport=FakeTransport(response(200, json.dumps([broken])))
        )
        observation = observer.observe_pull_requests("octo/repo")
        self.assertIs(observation.outcome, GitHubOutcome.MALFORMED)


class TransportFailureTests(unittest.TestCase):
    def _observing(self, raised):
        observer = GitHubObserver(token=TOKEN, transport=FakeTransport(raised))
        return observer.observe_repository("octo/repo")

    def test_timeout(self) -> None:
        self.assertIs(
            self._observing(GitHubTimeoutError("slow")).outcome,
            GitHubOutcome.TIMEOUT,
        )
        self.assertIs(self._observing(TimeoutError()).outcome, GitHubOutcome.TIMEOUT)

    def test_transport_error(self) -> None:
        self.assertIs(
            self._observing(GitHubTransportError("down")).outcome,
            GitHubOutcome.TRANSPORT_ERROR,
        )
        self.assertIs(
            self._observing(ValueError("boom")).outcome,
            GitHubOutcome.TRANSPORT_ERROR,
        )

    def test_malformed_transport_result(self) -> None:
        observer = GitHubObserver(token=TOKEN, transport=lambda request: "nope")
        observation = observer.observe_repository("octo/repo")
        self.assertIs(observation.outcome, GitHubOutcome.TRANSPORT_ERROR)
        self.assertIsNone(observation.value)


class PaginationTests(unittest.TestCase):
    def test_pagination_completes_on_a_short_page(self) -> None:
        transport = FakeTransport(
            response(200, json.dumps([pull(1), pull(2)])),
            response(200, json.dumps([pull(3)])),
        )
        observer = GitHubObserver(token=TOKEN, transport=transport)
        observation = observer.observe_pull_requests("octo/repo", per_page=2)
        self.assertIs(observation.outcome, GitHubOutcome.OK)
        self.assertEqual(len(observation.value), 3)
        self.assertFalse(observation.truncated)
        self.assertEqual(len(transport.requests), 2)

    def test_page_cap_is_a_hard_cap(self) -> None:
        transport = FakeTransport(
            response(200, json.dumps([pull(1)])),
            response(200, json.dumps([pull(2)])),
        )
        observer = GitHubObserver(token=TOKEN, transport=transport)
        observation = observer.observe_pull_requests(
            "octo/repo", per_page=1, max_pages=2
        )
        self.assertIs(observation.outcome, GitHubOutcome.CAP_REACHED)
        self.assertTrue(observation.truncated)
        self.assertFalse(observation.ok)
        self.assertEqual(len(observation.value), 2)
        # Never more than the stated cap of requests.
        self.assertEqual(len(transport.requests), 2)

    def test_checks_truncation_is_explicit(self) -> None:
        payload = {
            "total_count": 250,
            "check_runs": [
                {"name": f"check-{i}", "status": "completed", "conclusion": "success"}
                for i in range(100)
            ],
        }
        observer = GitHubObserver(
            token=TOKEN, transport=FakeTransport(response(200, json.dumps(payload)))
        )
        observation = observer.observe_checks("octo/repo", "main")
        self.assertIs(observation.outcome, GitHubOutcome.TRUNCATED)
        self.assertTrue(observation.truncated)
        self.assertEqual(observation.value["total_count"], 250)


class InputValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.observer = GitHubObserver(
            token=TOKEN, transport=FakeTransport(response(200, b"{}"))
        )

    def test_malformed_repository_raises(self) -> None:
        for repo in ("octo", "octo/", "/repo", "octo/repo/extra", "..", "a/.."):
            with self.subTest(repo=repo):
                with self.assertRaises(GitHubError):
                    self.observer.observe_repository(repo)

    def test_invalid_pull_state_raises(self) -> None:
        with self.assertRaises(GitHubError):
            self.observer.observe_pull_requests("octo/repo", state="merged")

    def test_invalid_per_page_and_cap_raise(self) -> None:
        for per_page in (0, 101, True):
            with self.subTest(per_page=per_page):
                with self.assertRaises(GitHubError):
                    self.observer.observe_pull_requests("octo/repo", per_page=per_page)
        with self.assertRaises(GitHubError):
            self.observer.observe_pull_requests("octo/repo", max_pages=0)

    def test_invalid_ref_raises(self) -> None:
        for ref in ("", " ", "bad?ref", "../escape"):
            with self.subTest(ref=ref):
                with self.assertRaises(GitHubError):
                    self.observer.observe_checks("octo/repo", ref)

    def test_bad_api_base_raises(self) -> None:
        with self.assertRaises(GitHubError):
            GitHubObserver(token=TOKEN, transport=lambda request: None, api_base="api")

    def test_observation_shape_is_typed(self) -> None:
        observation = self.observer.observe_repository("octo/repo")
        self.assertIsInstance(observation, GitHubObservation)


if __name__ == "__main__":
    unittest.main()
