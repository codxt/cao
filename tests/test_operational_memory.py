"""Synthetic tests for the isolated operational-memory projection (Batch A).

Every fixture is invented: invented repositories, ids, digests and timestamps.
No real submission, run id, credential, transcript or operator value appears.
The tests pin the invariants the brief requires: idempotent ingest, dedup and
conflict, replay across restart, scope isolation and scope-enforced read,
verdict preservation, the retention boundary with the named ``observed_at``
fallback, schema/field drift, stale provenance, corruption, symlink and
traversal inertness, refusal of direct narrative and of every lesson,
read-only retrieval and the absence of any egress, shell or runtime-plugin
import.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cao import evidence, intake
from cao import operational_memory as om

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
HEAD_A = "a" * 40
HEAD_B = "b" * 40
EPOCH = "2026-01-01T00:00:00Z"
#: A fixed "now" so a real wall clock can never make a synthetic episode expire.
NOW = "2026-01-05T00:00:00Z"
RETENTION = 86400 * 30


def fixed_clock() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def narrative(**over: object) -> dict:
    base = {
        "objective": None,
        "case": None,
        "actor": None,
        "team": None,
        "hypothesis": None,
        "action": None,
        "outcome": None,
        "correction": None,
    }
    base.update(over)
    return base


def doc(**over: object) -> dict:
    base = {
        "schema": om.EVIDENCE_SCHEMA,
        "source": {
            "schema": "cao.intake.submission.v1",
            "id": "submission-one",
            "ref": "urn:cao:intake-submission:submission-one",
            "digest": DIGEST_A,
        },
        "scope": {
            "owner": "example-owner",
            "repository": "example-owner/platform",
            "data_class": "synthetic",
        },
        "event": {
            "event_type": "routing",
            "verdict": "UNKNOWN",
            "event_time": EPOCH,
            "observed_at": "2026-01-01T00:05:00Z",
        },
        "repo": {"head": HEAD_A, "base": None, "path_scope": []},
        "context": {"refs": [], "cutoff": None},
        "narrative": narrative(),
        "lesson": None,
    }
    base.update(over)
    return base


def _path(record: dict, dotted: str) -> object:
    node: object = record
    for part in dotted.split("."):
        assert isinstance(node, dict)
        node = node[part]
    return node


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        root = Path(self._temp.name)
        self.env = {
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_CONFIG_HOME": str(root / "config"),
        }

    @property
    def store(self) -> Path:
        return Path(om.episodes_dir(self.env))

    def names(self) -> list[str]:
        return sorted(p.name for p in self.store.iterdir()) if self.store.exists() else []

    def scope(self) -> dict:
        return {
            "owner": "example-owner",
            "repository": "example-owner/platform",
            "data_class": "synthetic",
        }

    def put(self, document: dict, **kwargs: object) -> dict:
        return om.ingest(document, env=self.env, clock=fixed_clock, **kwargs)

    def read(self, episode_id: str, **kwargs: object) -> dict:
        kwargs.setdefault("scope", self.scope())
        kwargs.setdefault("retention_seconds", RETENTION)
        kwargs.setdefault("now", NOW)
        return om.read_episode(episode_id, env=self.env, **kwargs)


class SchemaTests(unittest.TestCase):
    def test_supported_schema_is_explicit(self) -> None:
        self.assertEqual(om.SUPPORTED_SCHEMAS, (om.EPISODE_SCHEMA,))
        self.assertEqual(om.EPISODE_SCHEMA, "cao.operational.episode.v1")

    def test_verdicts_come_from_the_evidence_contract(self) -> None:
        self.assertEqual(om.VERDICTS, frozenset(v.value for v in evidence.Verdict))

    def test_field_source_map_covers_the_schema(self) -> None:
        self.assertEqual(set(om.FIELD_SOURCE_MAP), set(om.EPISODE_FIELD_PATHS))
        record = om.project_episode(doc(), clock=fixed_clock)
        for path in om.EPISODE_FIELD_PATHS:
            _path(record, path)  # every mapped field really exists
        # The map must honestly record fields with no attributable origin.
        unsupported = {
            key
            for key, value in om.FIELD_SOURCE_MAP.items()
            if om.UNSUPPORTED in value
        }
        self.assertTrue(unsupported)


class ProjectionTests(unittest.TestCase):
    def test_projection_is_deterministic_and_authority_free(self) -> None:
        first = om.project_episode(doc(), clock=fixed_clock)
        second = om.project_episode(doc(), clock=fixed_clock)
        self.assertEqual(first, second)
        self.assertRegex(first["episode_id"], r"^[0-9a-f]{64}$")
        self.assertRegex(first["content_fingerprint"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(first["schema"], om.EPISODE_SCHEMA)
        self.assertIs(first["projection"], True)
        self.assertEqual(first["authority"], "none")
        # Equal identity, different content: the content fingerprint differs.
        changed = doc()
        changed["event"] = dict(changed["event"], verdict="SUPPORTED")
        other = om.project_episode(changed, clock=fixed_clock)
        self.assertEqual(other["episode_id"], first["episode_id"])
        self.assertNotEqual(other["content_fingerprint"], first["content_fingerprint"])

    def test_identity_binds_the_immutable_source(self) -> None:
        base = om.episode_id_for(doc())
        changed_source = dict(doc())
        changed_source["source"] = dict(changed_source["source"], digest=DIGEST_B)
        self.assertNotEqual(om.episode_id_for(changed_source), base)
        changed_scope = dict(doc())
        changed_scope["scope"] = {
            "owner": "other-owner",
            "repository": "other-owner/platform",
            "data_class": "synthetic",
        }
        self.assertNotEqual(om.episode_id_for(changed_scope), base)
        # Non-identity content never changes the projection identity.
        changed_content = doc()
        changed_content["event"] = dict(changed_content["event"], verdict="SUPPORTED")
        self.assertEqual(om.episode_id_for(changed_content), base)


class StorageTests(_Fixture):
    def test_ingest_is_idempotent_with_one_durable_record(self) -> None:
        first = self.put(doc())
        self.assertEqual(first["status"], "ADMITTED")
        episode_id = first["episode"]["episode_id"]
        before = self.read_file(episode_id)
        again = self.put(doc())
        self.assertEqual(again["status"], "DUPLICATE")
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["episode"]["episode_id"], episode_id)
        self.assertEqual(self.names(), [episode_id + ".json"])
        self.assertEqual(self.read_file(episode_id), before)

    def test_conflict_on_reused_identity_with_different_content(self) -> None:
        first = self.put(doc())
        episode_id = first["episode"]["episode_id"]
        before = self.read_file(episode_id)
        changed = doc()
        changed["event"] = dict(changed["event"], verdict="SUPPORTED")
        with self.assertRaises(om.EpisodeConflict):
            self.put(changed)
        self.assertEqual(self.read_file(episode_id), before)
        self.assertEqual(self.names(), [episode_id + ".json"])

    def test_replay_across_restart_creates_no_second_record(self) -> None:
        first = self.put(doc())
        episode_id = first["episode"]["episode_id"]
        module = importlib.reload(om)
        again = module.ingest(doc(), env=self.env, clock=fixed_clock)
        self.assertEqual(again["status"], "DUPLICATE")
        self.assertEqual(self.names(), [episode_id + ".json"])
        views = module.list_episodes(
            env=self.env,
            scope=self.scope(),
            retention_seconds=RETENTION, now=NOW,
        )
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0]["episode_id"], episode_id)

    def test_private_store_permissions(self) -> None:
        self.put(doc())
        self.assertEqual(stat.S_IMODE(os.stat(self.store).st_mode), 0o700)
        path = self.store / (os.listdir(self.store)[0])
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def read_file(self, episode_id: str) -> bytes:
        return (self.store / (episode_id + ".json")).read_bytes()


class ScopeAndRetentionTests(_Fixture):
    def _two_scopes(self) -> tuple[str, str]:
        first = self.put(doc())
        other = dict(doc())
        other["source"] = dict(other["source"], id="submission-two")
        other["scope"] = {
            "owner": "example-owner",
            "repository": "example-owner/other",
            "data_class": "synthetic",
        }
        second = self.put(other)
        return first["episode"]["episode_id"], second["episode"]["episode_id"]

    def test_scope_isolation_and_no_partial_match(self) -> None:
        first_id, second_id = self._two_scopes()
        scoped = om.list_episodes(
            env=self.env, scope=self.scope(), retention_seconds=RETENTION, now=NOW
        )
        self.assertEqual([v["episode_id"] for v in scoped], [first_id])
        cross = om.list_episodes(
            env=self.env,
            scope={
                "owner": "example-owner",
                "repository": "example-owner/absent",
                "data_class": "synthetic",
            },
            retention_seconds=RETENTION, now=NOW,
        )
        self.assertEqual(cross, [])
        both = om.list_episodes(
            env=self.env,
            scope={
                "owner": "example-owner",
                "repository": "example-owner/other",
                "data_class": "synthetic",
            },
            retention_seconds=RETENTION, now=NOW,
        )
        self.assertEqual([v["episode_id"] for v in both], [second_id])

    def test_deterministic_ordering(self) -> None:
        for index, stamp in enumerate(
            ("2026-01-03T00:00:00Z", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
        ):
            document = doc()
            document["source"] = dict(document["source"], id=f"submission-{index}")
            document["event"] = dict(document["event"], event_time=stamp)
            self.put(document)
        first = om.list_episodes(
            env=self.env, scope=self.scope(), retention_seconds=RETENTION, now=NOW
        )
        second = om.list_episodes(
            env=self.env, scope=self.scope(), retention_seconds=RETENTION, now=NOW
        )
        self.assertEqual(first, second)
        self.assertEqual(
            [v["event"]["event_time"] for v in first],
            ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"],
        )

    def test_retention_boundary_is_inclusive_then_excluded(self) -> None:
        result = self.put(doc())
        episode_id = result["episode"]["episode_id"]
        retained = om.list_episodes(
            env=self.env,
            scope=self.scope(),
            retention_seconds=86400,
            now="2026-01-02T00:00:00Z",
        )
        self.assertEqual([v["episode_id"] for v in retained], [episode_id])
        expired = om.list_episodes(
            env=self.env,
            scope=self.scope(),
            retention_seconds=86400,
            now="2026-01-02T00:00:01Z",
        )
        self.assertEqual(expired, [])
        with self.assertRaises(om.EpisodeUnavailable):
            self.read(episode_id, retention_seconds=86400, now="2026-01-02T00:00:01Z")

    def test_retention_window_must_be_explicit_and_bounded(self) -> None:
        self.put(doc())
        for bad in (0, -1, True, "86400", 3.5, om.MAX_RETENTION_SECONDS + 1):
            with self.assertRaises(om.EpisodeInvalid):
                om.list_episodes(env=self.env, scope=self.scope(), retention_seconds=bad)


class VerdictTests(_Fixture):
    def test_verdict_status_is_preserved_never_rounded_up(self) -> None:
        for index, verdict in enumerate(sorted(om.VERDICTS)):
            document = doc()
            document["source"] = dict(document["source"], id=f"submission-{index}")
            document["event"] = dict(document["event"], verdict=verdict)
            result = self.put(document)
            view = self.read(result["episode"]["episode_id"])
            self.assertEqual(view["event"]["verdict"], verdict)

    def test_absent_verdict_stays_unknown(self) -> None:
        document = doc()
        del document["event"]["verdict"]
        result = self.put(document)
        self.assertEqual(result["episode"]["event"]["verdict"], "UNKNOWN")

    def test_ref_verdicts_are_preserved(self) -> None:
        document = doc(
            context={
                "refs": [
                    {"location": "urn:example:obs:1", "digest": DIGEST_A, "verdict": "UNAVAILABLE"},
                    {"location": "urn:example:obs:2", "digest": None, "verdict": "UNKNOWN"},
                ],
                "cutoff": "2025-12-31T00:00:00Z",
            }
        )
        result = self.put(document)
        view = self.read(result["episode"]["episode_id"])
        self.assertEqual(
            [ref["verdict"] for ref in view["context"]["refs"]],
            ["UNAVAILABLE", "UNKNOWN"],
        )


class ProvenanceTests(_Fixture):
    def _one(self) -> str:
        return self.put(doc())["episode"]["episode_id"]

    def test_no_resolver_is_unverified_not_positive(self) -> None:
        view = self.read(self._one())
        self.assertEqual(view["provenance_status"], "UNVERIFIED")
        self.assertFalse(view["grounded"])

    def test_matching_resolver_is_current_and_grounded(self) -> None:
        view = self.read(
            self._one(), resolver=lambda source: {"digest": DIGEST_A, "head": HEAD_A}
        )
        self.assertEqual(view["provenance_status"], "CURRENT")
        self.assertTrue(view["grounded"])

    def test_digest_mismatch_is_invalidated(self) -> None:
        view = self.read(
            self._one(), resolver=lambda source: {"digest": DIGEST_B, "head": HEAD_A}
        )
        self.assertEqual(view["provenance_status"], "INVALIDATED")
        self.assertFalse(view["grounded"])

    def test_head_mismatch_is_stale(self) -> None:
        view = self.read(
            self._one(), resolver=lambda source: {"digest": DIGEST_A, "head": HEAD_B}
        )
        self.assertEqual(view["provenance_status"], "STALE")
        self.assertFalse(view["grounded"])

    def test_unresolved_resolver_is_not_positive(self) -> None:
        view = self.read(self._one(), resolver=lambda source: None)
        self.assertEqual(view["provenance_status"], "UNRESOLVED")
        self.assertFalse(view["grounded"])
        raised = self.read(
            self._one(), resolver=lambda source: (_ for _ in ()).throw(RuntimeError())
        )
        self.assertEqual(raised["provenance_status"], "UNRESOLVED")

    def test_read_never_mutates_the_stored_record(self) -> None:
        episode_id = self._one()
        path = self.store / (episode_id + ".json")
        before_bytes = path.read_bytes()
        before_names = self.names()
        self.read(episode_id, resolver=lambda source: {"digest": DIGEST_A, "head": HEAD_B})
        om.list_episodes(
            env=self.env, scope=self.scope(), retention_seconds=RETENTION, now=NOW
        )
        self.assertEqual(path.read_bytes(), before_bytes)
        self.assertEqual(self.names(), before_names)


class RobustnessTests(_Fixture):
    def _stored(self) -> tuple[str, Path, dict]:
        result = self.put(doc())
        episode_id = result["episode"]["episode_id"]
        path = self.store / (episode_id + ".json")
        return episode_id, path, json.loads(path.read_text(encoding="utf-8"))

    def test_corrupt_record_fails_closed(self) -> None:
        episode_id, path, _ = self._stored()
        path.write_bytes(b"{not json")
        with self.assertRaises(om.EpisodeStateError):
            self.read(episode_id)

    def test_unknown_schema_is_refused(self) -> None:
        episode_id, path, stored = self._stored()
        stored["schema"] = "cao.operational.episode.v9"
        path.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(om.EpisodeStateError):
            self.read(episode_id)

    def test_unknown_field_is_refused(self) -> None:
        episode_id, path, stored = self._stored()
        stored["unexpected"] = 1
        path.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(om.EpisodeStateError):
            self.read(episode_id)

    def test_missing_field_is_refused(self) -> None:
        episode_id, path, stored = self._stored()
        del stored["narrative"]
        path.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(om.EpisodeStateError):
            self.read(episode_id)

    def test_tampered_content_is_refused(self) -> None:
        episode_id, path, stored = self._stored()
        stored["event"] = dict(stored["event"], verdict="SUPPORTED")
        path.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(om.EpisodeStateError):
            self.read(episode_id)

    def test_symlinked_record_or_traversal_is_not_followed(self) -> None:
        outside = Path(self._temp.name) / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        self.put(doc())
        link = self.store / (HEAD_B + "0" * 0 + "c" * 24 + ".json")
        os.symlink(outside, link)
        with self.assertRaises((om.EpisodeUnavailable, om.EpisodeStateError)):
            om.list_episodes(
            env=self.env, scope=self.scope(), retention_seconds=RETENTION, now=NOW
        )
        with self.assertRaises(om.EpisodeInvalid):
            self.read("../../../etc/passwd")
        with self.assertRaises(om.EpisodeInvalid):
            om.episode_path("..")

    def test_unexpected_store_file_is_refused(self) -> None:
        self.put(doc())
        (self.store / "notes.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(om.EpisodeStateError):
            om.list_episodes(
            env=self.env, scope=self.scope(), retention_seconds=RETENTION, now=NOW
        )

    def test_hostile_text_is_refused_and_inert(self) -> None:
        hostile = "'; rm -rf / #$(touch none) `id` && echo pwned"
        with self.assertRaises(om.EpisodeInvalid):
            self.put(doc(narrative=narrative(objective=hostile)))
        self.assertEqual(self.names(), [])

    def test_any_direct_narrative_is_refused(self) -> None:
        # No proved sanitized narrative adapter exists, so bounded narrative is
        # refused outright rather than trusted: a pattern match cannot prove a
        # field free of personal data.
        for value in (
            "a plain note",
            "token: abcdef0123456789",
            "x" * (om.MAX_TEXT + 1),
        ):
            with self.assertRaises(om.EpisodeInvalid):
                self.put(doc(narrative=narrative(objective=value)))
        self.assertEqual(self.names(), [])

    def test_absolute_or_unsafe_paths_are_refused(self) -> None:
        with self.assertRaises(om.EpisodeInvalid):
            self.put(doc(repo={"head": None, "base": None, "path_scope": ["/etc/passwd"]}))
        with self.assertRaises(om.EpisodeInvalid):
            self.put(
                doc(
                    scope={
                        "owner": "example-owner",
                        "repository": "/var/private/checkout",
                        "data_class": "synthetic",
                    }
                )
            )
        with self.assertRaises(om.EpisodeInvalid):
            self.put(doc(repo={"head": "not-a-head", "base": None, "path_scope": []}))

    def test_unknown_fields_and_values_are_refused(self) -> None:
        extra = doc()
        extra["unexpected"] = True
        with self.assertRaises(om.EpisodeInvalid):
            om.project_episode(extra)
        with self.assertRaises(om.EpisodeInvalid):
            self.put(doc(event={"event_type": "guessing", "observed_at": EPOCH}))
        with self.assertRaises(om.EpisodeInvalid):
            self.put(doc(scope={"data_class": "top-secret"}))


class LessonTests(_Fixture):
    def _acceptance(self, **over: object) -> dict:
        base = {
            "source": "operator",
            "ref": "urn:cao:acceptance:1",
            "digest": DIGEST_B,
            "verdict": "SUPPORTED",
            "observed_at": "2026-01-02T00:00:00Z",
        }
        base.update(over)
        return base

    def test_no_lesson_is_accepted_in_this_module(self) -> None:
        # No independently qualified acceptance adapter exists here, so every
        # non-null lesson is refused -- including one a caller attributes to an
        # operator. The field is always None.
        candidates = (
            {"statement": "a lesson"},
            {"statement": "a lesson", "acceptance": self._acceptance()},
            {"statement": "a lesson", "acceptance": self._acceptance(source="model")},
            {"statement": "a lesson", "acceptance": self._acceptance(verdict="UNKNOWN")},
        )
        for lesson in candidates:
            with self.assertRaises(om.EpisodeInvalid):
                self.put(doc(lesson=lesson))
        self.assertEqual(self.names(), [])
        result = self.put(doc())
        self.assertIsNone(result["episode"]["lesson"])
        view = self.read(result["episode"]["episode_id"])
        self.assertIsNone(view["lesson"])


class SubmissionAdapterTests(_Fixture):
    def _submit(self) -> tuple[str, str]:
        body = "please investigate the retry behavior"
        result = intake.submit(
            {
                "schema": intake.REQUEST_SCHEMA,
                "item_id": "synthetic-item",
                "title": "A bounded objective",
                "body": body,
                "evidence_refs": [
                    {"ref": "urn:example:observation:1", "fingerprint": DIGEST_A}
                ],
            },
            env=self.env,
            clock=fixed_clock,
        )
        return result["submission"]["submission_id"], body

    def test_adapter_grounds_only_substantiated_fields(self) -> None:
        submission_id, _ = self._submit()
        document = om.evidence_from_submission(
            submission_id,
            event_type="routing",
            data_class="synthetic",
            owner="example-owner",
            repository="example-owner/platform",
            env=self.env,
        )
        record = intake.read_submission(submission_id, self.env)
        self.assertEqual(document["source"]["schema"], intake.SUBMISSION_SCHEMA)
        self.assertEqual(document["source"]["id"], submission_id)
        self.assertEqual(document["source"]["digest"], record["fingerprint"])
        self.assertEqual(document["event"]["verdict"], "UNKNOWN")
        self.assertIsNone(document["event"]["event_time"])
        self.assertEqual(document["event"]["observed_at"], record["admitted_at"])
        # Raw inbound text is never copied into the projection.
        for field in document["narrative"].values():
            self.assertIsNone(field)
        self.assertEqual(
            [ref["location"] for ref in document["context"]["refs"]],
            [f"urn:cao:intake-submission:{submission_id}#evidence-0"],
        )
        self.assertEqual(document["context"]["refs"][0]["digest"], DIGEST_A)
        self.assertEqual(
            [ref["verdict"] for ref in document["context"]["refs"]], ["UNKNOWN"]
        )

    def test_adapter_copies_no_raw_inbound_text(self) -> None:
        submission_id, body = self._submit()
        document = om.evidence_from_submission(
            submission_id,
            event_type="routing",
            data_class="synthetic",
            owner="example-owner",
            repository="example-owner/platform",
            env=self.env,
        )
        serialized = json.dumps(document)
        for raw in (
            "A bounded objective",
            "synthetic-item",
            "urn:example:observation:1",
            body,
        ):
            self.assertNotIn(raw, serialized)

    def test_adapter_drops_a_non_digest_reference_fingerprint(self) -> None:
        result = intake.submit(
            {
                "schema": intake.REQUEST_SCHEMA,
                "item_id": "synthetic-item",
                "title": "",
                "body": "",
                "evidence_refs": [{"ref": "urn:example:observation:2"}],
            },
            env=self.env,
            clock=fixed_clock,
        )
        document = om.evidence_from_submission(
            result["submission"]["submission_id"],
            event_type="routing",
            data_class="synthetic",
            owner="example-owner",
            repository="example-owner/platform",
            env=self.env,
        )
        self.assertIsNone(document["context"]["refs"][0]["digest"])

    def test_adapter_never_stores_the_raw_body(self) -> None:
        submission_id, body = self._submit()
        result = om.ingest_submission(
            submission_id,
            event_type="routing",
            data_class="synthetic",
            owner="example-owner",
            repository="example-owner/platform",
            env=self.env,
            clock=fixed_clock,
        )
        serialized = json.dumps(result["episode"])
        self.assertNotIn(body, serialized)
        for forbidden in ("body", "prompt", "transcript", "output"):
            self.assertNotIn(f'"{forbidden}"', serialized)
        again = om.ingest_submission(
            submission_id,
            event_type="routing",
            data_class="synthetic",
            owner="example-owner",
            repository="example-owner/platform",
            env=self.env,
            clock=fixed_clock,
        )
        self.assertEqual(again["status"], "DUPLICATE")


class ReadScopeTests(_Fixture):
    def _one(self) -> str:
        return self.put(doc())["episode"]["episode_id"]

    def test_read_requires_an_explicit_scope(self) -> None:
        episode_id = self._one()
        with self.assertRaises(om.EpisodeInvalid):
            om.read_episode(
                episode_id, env=self.env, retention_seconds=RETENTION, now=NOW
            )

    def test_read_refuses_an_incomplete_scope(self) -> None:
        episode_id = self._one()
        with self.assertRaises(om.EpisodeInvalid):
            om.read_episode(
                episode_id,
                env=self.env,
                scope={
                    "owner": "example-owner",
                    "repository": "example-owner/platform",
                },
                retention_seconds=RETENTION,
                now=NOW,
            )

    def test_read_refuses_a_mismatched_scope(self) -> None:
        episode_id = self._one()
        with self.assertRaises(om.EpisodeUnavailable):
            om.read_episode(
                episode_id,
                env=self.env,
                scope={
                    "owner": "other-owner",
                    "repository": "other-owner/platform",
                    "data_class": "synthetic",
                },
                retention_seconds=RETENTION,
                now=NOW,
            )

    def test_read_returns_only_for_the_exact_scope(self) -> None:
        episode_id = self._one()
        view = om.read_episode(
            episode_id,
            env=self.env,
            scope=self.scope(),
            retention_seconds=RETENTION,
            now=NOW,
        )
        self.assertEqual(view["episode_id"], episode_id)
        self.assertEqual(view["scope"], self.scope())


class IntakeRetrievalTests(_Fixture):
    """A real synthetic Intake -> ingest -> list -> read round trip."""

    def _admit(self) -> str:
        result = intake.submit(
            {
                "schema": intake.REQUEST_SCHEMA,
                "item_id": "synthetic-item",
                "title": "A bounded objective",
                "body": "please investigate the retry behavior",
                "evidence_refs": [
                    {"ref": "urn:example:observation:1", "fingerprint": DIGEST_A}
                ],
            },
            env=self.env,
            clock=fixed_clock,
        )
        return result["submission"]["submission_id"]

    def _ingest(self, submission_id: str) -> str:
        admitted = om.ingest_submission(
            submission_id,
            event_type="routing",
            data_class="synthetic",
            owner="example-owner",
            repository="example-owner/platform",
            env=self.env,
            clock=fixed_clock,
        )
        return admitted["episode"]["episode_id"]

    def test_intake_episode_without_event_time_is_retrievable(self) -> None:
        episode_id = self._ingest(self._admit())
        stored = json.loads(
            (self.store / (episode_id + ".json")).read_text(encoding="utf-8")
        )
        # The absent source event time is preserved, never fabricated.
        self.assertIsNone(stored["event"]["event_time"])
        self.assertEqual(stored["event"]["observed_at"], "2026-01-01T12:00:00Z")
        views = om.list_episodes(
            env=self.env, scope=self.scope(), retention_seconds=RETENTION, now=NOW
        )
        self.assertEqual([v["episode_id"] for v in views], [episode_id])
        view = self.read(episode_id)
        self.assertEqual(view["episode_id"], episode_id)
        self.assertIsNone(view["event"]["event_time"])

    def test_absent_event_time_uses_the_named_observed_at_fallback(self) -> None:
        direct = self.put(doc())  # event_time 2026-01-01T00:00:00Z
        direct_id = direct["episode"]["episode_id"]
        adapter_id = self._ingest(self._admit())  # observed_at 2026-01-01T12:00:00Z
        ordered = om.list_episodes(
            env=self.env, scope=self.scope(), retention_seconds=RETENTION, now=NOW
        )
        self.assertEqual([v["episode_id"] for v in ordered], [direct_id, adapter_id])
        # A window that excludes the source event time still retains the
        # fallback episode and reads it back; the direct one is out of window.
        window = om.list_episodes(
            env=self.env,
            scope=self.scope(),
            retention_seconds=1,
            now="2026-01-01T12:00:00Z",
        )
        self.assertEqual([v["episode_id"] for v in window], [adapter_id])
        self.assertIsNone(self.read(adapter_id)["event"]["event_time"])
        with self.assertRaises(om.EpisodeUnavailable):
            self.read(direct_id, retention_seconds=1, now="2026-01-01T12:00:00Z")


class BoundaryTests(unittest.TestCase):
    def _source(self) -> ast.Module:
        path = Path(om.__file__)
        return ast.parse(path.read_text(encoding="utf-8"))

    def test_no_egress_shell_or_dynamic_import(self) -> None:
        banned = {
            "socket",
            "ssl",
            "urllib",
            "http",
            "requests",
            "httpx",
            "aiohttp",
            "subprocess",
        }
        imported: set[str] = set()
        for node in ast.walk(self._source()):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported & banned, set())

    def test_no_runtime_plugin_or_team_execution_import(self) -> None:
        imported: set[str] = set()
        for node in ast.walk(self._source()):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        offenders = {
            module
            for module in imported
            if module in {"plugins", "cao.plugins"}
            or module.startswith(("plugins.", "cao.plugins."))
        }
        self.assertEqual(offenders, set())
        for forbidden in ("cao.runtime", "cao.policy", "cao.contracts", "cao.team"):
            self.assertNotIn(forbidden, imported)

    def test_no_shell_invocation(self) -> None:
        text = Path(om.__file__).read_text(encoding="utf-8")
        self.assertNotIn("os.system", text)
        self.assertNotIn("shell=True", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
