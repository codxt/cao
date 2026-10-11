"""Opt-in, source-bound engineering automation; no new scheduler or authority.

A comparison alone cannot merge a PR. An external operator grant, distinct
exact-head review, branch rules, checked CI and unchanged source scope are all
required. A process-death checkpoint prevents replaying a possibly submitted
merge request. Only the existing CAO daemon supplies periodic wake-ups.
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import tempfile
try:
    import fcntl  # POSIX kernel lock; absence fails closed for effectful ticks
except ImportError:
    fcntl = None
from pathlib import Path
from typing import Any, Callable

SCHEMA = "cao.engineering.watch.v1"
STATE_SCHEMA = "cao.engineering.state.v1"
METRIC_SCHEMA = "tolest.gbrain.eval.comparison.v1"
SHA = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\Z")
REPO = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}/[a-z0-9][a-z0-9._-]{0,79}\Z")
PATH = re.compile(r"[A-Za-z0-9_.-][A-Za-z0-9_./-]{0,230}\Z")
CHECK = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.:()/+-]{0,127}\Z")
MAX_BYTES = 100_000
# A manifest permitting workflow/authority/infra changes is not low-risk auto merge.
PROTECTED = (".github/", ".git/", "docs/adr/", "deployment-targets",
             "tenants", "compose.", "Dockerfile", "platform/",
             "pyproject.toml", "setup.py", "requirements")


class EngineeringError(ValueError):
    code = "ENGINEERING_HELD"


def refuse(message: str) -> None:
    raise EngineeringError(message)


def _dict(value: Any, allowed: set[str], required: set[str], purpose: str) -> dict:
    if type(value) is not dict or not required <= value.keys() or value.keys() - allowed:
        refuse("invalid " + purpose + " fields")
    return value


def _string(value: Any, what: str, pattern=NAME) -> str:
    if type(value) is not str or not pattern.fullmatch(value):
        refuse("invalid " + what)
    return value


def _file(path: str) -> dict:
    if type(path) is not str or not os.path.isabs(path) or os.path.normpath(path) != path:
        refuse("path must be normalized absolute")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                refuse("required file is not bounded regular")
            contents = stream.read(MAX_BYTES + 1)
    except OSError as exc:
        raise EngineeringError("required file unavailable") from exc
    if len(contents) > MAX_BYTES:
        refuse("oversized file")
    def pairs(rows: list[tuple[str, Any]]) -> dict:
        out = {}
        for k, v in rows:
            if k in out:
                refuse("duplicate JSON key")
            out[k] = v
        return out
    try:
        result = json.loads(contents, object_pairs_hook=pairs,
                            parse_constant=lambda _: refuse("invalid numeric JSON"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EngineeringError("invalid JSON") from exc
    if type(result) is not dict:
        refuse("JSON object required")
    return result


def _utc(text: str) -> dt.datetime:
    if type(text) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text):
        refuse("invalid UTC time")
    try:
        return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise EngineeringError("invalid UTC value") from exc


def _digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True).encode()).hexdigest()


def validate(value: Any) -> dict:
    required = {"schema", "watch_id", "repo", "pr", "base_branch",
                "evaluation_file", "suite_sha256", "min_gain",
                "required_checks", "operators", "reviewers", "allowed_paths",
                "expires_at", "min_interval_seconds", "state_file", "auto_merge"}
    v = _dict(value, required, required, "engineering watch")
    if v["schema"] != SCHEMA:
        refuse("unsupported engineering manifest")
    _string(v["watch_id"], "watch_id")
    _string(v["repo"], "repo", REPO)
    _string(v["base_branch"], "base_branch", PATH)
    if v["base_branch"] not in ("main", "master"):
        refuse("auto merge only targets declared default branch")
    if type(v["pr"]) is not int or not 1 <= v["pr"] <= 10000000:
        refuse("invalid PR id")
    if type(v["suite_sha256"]) is not str or not SHA256.fullmatch(v["suite_sha256"]):
        refuse("exact suite SHA256 is required")
    gain = v["min_gain"]
    if type(gain) not in (int, float) or not math.isfinite(gain) or gain <= 0:
        refuse("min_gain must be positive")
    for key in ("state_file", "evaluation_file"):
        p = v[key]
        if type(p) is not str or not os.path.isabs(p) or os.path.normpath(p) != p or len(p) > 2048:
            refuse("invalid private path")
    checks = v["required_checks"]
    if type(checks) is not list or not 2 <= len(checks) <= 12:
        refuse("two or more required checks")
    for label in checks:
        _string(label, "required check", CHECK)
    if len(set(checks)) != len(checks):
        refuse("duplicate required check")
    if (not any("eval" in key.lower() for key in checks)
            or not any(any(label in key.lower() for label in
                           ("test", "validate", "unit", "ci")) for key in checks)):
        refuse("required checks must include an evaluation and a general CI gate")
    for key in ("operators", "reviewers"):
        names = v[key]
        if type(names) is not list or not 1 <= len(names) <= 12:
            refuse("invalid reviewer/operator roster")
        for login in names:
            _string(login, key)
        if len(set(names)) != len(names):
            refuse("duplicate role login")
    if set(v["operators"]) & set(v["reviewers"]):
        refuse("operator and reviewer must use independent identities")
    paths = v["allowed_paths"]
    if type(paths) is not list or not 1 <= len(paths) <= 64:
        refuse("invalid write scope")
    for name in paths:
        _string(name, "write scope", PATH)
        if (name.startswith("/") or name.startswith("../")
                or ".." in name.split("/") or "//" in name
                or name == "." or name.startswith(PROTECTED)):
            refuse("protected or unsafe change scope")
    if len(paths) != len(set(paths)):
        refuse("duplicate write scope")
    if type(v["min_interval_seconds"]) is not int or not 60 <= v["min_interval_seconds"] <= 86400:
        refuse("invalid polling cadence")
    if type(v["auto_merge"]) is not bool:
        refuse("auto_merge must be explicitly true or false")
    _utc(v["expires_at"])
    return v


def _comparison(v: dict, manifest: dict) -> tuple[str, str]:
    if (v.get("schema") != METRIC_SCHEMA
            or v.get("suite_sha256") != manifest["suite_sha256"]):
        refuse("evaluation suite identity mismatch")
    baseline = v.get("baseline_sha")
    head = v.get("candidate_sha")
    if type(baseline) is not str or not SHA.fullmatch(baseline):
        refuse("invalid evaluation baseline HEAD")
    if type(head) is not str or not SHA.fullmatch(head) or head == baseline:
        refuse("invalid evaluation candidate HEAD")
    if v.get("canonical_operating_proof") is not False or v.get("source_only") is not True:
        refuse("evaluation proof scope is not source-only")
    if v.get("status") == "SOURCE_REPORTED_IMPROVEMENT":
        gain = v.get("objective_gain")
        if type(gain) not in (int, float) or not math.isfinite(gain) or gain < manifest["min_gain"]:
            refuse("evaluation gain smaller than independent manifest threshold")
    return baseline, head


def _read_state(path: str, digest: str) -> dict | None:
    if not os.path.exists(path):
        return None
    doc = _file(path)
    if (doc.get("schema") != STATE_SCHEMA or doc.get("manifest_sha256") != digest
            or doc.get("phase") not in ("CHECKED", "SUBMITTING", "REQUESTED")
            or type(doc.get("last_checked")) is not str):
        refuse("existing engineering checkpoint differs or is corrupt")
    _utc(doc["last_checked"])
    return doc


def _save_state(path: str, value: dict) -> None:
    folder = os.path.dirname(path)
    if not os.path.isdir(folder) or os.path.islink(folder):
        refuse("engineering state directory is absent or linked")
    # The checkpoint belongs to the operator-controlled state directory and
    # must survive power loss before the GitHub effect can be issued.
    folder_stat = os.stat(folder, follow_symlinks=False)
    if folder_stat.st_mode & 0o022:
        refuse("engineering state directory is writable by other users")
    if os.path.lexists(path) and os.path.islink(path):
        refuse("engineering state path cannot be a symlink")
    fd, tmp = tempfile.mkstemp(prefix=".engineering-", dir=folder)
    try:
        with os.fdopen(fd, "w") as file:
            os.fchmod(file.fileno(), 0o600)
            json.dump(value, file, sort_keys=True, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
        dfd = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _gh_json(api_path: str) -> Any:
    if not re.fullmatch(r"[A-Za-z0-9_./?=&-]{1,400}", api_path):
        refuse("invalid GitHub API request")
    try:
        cmd = subprocess.run(["gh", "api", api_path], capture_output=True,
                             text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EngineeringError("GitHub read unavailable") from exc
    if cmd.returncode != 0 or len(cmd.stdout) > 300_000:
        refuse("GitHub API read unavailable")
    try:
        return json.loads(cmd.stdout)
    except json.JSONDecodeError as exc:
        raise EngineeringError("invalid GitHub response") from exc


def _submit_merge(repo: str, pr: int, head: str) -> None:
    try:
        cmd = subprocess.run(
            ["gh", "pr", "merge", str(pr), "--repo", repo, "--auto",
             "--squash", "--match-head-commit", head],
            capture_output=True, text=True, timeout=35)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EngineeringError("merge request transport unknown; reconcile upstream") from exc
    if cmd.returncode:
        refuse("GitHub refused native auto-merge; do not replay without reconciliation")


def _check_paths(pr: dict, paths: Any, manifest: dict) -> bool:
    if (type(paths) is not list
            or type(pr.get("changed_files")) is not int
            or pr["changed_files"] != len(paths)
            or not 1 <= len(paths) <= 100):
        return False
    for row in paths:
        if type(row) is not dict or type(row.get("filename")) is not str:
            return False
        locations = [row["filename"]]
        if row.get("status") in ("renamed", "copied"):
            if type(row.get("previous_filename")) is not str:
                return False
            locations.append(row["previous_filename"])
        for path in locations:
            if (path.startswith("/") or "//" in path or ".." in path.split("/")
                    or path.startswith(PROTECTED)
                    or not any(path == scope or path.startswith(scope.rstrip("/") + "/")
                               for scope in manifest["allowed_paths"])):
                return False
    return True


def _check_ci(result: Any, manifest: dict, head: str) -> bool:
    if (type(result) is not dict or type(result.get("total_count")) is not int
            or result["total_count"] > 100 or type(result.get("check_runs")) is not list
            or result["total_count"] != len(result["check_runs"])):
        return False
    expected = set(manifest["required_checks"])
    found = {}
    for row in result["check_runs"]:
        if type(row) is not dict or row.get("name") not in expected:
            continue
        name = row["name"]
        if name in found:
            return False
        if (row.get("head_sha") != head or row.get("status") != "completed"
                or row.get("conclusion") != "success"
                or not isinstance(row.get("app"), dict)
                or row["app"].get("slug") != "github-actions"):
            return False
        found[name] = True
    return expected == found.keys()


def _check_reviews(rows: Any, manifest: dict, head: str) -> bool:
    if type(rows) is not list or len(rows) >= 100:
        return False
    latest = {}
    for row in rows:
        if type(row) is not dict or type(row.get("user")) is not dict:
            return False
        login = row["user"].get("login")
        if login in manifest["reviewers"]:
            latest[login] = row
    return any(row.get("commit_id") == head and row.get("state") == "APPROVED"
               for row in latest.values()) and not any(
               row.get("state") == "CHANGES_REQUESTED" for row in latest.values())


def _approval(rows: Any, manifest: dict, head: str, eval_sha: str,
              runtime_login: str) -> bool:
    if type(rows) is not list or len(rows) >= 100:
        return False
    grant = "CAO-MERGE-AUTHORIZE " + head + " " + eval_sha
    revoke = "CAO-MERGE-REVOKE " + head + " " + eval_sha
    decision = None
    for row in rows:
        if (type(row) is dict and isinstance(row.get("user"), dict)
                and row["user"].get("login") in manifest["operators"]
                and row["user"].get("login") != runtime_login):
            if row.get("body") == grant:
                decision = True
            elif row.get("body") == revoke:
                decision = False
    return decision is True


def _tick_unlocked(watch_file: str, *, execute: bool = False,
                   fetch: Callable[[str], Any] | None = None,
                   submit: Callable[[str, int, str], None] | None = None,
                   now: dt.datetime | None = None) -> dict:
    manifest = validate(_file(watch_file))
    clock = now or dt.datetime.now(dt.timezone.utc)
    if clock.tzinfo is None:
        refuse("UTC-aware clock required")
    now_utc = clock.astimezone(dt.timezone.utc)
    snapshot = _file(manifest["evaluation_file"])
    eval_sha = _digest(snapshot)
    baseline, head = _comparison(snapshot, manifest)
    identity = _digest(manifest)
    state = _read_state(manifest["state_file"], identity)
    def checked(payload: dict) -> dict:
        if execute:
            _save_state(manifest["state_file"], {
                "schema": STATE_SCHEMA, "manifest_sha256": identity,
                "head": head, "evaluation_sha256": eval_sha,
                "last_checked": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "phase": "CHECKED",
            })
        return payload

    result = {
        "schema": "cao.engineering.tick.v1",
        "watch_id": manifest["watch_id"], "repo": manifest["repo"],
        "pr": manifest["pr"], "head": head, "baseline": baseline,
        "evaluation_sha256": eval_sha, "status": "HOLD",
        "merge_requested": False, "deployment_authorized": False,
        "operator_approval_required": True,
    }
    if state is not None:
        if state.get("phase") in ("SUBMITTING", "REQUESTED"):
            # No second submission after a crash. Query the accountable
            # GitHub PR to distinguish accepted arm, merged, and uncertainty.
            fetch = fetch or _gh_json
            pr = fetch(f"repos/{manifest['repo']}/pulls/{manifest['pr']}")
            if type(pr) is not dict or pr.get("head", {}).get("sha") != head:
                result["status"] = "AWAITING_GITHUB_RECONCILIATION"
            elif pr.get("state") == "closed" and pr.get("merged") is True:
                result["status"] = "MERGED_SOURCE_ONLY"
            elif pr.get("auto_merge") is not None:
                result["status"] = "AUTO_MERGE_ALREADY_ARMED"
            else:
                result["status"] = "AWAITING_GITHUB_RECONCILIATION"
            return result
        if execute and (now_utc - _utc(state["last_checked"])).total_seconds() < manifest["min_interval_seconds"]:
            result["status"] = "COOLDOWN"
            return result
    if now_utc >= _utc(manifest["expires_at"]):
        result["status"] = "EXPIRED"
        return checked(result)
    if snapshot.get("status") != "SOURCE_REPORTED_IMPROVEMENT":
        result["status"] = "NEEDS_IMPROVEMENT"
        return checked(result)
    fetch = fetch or _gh_json
    submit = submit or _submit_merge
    repo = manifest["repo"]
    pr_id = manifest["pr"]
    base = f"repos/{repo}"
    pr = fetch(f"{base}/pulls/{pr_id}")
    if type(pr) is not dict or pr.get("number") != pr_id:
        refuse("GitHub PR identity unavailable")
    if pr.get("merged") is True and pr.get("state") == "closed":
        result["status"] = "MERGED_SOURCE_ONLY"
        return checked(result)
    if (pr.get("state") != "open" or pr.get("draft") is not False
            or pr.get("base", {}).get("ref") != manifest["base_branch"]
            or pr.get("head", {}).get("sha") != head
            or pr.get("head", {}).get("repo", {}).get("full_name") != repo):
        result["status"] = "PR_SOURCE_MISMATCH"
        return checked(result)
    if pr.get("auto_merge") is not None:
        result["status"] = "AUTO_MERGE_ALREADY_ARMED"
        return checked(result)
    files = fetch(f"{base}/pulls/{pr_id}/files?per_page=100")
    if not _check_paths(pr, files, manifest):
        result["status"] = "SCOPE_MISMATCH"
        return checked(result)
    checks = fetch(f"{base}/commits/{head}/check-runs?per_page=100")
    if not _check_ci(checks, manifest, head):
        result["status"] = "CI_NOT_GREEN_FOR_HEAD"
        return checked(result)
    reviews = fetch(f"{base}/pulls/{pr_id}/reviews?per_page=100")
    author = pr.get("user", {}).get("login") if type(pr.get("user")) is dict else None
    if (type(author) is not str or author in manifest["reviewers"]
            or not _check_reviews(reviews, manifest, head)):
        result["status"] = "INDEPENDENT_REVIEW_REQUIRED"
        return checked(result)
    rules = fetch(f"{base}/branches/{manifest['base_branch']}/protection/required_status_checks")
    if (type(rules) is not dict or rules.get("strict") is not True
            or not set(manifest["required_checks"]) <= set(rules.get("contexts", []))):
        result["status"] = "BRANCH_RULES_NOT_VERIFIED"
        return checked(result)
    me = fetch("user")
    runtime_login = me.get("login") if type(me) is dict else None
    if (type(runtime_login) is not str or runtime_login in manifest["operators"]
            or runtime_login in manifest["reviewers"]):
        result["status"] = "SEPARATE_OPERATOR_IDENTITY_REQUIRED"
        return checked(result)
    comments = fetch(f"{base}/issues/{pr_id}/comments?per_page=100")
    if not _approval(comments, manifest, head, eval_sha, runtime_login):
        result["status"] = "AWAITING_OPERATOR_APPROVAL"
        return checked(result)
    result["operator_approval_required"] = False
    if not manifest["auto_merge"]:
        result["status"] = "APPROVED_PLAN_ONLY"
        return checked(result)
    if not execute:
        result["status"] = "READY_TO_ARM"
        return checked(result)
    # Record the uncertain boundary BEFORE the external side effect. If the
    # process dies, never issue a duplicate request from the checkpoint.
    base_state = {
        "schema": STATE_SCHEMA, "manifest_sha256": identity,
        "head": head, "evaluation_sha256": eval_sha,
        "last_checked": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "phase": "SUBMITTING",
    }
    _save_state(manifest["state_file"], base_state)
    submit(repo, pr_id, head)
    base_state["phase"] = "REQUESTED"
    _save_state(manifest["state_file"], base_state)
    result["status"] = "AUTO_MERGE_REQUESTED"
    result["merge_requested"] = True
    return result


@contextmanager
def _effect_lock(path: str):
    if fcntl is None:
        refuse("POSIX kernel lock unavailable; no automatic external actions")
    root = os.path.dirname(path)
    if not os.path.isdir(root) or os.path.islink(root):
        refuse("engineering lock parent unavailable")
    if os.stat(root, follow_symlinks=False).st_mode & 0o022:
        refuse("engineering lock parent must be operator-private")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path + ".lock", flags, 0o600)
    except OSError as exc:
        raise EngineeringError("engineering lock unavailable") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            refuse("engineering lock is not a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            raise EngineeringError("another engineering tick holds this watch") from exc
        yield
    finally:
        os.close(fd)


def tick(watch_file: str, *, execute: bool = False,
         fetch: Callable[[str], Any] | None = None,
         submit: Callable[[str, int, str], None] | None = None,
         now: dt.datetime | None = None) -> dict:
    if not execute:
        return _tick_unlocked(watch_file, execute=False,
                              fetch=fetch, submit=submit, now=now)
    manifest = validate(_file(watch_file))
    with _effect_lock(manifest["state_file"]):
        return _tick_unlocked(watch_file, execute=True,
                              fetch=fetch, submit=submit, now=now)


def tick_queue(directory: str, *, execute: bool = False,
               fetch: Callable[[str], Any] | None = None,
               submit: Callable[[str, int, str], None] | None = None,
               now: dt.datetime | None = None) -> dict:
    """Reconcile a bounded priority-ordered PR queue, one effect per pass.

    Name watch manifests 001-..., 002-..., etc. All members are validated
    before an external promotion is attempted. A pending approval, CI gate or
    auto-merge request stops later merges from overtaking the first PR.
    """
    if (type(directory) is not str or not os.path.isabs(directory)
            or os.path.normpath(directory) != directory
            or os.path.realpath(directory) != directory
            or not os.path.isdir(directory)):
        refuse("queue requires one real absolute directory")
    entries = sorted(os.listdir(directory))
    if not 1 <= len(entries) <= 8 or any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\.json", x)
            for x in entries):
        refuse("engineering queue requires 1..8 named JSON manifests")
    paths = [os.path.join(directory, x) for x in entries]
    owners = set()
    for file in paths:
        plan = validate(_file(file))
        key = (plan["repo"], plan["pr"])
        if key in owners:
            refuse("duplicate PR in queue")
        owners.add(key)
        _comparison(_file(plan["evaluation_file"]), plan)
        _read_state(plan["state_file"], _digest(plan))
    status = []
    for file in paths:
        row = tick(file, execute=execute, fetch=fetch, submit=submit, now=now)
        status.append({
            "watch_id": row["watch_id"], "repo": row["repo"],
            "pr": row["pr"], "status": row["status"],
            "merge_requested": row["merge_requested"],
        })
        if row["status"] != "MERGED_SOURCE_ONLY":
            return {
                "schema": "cao.engineering.queue.v1", "status": "PENDING",
                "in_flight": row["watch_id"], "items": status,
            }
    return {
        "schema": "cao.engineering.queue.v1", "status": "COMPLETE",
        "in_flight": None, "items": status,
    }
