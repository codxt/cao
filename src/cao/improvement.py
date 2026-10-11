"""Read-only, source-bound feedback from operating evidence to investigation.

This is an advisory layer. It never reads a provider, performs an agent prompt,
creates a Registry claim, changes an alert, edits code, or deploys software.
Evidence references are supplied by the owning source; their existence and
fingerprints must be verified by the accountable operator at acceptance.
"""
from __future__ import annotations

import copy
import math
import re
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from . import evidence

CASE_SCHEMA = "cao.improvement.case.v1"
AFTER_SCHEMA = "cao.improvement.after.v1"
CAUSE_SCHEMA = "cao.improvement.cause.v1"
HISTORY_SCHEMA = "cao.improvement.history.v1"
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_REPO = re.compile(r"[a-z0-9][a-z0-9._-]{0,98}/[a-z0-9][a-z0-9._-]{0,98}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REF = re.compile(r"(?:urn:|https://|git:)[^\s]{1,500}\Z")
_HOST = re.compile(r"[A-Za-z0-9.-]{1,253}\Z")
_UTC_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z\Z")
_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z")
_KINDS = frozenset({"service", "deployment", "database", "queue", "repository", "client"})
_RELATIONS = frozenset({"depends_on", "connects_to", "calls", "deployed_by", "owns", "routes_to"})
_SOURCES = frozenset({"runtime", "ci", "factory", "telemetry", "support", "synthetic", "operator", "analytics"})
_VERDICTS = frozenset({"SUPPORTED", "UNKNOWN", "UNAVAILABLE", "REFUSED", "INVALID"})
_FINDINGS = frozenset({"adverse", "normal", "unknown"})
_SEVERITIES = ("low", "medium", "high", "critical")
MAX_NODES, MAX_EDGES, MAX_SIGNALS, MAX_OBJECTIVES = 80, 160, 128, 24
MAX_HISTORY = 128


class ImprovementError(ValueError):
    """An unqualified input or unsafe inference."""
    code = "IMPROVEMENT_INVALID"


def _fields(value: Any, allowed: set[str], required: set[str], what: str) -> dict:
    if type(value) is not dict or set(value) - allowed or required - set(value):
        raise ImprovementError(f"{what}: missing or unknown fields")
    return value


def _seq(value: Any, limit: int, what: str, *, empty: bool = False) -> list:
    if type(value) is not list or len(value) > limit or (not empty and not value):
        raise ImprovementError(f"{what}: expected 1..{limit} items")
    return value


def _id(value: Any, what: str) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        raise ImprovementError(f"{what}: invalid bounded identifier")
    return value


def _text(value: Any, what: str, limit: int = 300) -> str:
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise ImprovementError(f"{what}: missing or unbounded text")
    return value


def _choice(value: Any, choices: set[str] | frozenset[str] | tuple, what: str) -> str:
    if type(value) is not str or value not in choices:
        raise ImprovementError(f"{what}: unsupported value")
    return value


def _number(value: Any, what: str) -> float:
    if type(value) not in (float, int):
        raise ImprovementError(f"{what}: expected finite number")
    try:
        numeric = float(value)
    except (OverflowError, ValueError) as exc:
        raise ImprovementError(f"{what}: numeric value outside supported range") from exc
    if not math.isfinite(numeric):
        raise ImprovementError(f"{what}: expected finite number")
    return numeric


def _time(value: Any, what: str) -> datetime:
    if not isinstance(value, str) or not _UTC_TIME.fullmatch(value):
        raise ImprovementError(f"{what}: expected RFC3339 UTC ending in Z")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ImprovementError(f"{what}: invalid UTC timestamp") from exc
    if result.utcoffset() is None or result.utcoffset().total_seconds() != 0:
        raise ImprovementError(f"{what}: timestamp must be UTC")
    return result


def _proof(value: Any, what: str) -> None:
    _fields(value, {"source", "ref", "fingerprint", "observed_at", "verdict"},
            {"source", "ref", "fingerprint", "observed_at", "verdict"}, what)
    _choice(value["source"], _SOURCES, what + ".source")
    ref = _text(value["ref"], what + ".ref", 512)
    if not _REF.fullmatch(ref):
        raise ImprovementError(f"{what}: evidence ref requires urn, https or git")
    if ref.startswith("https://"):
        try:
            parsed = urlsplit(ref)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise ImprovementError(f"{what}: malformed HTTPS reference") from exc
        authority = hostname if port is None else hostname + ":" + str(port)
        if (parsed.scheme != "https" or not hostname
                or not _HOST.fullmatch(hostname)
                or parsed.netloc != authority
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment
                or "?" in ref or "#" in ref
                or any(c in parsed.path for c in (";", "@", "%", "="))):
            raise ImprovementError(f"{what}: unsafe or credential-bearing URL")
    else:
        namespace = ("urn:" + value["source"] + ":", "git:" + value["source"] + ":")
        if (not ref.startswith(namespace)
                or not re.fullmatch(r"(?:urn|git):[a-z]+:[A-Za-z0-9._:/-]{1,400}", ref)
                or "//" in ref):
            raise ImprovementError(f"{what}: invalid or source-mismatched proof reference")
    if type(value["fingerprint"]) is not str or not _FINGERPRINT.fullmatch(value["fingerprint"]):
        raise ImprovementError(f"{what}: expected sha256 evidence fingerprint")
    _time(value["observed_at"], what + ".observed_at")
    _choice(value["verdict"], _VERDICTS, what + ".verdict")


def _distinct_proofs(proofs: list[dict], what: str) -> None:
    """One artifact identity cannot masquerade as several independent sources."""
    refs = [p["ref"] for p in proofs]
    digests = [p["fingerprint"] for p in proofs]
    if len(refs) != len(set(refs)) or len(digests) != len(set(digests)):
        raise ImprovementError(f"{what}: repeated evidence reference or fingerprint")


def _measurement(value: Any, what: str) -> None:
    _fields(value, {"value", "proof", "release_sha"}, {"value", "proof", "release_sha"}, what)
    _number(value["value"], what + ".value")
    _proof(value["proof"], what + ".proof")
    if type(value["release_sha"]) is not str or not _SHA.fullmatch(value["release_sha"]):
        raise ImprovementError(f"{what}: invalid release SHA")


def _positive_id_list(ids: list[str], label: str) -> None:
    if len(ids) != len(set(ids)):
        raise ImprovementError(f"{label}: duplicate identifier")


def validate_case(value: Any) -> dict:
    raw = _fields(value, {"schema", "case_id", "subject", "detected_at", "data_class",
                          "context", "signals", "objectives"},
                  {"schema", "case_id", "subject", "detected_at", "data_class",
                   "context", "signals", "objectives"}, "case")
    if raw["schema"] != CASE_SCHEMA:
        raise ImprovementError("unsupported improvement case schema")
    _id(raw["case_id"], "case_id")
    _choice(raw["data_class"], {"synthetic", "source_attested"}, "case.data_class")
    _id(raw["subject"], "subject")
    _time(raw["detected_at"], "detected_at")
    context = _fields(raw["context"], {"nodes", "edges"}, {"nodes", "edges"}, "context")
    nodes = _seq(context["nodes"], MAX_NODES, "nodes")
    _seq(context["edges"], MAX_EDGES, "edges", empty=True)
    for node in nodes:
        _fields(node, {"id", "kind", "owner_repo", "source_path", "release_sha", "scope"},
                {"id", "kind"}, "node")
        _id(node["id"], "node.id")
        if "scope" in node:
            _id(node["scope"], "node.scope")
        _choice(node["kind"], _KINDS, "node.kind")
        if "owner_repo" in node:
            if (type(node["owner_repo"]) is not str
                    or not _REPO.fullmatch(node["owner_repo"])
                    or node["owner_repo"].endswith(".git")
                    or ".." in node["owner_repo"]):
                raise ImprovementError("node.owner_repo must identify one canonical repository")
            if "source_path" not in node:
                raise ImprovementError("node.owner_repo requires exact source_path")
        if "source_path" in node:
            if "owner_repo" not in node or type(node["source_path"]) is not str or not _PATH.fullmatch(node["source_path"]):
                raise ImprovementError("node.source_path must be a bounded repo-relative path")
            if any(part in (".", "..") for part in node["source_path"].split("/")):
                raise ImprovementError("node.source_path traversal forbidden")
        if "release_sha" in node and (
                type(node["release_sha"]) is not str or not _SHA.fullmatch(node["release_sha"])):
            raise ImprovementError("node.release_sha must be full SHA")
    ids = [n["id"] for n in nodes]
    _positive_id_list(ids, "context.nodes")
    if raw["subject"] not in ids:
        raise ImprovementError("subject missing from context graph")
    edges = []
    for edge in context["edges"]:
        _fields(edge, {"from", "to", "relation"},
                {"from", "to", "relation"}, "edge")
        if edge["from"] not in ids or edge["to"] not in ids or edge["from"] == edge["to"]:
            raise ImprovementError("context edge has missing/self-linked endpoint")
        _choice(edge["relation"], _RELATIONS, "edge.relation")
        edges.append((edge["from"], edge["to"], edge["relation"]))
    if len(edges) != len(set(edges)):
        raise ImprovementError("duplicate context edge")
    signals = _seq(raw["signals"], MAX_SIGNALS, "signals")
    for sig in signals:
        _fields(sig, {"id", "resource", "issue", "finding", "severity", "proof",
                      "threshold", "metric"},
                {"id", "resource", "issue", "finding", "severity", "proof"}, "signal")
        _id(sig["id"], "signal.id")
        if sig["resource"] not in ids:
            raise ImprovementError("signal resource has no context node")
        _id(sig["issue"], "signal.issue")
        _choice(sig["finding"], _FINDINGS, "signal.finding")
        _choice(sig["severity"], _SEVERITIES, "signal.severity")
        _proof(sig["proof"], "signal.proof")
        if sig["finding"] != "unknown" and sig["proof"]["verdict"] != "SUPPORTED":
            raise ImprovementError("adverse/normal signals require SUPPORTED source evidence")
        if sig["finding"] == "unknown" and sig["proof"]["verdict"] == "SUPPORTED":
            raise ImprovementError("unknown finding contradicts supported evidence")
        if "metric" in sig:
            _id(sig["metric"], "signal.metric")
        if "threshold" in sig:
            if "metric" not in sig:
                raise ImprovementError("numeric signal threshold requires metric identity")
            t = _fields(sig["threshold"], {"op", "value", "limit"},
                        {"op", "value", "limit"}, "signal.threshold")
            op = _choice(t["op"], {"above", "below"}, "signal.threshold.op")
            actual = _number(t["value"], "signal.threshold.value")
            bound = _number(t["limit"], "signal.threshold.limit")
            computed = "adverse" if ((actual > bound) if op == "above" else (actual < bound)) else "normal"
            if sig["finding"] != computed:
                raise ImprovementError("source finding contradicts numeric threshold")
    _positive_id_list([s["id"] for s in signals], "signals")
    objectives = _seq(raw["objectives"], MAX_OBJECTIVES, "objectives", empty=True)
    for obj in objectives:
        _fields(obj, {"id", "resource", "issue", "metric", "direction", "target", "guardrail", "before"},
                {"id", "resource", "issue", "metric", "direction", "target", "guardrail", "before"}, "objective")
        _id(obj["id"], "objective.id")
        if "issue" not in obj:
            raise ImprovementError("objective must name an issue")
        _id(obj["issue"], "objective.issue")
        if obj["issue"] not in {sig["issue"] for sig in signals}:
            raise ImprovementError("objective must be bound to an observed issue")
        _id(obj["resource"], "objective.resource")
        if obj["resource"] not in ids:
            raise ImprovementError("objective refers to unknown resource")
        _id(obj["metric"], "objective.metric")
        _choice(obj["direction"], {"min", "max"}, "objective.direction")
        _number(obj["target"], "objective.target")
        if type(obj["guardrail"]) is not bool:
            raise ImprovementError("objective.guardrail must be boolean")
        _measurement(obj["before"], "objective.before")
        owner = next(node for node in nodes if node["id"] == obj["resource"])
        if "release_sha" not in owner:
            raise ImprovementError("objective baseline requires bound resource release")
        if obj["before"]["release_sha"] != owner["release_sha"]:
            raise ImprovementError("objective baseline SHA differs from resource release")
    _positive_id_list([o["id"] for o in objectives], "objectives")
    by_metric = [(o["issue"], o["resource"], o["metric"]) for o in objectives]
    if len(by_metric) != len(set(by_metric)):
        raise ImprovementError("duplicate or contradictory metric objective")
    all_proofs = [x["proof"] for x in signals] + [
        goal["before"]["proof"] for goal in objectives]
    _distinct_proofs(all_proofs, "case")
    if raw["data_class"] == "source_attested" and any(
            proof["source"] == "synthetic" for proof in all_proofs):
        raise ImprovementError("source-attested case cannot contain synthetic proof")
    return copy.deepcopy(raw)


def context_for(case: Any, resource_id: str, *, depth: int = 1) -> dict:
    doc = validate_case(case)
    if resource_id not in {n["id"] for n in doc["context"]["nodes"]}:
        raise ImprovementError("resource absent from context")
    if type(depth) is not int or depth not in (0, 1, 2):
        raise ImprovementError("context depth must be between 0 and 2")
    nodes = {n["id"]: n for n in doc["context"]["nodes"]}
    source_scope = nodes[resource_id].get("scope")
    visited = {resource_id}
    links = doc["context"]["edges"]
    for _ in range(depth):
        if source_scope is None:
            # Unscoped source nodes cannot authorize an inferred neighbour.
            break
        frontier = visited.copy()
        new = {edge["to"] for edge in links if edge["from"] in frontier}
        new |= {edge["from"] for edge in links if edge["to"] in frontier}
        visited |= {node_id for node_id in new
                    if nodes[node_id].get("scope") == source_scope}
    return {
        "schema": "cao.improvement.context.v1", "status": "DECLARED_PROJECTION",
        "case_id": doc["case_id"], "data_class": doc["data_class"],
        "center": resource_id,
        "nodes": [n for n in doc["context"]["nodes"] if n["id"] in visited],
        "edges": [e for e in links if e["from"] in visited and e["to"] in visited],
        "warning": "Context is a source declaration, not a live topology assertion.",
        "source_fingerprint": evidence.fingerprint(doc),
    }


def _linked(nodes: set[str], edges: list[dict]) -> bool:
    if not nodes:
        return False
    seen = {next(iter(sorted(nodes)))}
    while True:
        new = seen | {e["to"] for e in edges if e["from"] in seen} | {
            e["from"] for e in edges if e["to"] in seen}
        if new == seen:
            return nodes <= seen
        seen = new


def assess(case: Any) -> dict:
    doc = validate_case(case)
    indexed = {n["id"]: n for n in doc["context"]["nodes"]}
    groups: dict[str, list[dict]] = defaultdict(list)
    for sig in doc["signals"]:
        groups[sig["issue"]].append(sig)
    issues = []
    subject_neighbours = {doc["subject"]}
    for _ in range(2):
        subject_neighbours |= {
            edge["to"] for edge in doc["context"]["edges"]
            if edge["from"] in subject_neighbours
        } | {
            edge["from"] for edge in doc["context"]["edges"]
            if edge["to"] in subject_neighbours
        }
    for key, signals in sorted(groups.items()):
        adverse = [s for s in signals if s["finding"] == "adverse"
                   and s["proof"]["verdict"] == "SUPPORTED"]
        pending = [s for s in signals if s["finding"] == "unknown"]
        resources = {s["resource"] for s in adverse}
        # Graph connectivity alone is not tenant/data-sharing authority.
        # Cross-resource correlation also needs an explicit common boundary.
        scopes = {indexed[r].get("scope") for r in resources | {doc["subject"]}}
        # Intermediate unrelated infrastructure cannot bridge two distinct
        # incidents into one merely because a shared database/gateway exists.
        adverse_edges = [
            e for e in doc["context"]["edges"]
            if e["from"] in resources and e["to"] in resources
        ]
        consistent = (
            resources <= subject_neighbours
            and _linked(resources, adverse_edges)
            and len(scopes) == 1 and None not in scopes
        )
        sources = {s["proof"]["source"] for s in adverse}
        independent_artifacts = {
            (s["proof"]["ref"], s["proof"]["fingerprint"]) for s in adverse
        }
        distinct_fingerprints = {s["proof"]["fingerprint"] for s in adverse}
        if not adverse:
            status = "SOURCE_UNCERTAIN" if pending else "NO_ADVERSE_SIGNAL"
            action = "RECHECK_EVIDENCE" if pending else "NONE"
        elif not consistent:
            status, action = "UNLINKED_EVIDENCE", "RECONCILE_RESOURCE_MAPPING"
        else:
            status = ("CORROBORATED" if len(sources) > 1
                      and len(independent_artifacts) > 1
                      and len(distinct_fingerprints) > 1 else "CANDIDATE")
            action = "READ_INVESTIGATION"
        repos = sorted({indexed[r]["owner_repo"] for r in resources
                        if "owner_repo" in indexed[r]})
        paths = sorted({(indexed[r]["owner_repo"], indexed[r]["source_path"])
                        for r in resources if "owner_repo" in indexed[r]})
        issues.append({
            "issue": key, "status": status, "next_action": action,
            "severity": max((s["severity"] for s in adverse),
                            key=_SEVERITIES.index) if adverse else None,
            "resources": sorted(resources),
            "source_count": len(sources), "unknown_count": len(pending),
            "possible_owners": repos,
            "declared_source_paths": [{"repo": repo, "path": path} for repo, path in paths],
            "evidence": [{"id": s["id"], **s["proof"]} for s in adverse],
            "held_evidence": [{"id": s["id"], **s["proof"]} for s in pending],
            "normal_evidence": [
                {"id": s["id"], **s["proof"]}
                for s in signals if s["finding"] == "normal"],
            "warning": "Corroboration establishes observations, not root cause or write authority.",
        })
    return {
        "schema": "cao.improvement.assessment.v1",
        "status": "INVESTIGATE" if any(x["next_action"] == "READ_INVESTIGATION" for x in issues)
                  else "HOLD" if any(x["next_action"] != "NONE" for x in issues)
                  else "NO_ACTION",
        "case_id": doc["case_id"], "data_class": doc["data_class"],
        "case_fingerprint": evidence.fingerprint(doc),
        "issues": issues, "objectives": [
            {"id": o["id"], "resource": o["resource"], "metric": o["metric"],
             "target": o["target"], "direction": o["direction"],
             "guardrail": o["guardrail"], "before": o["before"]["value"]}
            for o in doc["objectives"]
        ],
        "permissions": {"investigate": "READ_ONLY",
                        "write": "NOT_GRANTED", "publish": "NOT_GRANTED",
                        "deploy": "NOT_GRANTED"},
    }


def _accepted_proof(proof: Mapping[str, Any]) -> bool:
    """A source's positive assertion, not an independent source fetch."""
    return proof["verdict"] == "SUPPORTED"


def validate_after(value: Any, case: Any) -> dict:
    before = validate_case(case)
    raw = _fields(value, {"schema", "case_id", "case_fingerprint",
                          "intervention", "measurements"},
                  {"schema", "case_id", "case_fingerprint",
                   "intervention", "measurements"}, "after")
    if raw["schema"] != AFTER_SCHEMA or raw["case_id"] != before["case_id"]:
        raise ImprovementError("after evidence does not match case schema/identity")
    if raw["case_fingerprint"] != evidence.fingerprint(before):
        raise ImprovementError("after evidence is bound to a different immutable case")
    i = _fields(raw["intervention"], {"repo", "commit_sha", "proposed_at",
                                      "review", "ci", "deployment"},
                {"repo", "commit_sha", "proposed_at", "review", "ci", "deployment"},
                "intervention")
    if (type(i["repo"]) is not str or not _REPO.fullmatch(i["repo"])
            or i["repo"].endswith(".git") or ".." in i["repo"]):
        raise ImprovementError("intervention.repo invalid")
    if type(i["commit_sha"]) is not str or not _SHA.fullmatch(i["commit_sha"]):
        raise ImprovementError("intervention.commit_sha invalid")
    proposal_at = _time(i["proposed_at"], "intervention.proposed_at")
    if proposal_at < _time(before["detected_at"], "case.detected_at"):
        raise ImprovementError("proposal predates detected case")
    repos = {n.get("owner_repo") for n in before["context"]["nodes"]}
    if i["repo"] not in repos:
        raise ImprovementError("intervention repository not declared in context")
    for obj in before["objectives"]:
        measured_before = obj["before"]
        if _time(measured_before["proof"]["observed_at"], "baseline observed") > proposal_at:
            raise ImprovementError("baseline measurement postdates proposed fix")
        if measured_before["release_sha"] == i["commit_sha"]:
            raise ImprovementError("baseline refers to proposed change SHA")
    for name, statuses in (
            ("review", {"approved", "changes_requested", "unknown"}),
            ("ci", {"passed", "failed", "unknown"}),
            ("deployment", {"verified", "failed", "not_deployed", "unknown"})):
        item = _fields(i[name], {"status", "head", "proof", "completed_at"},
                       {"status", "head", "proof", "completed_at"}, name)
        _choice(item["status"], statuses, name + ".status")
        if type(item["head"]) is not str or not _SHA.fullmatch(item["head"]):
            raise ImprovementError(f"{name}.head invalid")
        _proof(item["proof"], name + ".proof")
        expected_sources = {
            "review": {"operator"}, "ci": {"ci"},
            "deployment": {"runtime", "factory"},
        }[name]
        if item["proof"]["source"] not in expected_sources:
            raise ImprovementError(name + ": evidence source does not match gate")
        t = _time(item["completed_at"], name + ".completed_at")
        observed = _time(item["proof"]["observed_at"], name + ".proof observed")
        if t < proposal_at or observed < proposal_at or observed > t:
            raise ImprovementError(f"{name} source evidence is outside the proposed-stage window")
        if (item["status"] in ("approved", "passed", "verified")
                and not _accepted_proof(item["proof"])):
            raise ImprovementError(f"{name} reports success without supported evidence")
        if item["status"] == "unknown" and _accepted_proof(item["proof"]):
            raise ImprovementError(f"{name} unknown status contradicts evidence")
    stage_proofs = [i[stage]["proof"] for stage in ("review", "ci", "deployment")]
    if (len({p["ref"] for p in stage_proofs}) != 3
            or len({p["fingerprint"] for p in stage_proofs}) != 3):
        raise ImprovementError("review, CI, and deployment must have independent artifacts")
    if _time(i["deployment"]["completed_at"], "deployed") < max(
            _time(i["review"]["completed_at"], "review"),
            _time(i["ci"]["completed_at"], "ci")):
        raise ImprovementError("deployment observation precedes review or CI")
    measured = _seq(raw["measurements"], MAX_OBJECTIVES, "measurements", empty=True)
    expected = {o["id"] for o in before["objectives"]}
    ids = []
    for m in measured:
        _fields(m, {"objective_id", "measurement"},
                {"objective_id", "measurement"}, "after.measurement")
        _id(m["objective_id"], "after.objective_id")
        if m["objective_id"] not in expected:
            raise ImprovementError("after objective not in original case")
        ids.append(m["objective_id"])
        _measurement(m["measurement"], "after.measurement")
    _positive_id_list(ids, "after.measurements")
    case_proofs = ([sig["proof"] for sig in before["signals"]]
                   + [goal["before"]["proof"] for goal in before["objectives"]])
    _distinct_proofs(case_proofs + stage_proofs
                     + [m["measurement"]["proof"] for m in measured],
                     "case and follow-up")
    for sig in before["signals"]:
        if _time(sig["proof"]["observed_at"], "signal observed") > proposal_at:
            raise ImprovementError("signal observed after proposed fix")
    return copy.deepcopy(raw)


def _threshold(value: float, goal: Mapping[str, Any]) -> bool:
    return value >= goal["target"] if goal["direction"] == "max" else value <= goal["target"]


def _gain(before: float, after: float, direction: str) -> float:
    return after - before if direction == "max" else before - after


def _qualify_intervention(i: Mapping[str, Any]) -> tuple[bool, list[str]]:
    reasons = []
    for stage, allowed in (("review", "approved"), ("ci", "passed"),
                           ("deployment", "verified")):
        item = i[stage]
        if item["status"] != allowed or not _accepted_proof(item["proof"]):
            reasons.append(stage + " is not positively qualified")
        if item["head"] != i["commit_sha"]:
            reasons.append(stage + " references a different commit")
    return not reasons, reasons


def verify(case: Any, after: Any) -> dict:
    before = validate_case(case)
    measured = validate_after(after, before)
    i = measured["intervention"]
    qualified, reasons = _qualify_intervention(i)
    diagnosis = assess(before)
    active = [issue for issue in diagnosis["issues"] if issue["evidence"]]
    if not active:
        reasons.append("no supported adverse issue to resolve")
    for issue in diagnosis["issues"]:
        if issue["unknown_count"]:
            reasons.append("unresolved source coverage: " + issue["issue"])
    for issue in active:
        relevant = [
            goal for goal in before["objectives"]
            if (goal["issue"] == issue["issue"] and not goal["guardrail"]
                and goal["resource"] in issue["resources"])
        ]
        if not relevant:
            reasons.append("issue has no bound success objective: " + issue["issue"])
        elif not any(not _threshold(goal["before"]["value"], goal) for goal in relevant):
            reasons.append("issue had no unmet baseline objective: " + issue["issue"])
        if i["repo"] not in issue["possible_owners"]:
            reasons.append("fix repository not owned by affected issue: " + issue["issue"])
        if issue["status"] == "UNLINKED_EVIDENCE":
            reasons.append("issue resource correlation unverified: " + issue["issue"])
    after_values = {x["objective_id"]: x["measurement"] for x in measured["measurements"]}
    metrics = []
    for obj in before["objectives"]:
        baseline = obj["before"]
        current = after_values.get(obj["id"])
        if current is None:
            reasons.append("missing after measurement: " + obj["id"])
        else:
            if current["release_sha"] != i["commit_sha"]:
                reasons.append("after measurement references different release: " + obj["id"])
            if not _accepted_proof(current["proof"]):
                reasons.append("after measurement unqualified: " + obj["id"])
            if _time(current["proof"]["observed_at"], "after observed_at") <= _time(
                    baseline["proof"]["observed_at"], "before observed_at"):
                reasons.append("after measurement is not later than before: " + obj["id"])
            if _time(current["proof"]["observed_at"], "after observed_at") < _time(
                    i["deployment"]["completed_at"], "deployment.completed_at"):
                reasons.append("after measurement predates deployed release: " + obj["id"])
            if current["proof"]["fingerprint"] == baseline["proof"]["fingerprint"]:
                reasons.append("reused baseline evidence as after: " + obj["id"])
            if not _accepted_proof(baseline["proof"]):
                reasons.append("baseline measurement unqualified: " + obj["id"])
        gain = _gain(baseline["value"], current["value"], obj["direction"]) if current else None
        metrics.append({
            "objective_id": obj["id"], "metric": obj["metric"],
            "issue": obj["issue"], "resource": obj["resource"],
            "guardrail": obj["guardrail"], "before": baseline["value"],
            "after": current["value"] if current else None, "gain": gain,
            "target_met": _threshold(current["value"], obj) if current else None,
            "evidence_refs": [baseline["proof"]["ref"]] +
                             ([current["proof"]["ref"]] if current else []),
        })
    # Even an improved target cannot erase a still-adverse numeric signal
    # from the same issue/resource/metric.
    unresolved_conditions = []
    for sig in before["signals"]:
        if sig["finding"] != "adverse" or "threshold" not in sig:
            continue
        bound = next((m for m in metrics if m["resource"] == sig["resource"]
                      and m["issue"] == sig["issue"] and m["metric"] == sig["metric"]), None)
        if bound is None:
            reasons.append("numeric adverse signal has no outcome metric: " + sig["id"])
            continue
        if bound["after"] is not None:
            rule = sig["threshold"]
            still_adverse = (bound["after"] > rule["limit"]) if rule["op"] == "above" else (
                bound["after"] < rule["limit"])
            if still_adverse:
                unresolved_conditions.append(sig["id"])
    adverse_gain = any(m["gain"] is not None and m["gain"] < 0 for m in metrics)
    if not qualified or reasons:
        verdict = "UNQUALIFIED"
    elif adverse_gain or any(m["guardrail"] and not m["target_met"] for m in metrics):
        verdict = "REGRESSION"
    elif (unresolved_conditions
            or any(not m["target_met"] for m in metrics if not m["guardrail"])):
        verdict = "TARGET_NOT_MET"
    elif any(m["gain"] > 0 for m in metrics if not m["guardrail"]):
        verdict = "SOURCE_REPORTED_IMPROVEMENT"
    else:
        verdict = "NO_MEASURED_IMPROVEMENT"
    all_proofs = (
        [s["proof"] for s in before["signals"]]
        + [o["before"]["proof"] for o in before["objectives"]]
        + [i[step]["proof"] for step in ("review", "ci", "deployment")]
        + [m["measurement"]["proof"] for m in measured["measurements"]]
    )
    simulated = before["data_class"] == "synthetic" or any(
        proof["source"] == "synthetic" for proof in all_proofs)
    if simulated and verdict == "SOURCE_REPORTED_IMPROVEMENT":
        verdict = "SYNTHETIC_ONLY"
    detected = _time(before["detected_at"], "detected_at")
    proposed = _time(i["proposed_at"], "proposed_at")
    reviewed = _time(i["review"]["completed_at"], "reviewed")
    deployed = _time(i["deployment"]["completed_at"], "deployed")
    return {
        "schema": "cao.improvement.outcome.v1", "case_id": before["case_id"],
        "status": verdict,
        "case_fingerprint": evidence.fingerprint(before),
        "after_fingerprint": evidence.fingerprint(measured),
        "release_head": i["commit_sha"],
        "source_attested": not simulated and before["data_class"] == "source_attested" and all(
            p["verdict"] == "SUPPORTED" for p in all_proofs),
        "externally_authenticated": False,
        "regression_observed": adverse_gain,
        "simulated_sources": simulated,
        "accepted_as_canonical_operating_proof": False,
        "stage_evidence": {
            k: {"status": i[k]["status"], "head": i[k]["head"],
                "ref": i[k]["proof"]["ref"],
                "fingerprint": i[k]["proof"]["fingerprint"]}
            for k in ("review", "ci", "deployment")
        },
        "elapsed_seconds": {
            "detection_to_proposal": int((proposed - detected).total_seconds()),
            "detection_to_review": int((reviewed - detected).total_seconds()),
            "detection_to_deployment": int((deployed - detected).total_seconds()),
        },
        "metrics": metrics, "holds": sorted(set(reasons)),
        "unresolved_adverse_conditions": sorted(unresolved_conditions),
        "warning": "Source-reported measurements need independent artifact verification. This grants no release or authority.",
    }


def propose(case: Any, cause: Any) -> dict:
    doc = validate_case(case)
    raw = _fields(cause, {"schema", "case_id", "issue", "hypothesis",
                          "repo", "write_scope", "evidence_ids"},
                  {"schema", "case_id", "issue", "hypothesis",
                   "repo", "write_scope", "evidence_ids"}, "cause")
    if raw["schema"] != CAUSE_SCHEMA or raw["case_id"] != doc["case_id"]:
        raise ImprovementError("cause proposal case mismatch")
    _id(raw["issue"], "cause.issue")
    _text(raw["hypothesis"], "cause.hypothesis", 2000)
    if type(raw["repo"]) is not str or not _REPO.fullmatch(raw["repo"]):
        raise ImprovementError("cause.repo invalid")
    names = _seq(raw["write_scope"], 16, "cause.write_scope")
    for name in names:
        if type(name) is not str or not _PATH.fullmatch(name):
            raise ImprovementError("cause.write_scope must be exact repo-relative paths")
        parts = name.lower().split("/")
        restricted = {".git", ".github", ".env", "secrets", "secret",
                      "codeowners", "agents.md", "claude.md", "gemini.md",
                      "policy.py", "registry.py", "dockerfile",
                      "pyproject.toml", "setup.py", "makefile",
                      "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}
        suspicious = ("secret", "token", "cred", "passw", "private", "key", "pem")
        if any(part.startswith(".") or part in restricted
               or any(pattern in part for pattern in suspicious)
               for part in parts):
            raise ImprovementError("reserved/secret scope requires separate owner-governed review")
    _positive_id_list(names, "cause.write_scope")
    ids = _seq(raw["evidence_ids"], 32, "cause.evidence_ids")
    for ident in ids:
        _id(ident, "cause.evidence_id")
    _positive_id_list(ids, "cause.evidence_ids")
    current = assess(doc)
    selected = next((x for x in current["issues"] if x["issue"] == raw["issue"]), None)
    if selected is None or selected["next_action"] != "READ_INVESTIGATION":
        raise ImprovementError("selected issue lacks a supported, linked adverse signal")
    verified_ids = {x["id"] for x in selected["evidence"]}
    if not set(ids) <= verified_ids:
        raise ImprovementError("proposal cites unsupported or foreign evidence")
    permitted = {x["path"] for x in selected["declared_source_paths"]
                 if x["repo"] == raw["repo"]}
    if not set(names) <= permitted:
        raise ImprovementError("proposal paths are not declared sources for this owner")
    return {
        "schema": "cao.improvement.work-draft.v1",
        "status": "PROPOSAL_ONLY",
        "case_id": doc["case_id"], "data_class": doc["data_class"],
        "case_fingerprint": current["case_fingerprint"],
        "issue": raw["issue"], "hypothesis_unverified": raw["hypothesis"],
        "repo": raw["repo"], "suggested_write_scope": sorted(names),
        "scope_authority": "SOURCE_DECLARED_NOT_VERIFIED",
        "evidence": [x for x in selected["evidence"] if x["id"] in set(ids)],
        "acceptance_metrics": [
            {"id": o["id"], "target": o["target"],
             "direction": o["direction"], "guardrail": o["guardrail"]}
            for o in doc["objectives"]
        ],
        "operator_must": ["verify original evidence", "establish root cause",
                          "approve exact scope", "acquire separate Registry WRITE claim",
                          "review exact SHA and CI", "authorize deployment separately"],
        "no_execution_authority": True,
    }


def learn(history: Any) -> dict:
    data = _fields(history, {"schema", "entries"}, {"schema", "entries"}, "history")
    if data["schema"] != HISTORY_SCHEMA:
        raise ImprovementError("unsupported history schema")
    entries = _seq(data["entries"], MAX_HISTORY, "history.entries", empty=True)
    # Preflight all source evidence so an acceptance in an earlier entry
    # cannot masquerade as a later entry's CI/metric/source artifact.
    source_refs, source_fingerprints = set(), set()
    for entry in entries:
        _fields(entry, {"case", "after", "acceptance"},
                {"case", "after", "acceptance"}, "history.entry")
        source_case = validate_case(entry["case"])
        source_after = validate_after(entry["after"], source_case)
        source_proofs = (
            [s["proof"] for s in source_case["signals"]]
            + [goal["before"]["proof"] for goal in source_case["objectives"]]
            + [source_after["intervention"][name]["proof"]
               for name in ("review", "ci", "deployment")]
            + [m["measurement"]["proof"] for m in source_after["measurements"]]
        )
        source_refs.update(p["ref"] for p in source_proofs)
        source_fingerprints.update(p["fingerprint"] for p in source_proofs)
    collected: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
    observed_case_ids = set()
    seen_acceptance_refs, seen_acceptance_digests = set(), set()
    for entry in entries:
        _fields(entry, {"case", "after", "acceptance"},
                {"case", "after", "acceptance"}, "history.entry")
        case = validate_case(entry["case"])
        if case["case_id"] in observed_case_ids:
            raise ImprovementError("duplicate history case ID")
        observed_case_ids.add(case["case_id"])
        outcome = verify(case, entry["after"])
        acceptance = _fields(
            entry["acceptance"],
            {"decision", "case_fingerprint", "after_fingerprint", "proof"},
            {"decision", "case_fingerprint", "after_fingerprint", "proof"},
            "history.acceptance"
        )
        _choice(acceptance["decision"], {"accepted", "rejected"}, "acceptance.decision")
        _proof(acceptance["proof"], "acceptance.proof")
        if (acceptance["case_fingerprint"] != outcome["case_fingerprint"]
                or acceptance["after_fingerprint"] != outcome["after_fingerprint"]):
            raise ImprovementError("acceptance evidence disagrees with reviewed case/outcome")
        if acceptance["decision"] != "accepted":
            continue
        if (acceptance["proof"]["source"] != "operator"
                or acceptance["proof"]["verdict"] != "SUPPORTED"):
            raise ImprovementError("accepted learning requires operator-attested decision")
        all_after_proofs = (
            [entry["after"]["intervention"][name]["proof"]
             for name in ("review", "ci", "deployment")]
            + [m["measurement"]["proof"] for m in entry["after"]["measurements"]]
        )
        acceptance_time = _time(acceptance["proof"]["observed_at"], "acceptance time")
        last_source_observation = max(
            [_time(entry["after"]["intervention"]["deployment"]["completed_at"],
                   "deployment time")]
            + [_time(p["observed_at"], "accepted source observation")
               for p in all_after_proofs]
        )
        if acceptance_time < last_source_observation:
            raise ImprovementError("acceptance predates the evidence it claims to accept")
        prior_proofs = ([s["proof"] for s in case["signals"]]
                        + [o["before"]["proof"] for o in case["objectives"]]
                        + all_after_proofs)
        if (acceptance["proof"]["ref"] in {p["ref"] for p in prior_proofs}
                or acceptance["proof"]["fingerprint"] in
                {p["fingerprint"] for p in prior_proofs}):
            raise ImprovementError("acceptance cannot reuse case or outcome source artifacts")
        a_ref = acceptance["proof"]["ref"]
        a_digest = acceptance["proof"]["fingerprint"]
        if (a_ref in seen_acceptance_refs or a_digest in seen_acceptance_digests
                or a_ref in source_refs or a_digest in source_fingerprints):
            raise ImprovementError("replayed operator acceptance artifact")
        seen_acceptance_refs.add(a_ref)
        seen_acceptance_digests.add(a_digest)
        if outcome["status"] != "SOURCE_REPORTED_IMPROVEMENT":
            continue
        assessment = assess(case)
        for issue in assessment["issues"]:
            relevant_gain = any(
                (not metric["guardrail"] and metric["gain"] > 0
                 and metric["issue"] == issue["issue"]
                 and metric["resource"] in issue["resources"])
                for metric in outcome["metrics"]
            )
            source_node = next(
                n for n in case["context"]["nodes"] if n["id"] == case["subject"]
            )
            scope = source_node.get("scope")
            if (issue["status"] == "CORROBORATED" and relevant_gain
                    and len(issue["possible_owners"]) == 1 and scope is not None
                    and all(next(n for n in case["context"]["nodes"]
                                 if n["id"] == rid).get("scope") == scope
                            for rid in issue["resources"])):
                key = (issue["issue"], case["subject"],
                       issue["possible_owners"][0], scope)
                collected[key].append({
                    "case_id": case["case_id"], "case_fingerprint": assessment["case_fingerprint"],
                    "detected_at": case["detected_at"],
                    "after_fingerprint": outcome["after_fingerprint"],
                    "release_head": outcome["release_head"],
                    "resources": issue["resources"],
                    "sources": [x["source"] for x in issue["evidence"]],
                    "artifact_refs": [
                        s["proof"]["ref"] for s in case["signals"]
                        if s["issue"] == issue["issue"]
                    ] + [
                        obj["before"]["proof"]["ref"]
                        for obj in case["objectives"]
                        if obj["issue"] == issue["issue"]
                        and obj["resource"] in issue["resources"]
                    ] + [
                        sample["measurement"]["proof"]["ref"]
                        for sample in entry["after"]["measurements"]
                        if any(o["id"] == sample["objective_id"]
                               and o["issue"] == issue["issue"]
                               and o["resource"] in issue["resources"]
                               for o in case["objectives"])
                    ],
                    "artifact_fingerprints": [
                        s["proof"]["fingerprint"] for s in case["signals"]
                        if s["issue"] == issue["issue"]
                    ] + [
                        obj["before"]["proof"]["fingerprint"]
                        for obj in case["objectives"]
                        if obj["issue"] == issue["issue"]
                        and obj["resource"] in issue["resources"]
                    ] + [
                        sample["measurement"]["proof"]["fingerprint"]
                        for sample in entry["after"]["measurements"]
                        if any(o["id"] == sample["objective_id"]
                               and o["issue"] == issue["issue"]
                               and o["resource"] in issue["resources"]
                               for o in case["objectives"])
                    ],
                })
    suggestions = []
    for (issue, subject, owner_repo, scope), records in sorted(collected.items()):
        if (len(records) < 2 or len({x["release_head"] for x in records}) < 2
                or len({x["detected_at"] for x in records}) != len(records)
                or len({s for x in records for s in x["sources"]}) < 2):
            continue
        seen_refs, seen_digests = set(), set()
        repeated = False
        for item in records:
            refs, digests = set(item["artifact_refs"]), set(item["artifact_fingerprints"])
            if seen_refs & refs or seen_digests & digests:
                repeated = True
                break
            seen_refs |= refs
            seen_digests |= digests
        if repeated:
            # A renamed case or a recycled measurement is not new learning.
            continue
        suggestions.append({
            "issue": issue, "subject": subject, "owner_repo": owner_repo,
            "scope": scope, "type": "SUGGEST_NEW_OR_REFINED_DETECTION_CHECK",
            "status": "ADVISORY_ONLY",
            "supporting_cases": sorted(x["case_id"] for x in records),
            "supporting_releases": sorted({x["release_head"] for x in records}),
            "evidence_fingerprints": sorted(x["case_fingerprint"] for x in records),
            "candidate_questions": [
                "Can the existing observation detect this issue before impact?",
                "What added signal distinguishes it from benign behavior?",
                "Would this check increase false positives or collection cost?",
            ],
            "required_next_step": "Owner evaluates against a holdout and accepts any detection-rule change separately.",
        })
    return {
        "schema": "cao.improvement.learning.v1",
        "status": "PROPOSALS_ONLY",
        "sampled_cases": len(entries), "suggestions": suggestions,
        "rules_modified": 0, "tasks_started": 0,
    }
