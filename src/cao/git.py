"""Read-only Git probes for CAO.

Every function here *observes* a repository; none of them writes. Git is
invoked with optional locks disabled and index auto-refresh off, so merely
asking what state a repository is in never leaves it byte-different.

Probes return a plain mapping and never raise. A probe that could not be read
returns ``{"ok": False, ...}``: that is ``UNKNOWN``, and it is deliberately
never collapsed into an empty ``{"ok": True, "items": []}``. The difference
between "nothing is there" and "I could not look" is what keeps a later
authority decision from failing open.

Only identifiers, paths, refs, status words and one-way content digests leave
this module. No file content is ever returned, so a probe result can be
persisted without carrying a secret.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess

__all__ = [
    "GIT",
    "PROBE_FAILED",
    "PROBE_MALFORMED",
    "HEAD_RE",
    "MalformedOutput",
    "common_dir",
    "normalize_repo",
    "normalize_worktree",
    "probe_worktrees",
    "parse_worktree_porcelain",
    "short_branch",
    "probe_status",
    "parse_status_porcelain",
    "touched_paths",
    "probe_committed_diff",
    "parse_committed_name_status",
    "probe_dirty_digest",
    "probe_baseline_ref",
    "baseline_warnings",
    "valid_commit_id",
    "valid_short_ref",
    "valid_abs_path",
    "valid_relpath",
    "valid_status_pair",
    "valid_digest",
    "valid_worktree_state",
    "status_flags",
]

GIT = "git"
PROBE_FAILED = "probe_failed"
PROBE_MALFORMED = "probe_malformed"

# An exact Git commit id. Terminal-head lineage evidence is compared by string
# equality against an observed HEAD, so anything looser than 40 hex characters
# would make "some commit on that branch" indistinguishable from "that commit".
HEAD_RE = re.compile(r"[0-9a-f]{40}")

# The complete ``git status --porcelain=v1`` XY table, restored explicitly.
# The columns are not independent: a state may appear in the index column, the
# worktree column, or both, and ``R``/``C`` (rename/copy) may appear in EITHER
# column and then carry two paths. ``T`` (typechange) may appear in either
# column. Deliberately absent (and rejected): ``UM``, ``XX``, ``DM``, ``DT``,
# ``DR``, ``MR`` and ``AC``.
NORMAL_STATUS_PAIRS = frozenset({
    "  ",
    "M ", " M", "MM", "MT", "MD",
    "T ", " T", "TT", "TM", "TD",
    "A ", " A", "AA", "AM", "AT", "AD",
    "D ", " D",
    "R ", " R", "RR", "RM", "RT", "RD",
    "C ", " C", "CC", "CM", "CT", "CD",
    "??", "!!",
})

# The enumerated unmerged pairs. These are the only pairs that use ``U`` and the
# only ones where ``A``/``D`` may appear in the worktree column alongside them.
UNMERGED_STATUS_PAIRS = frozenset({"DD", "DU", "UD", "AU", "UA", "AD", "AA",
                                   "UU"})

# Characters that are never valid inside a Git ref, plus a control-character
# rule applied separately.
REF_FORBIDDEN = frozenset(" ~^:?*[\\")

# Single-character ``--name-status`` codes. ``R``/``C`` are handled separately
# because they carry a numeric similarity score.
SIMPLE_NAME_STATUS = frozenset("MADTUXB")


class MalformedOutput(ValueError):
    """Raised when Git output does not have the shape the parser understands.

    A partially readable document must not be silently reduced to the records
    that happened to parse: the caller turns this into ``PROBE_MALFORMED`` so
    the missing part is reported as unknown rather than as absent.

    Messages are fixed diagnostics. Raw Git output is never echoed into one,
    because a malformed record can contain anything, including a secret.
    """


def _invalid(what: str) -> None:
    """The single raise point for a malformed field, with a fixed message."""
    raise MalformedOutput(what)


def _raise_sanitized_text(message: str) -> None:
    """Raise a fixed malformed-output error with nothing else attached."""
    raise MalformedOutput(message)


def _sanitized(fn):
    """Boundary wrapper: a raised parse error carries no raw input.

    Any exception type is caught (a hostile ``str`` subclass can raise something
    other than ``MalformedOutput``), the public function's raw parameter and the
    original exception are cleared before the sanitized re-raise, and the inner
    parser frame is left only on the now-detached original traceback. A
    non-malformed exception gets a fixed generic message rather than its own
    ``str`` (which could echo a secret).
    """
    def wrapper(text):
        error = None
        try:
            return fn(text)
        except Exception as exc:  # noqa: BLE001 - a boundary never propagates
            error = exc
        del text
        message = (str(error) if isinstance(error, MalformedOutput)
                   else "git output is malformed")
        error = None
        _raise_sanitized_text(message)
    return wrapper


# --------------------------------------------------------------------------
# Choke point 1: field validation.
#
# Every Git record field is validated through one of these helpers. Each takes a
# fixed label (never the value) and raises a fixed, non-echoing diagnostic, so
# no call site can leak raw output into a public error field.
# --------------------------------------------------------------------------

def _require_no_edge_ws(value: str, what: str) -> str:
    if value != value.strip():
        _invalid("%s has leading or trailing whitespace" % what)
    return value


def _require_abs_path(path: str, what: str) -> str:
    if not path:
        _invalid("%s is empty" % what)
    _require_no_edge_ws(path, what)
    if "\x00" in path:
        _invalid("%s contains a NUL byte" % what)
    if not os.path.isabs(path):
        _invalid("%s is not absolute" % what)
    if os.path.normpath(path) != path:
        _invalid("%s is not canonical" % what)
    return path


def _require_relpath(path: str, what: str) -> str:
    """Reject a path that is not a canonical repository-relative path."""
    if not path:
        _invalid("%s is empty" % what)
    _require_no_edge_ws(path, what)
    if "\x00" in path:
        _invalid("%s contains a NUL byte" % what)
    p = path.replace("\\", "/")
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        _invalid("%s is not repository-relative" % what)
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        _invalid("%s escapes the repository" % what)
    if not parts:
        _invalid("%s resolves to the repository root" % what)
    if "/".join(parts) != p:
        _invalid("%s is not canonical" % what)
    return path


def _require_commit_id(value: str, what: str) -> str:
    if not HEAD_RE.fullmatch(value or ""):
        _invalid("%s is not a commit id" % what)
    return value


def _require_ref_components(ref: str, what: str) -> str:
    """Validate every slash-separated ref component.

    One choke point for the component rules: no empty component, no leading
    dot, no trailing ``.lock``, and no leading dash. A leading dash is an
    option-injection risk because the value is handed to git as an argument, so
    it is rejected here rather than at each call site.
    """
    for component in ref.split("/"):
        if not component or component.startswith(".") \
                or component.endswith(".lock") or component.startswith("-"):
            _invalid("%s is not a valid ref" % what)
    return ref


def _require_ref(ref: str, what: str) -> str:
    """Validate complete Git ref syntax, not just the ``refs/`` prefix."""
    if not ref.startswith("refs/") or ref.endswith("/") or ref.endswith("."):
        _invalid("%s is not a valid ref" % what)
    if ".." in ref or "@{" in ref or "//" in ref:
        _invalid("%s is not a valid ref" % what)
    for ch in ref:
        if ch in REF_FORBIDDEN or ord(ch) < 0x20 or ord(ch) == 0x7f:
            _invalid("%s is not a valid ref" % what)
    _require_ref_components(ref, what)
    return ref


def _require_short_ref(name: str, what: str) -> str:
    """Validate a short ref name such as ``main`` or ``origin/main``."""
    if not name:
        _invalid("%s is empty" % what)
    _require_no_edge_ws(name, what)
    if name.startswith("/") or name.endswith("/") or name.endswith(".") \
            or name.startswith(".") or ".." in name or "@{" in name \
            or "//" in name:
        _invalid("%s is not a valid ref name" % what)
    for ch in name:
        if ch in REF_FORBIDDEN or ord(ch) < 0x20 or ord(ch) == 0x7f:
            _invalid("%s is not a valid ref name" % what)
    _require_ref_components(name, what)
    return name


def _one_line(out: str, what: str) -> str:
    """Extract exactly one newline-terminated line, or refuse the output.

    Framing is validated BEFORE the single expected newline is removed: output
    with no terminator, an embedded newline, or an extra trailing newline is
    malformed, so arbitrary raw output cannot be trimmed into a valid value.
    """
    if not out.endswith("\n"):
        _invalid("%s is not newline-terminated" % what)
    line = out[:-1]
    if "\n" in line or "\r" in line:
        _invalid("%s has more than one line" % what)
    return line


# A dirty-digest value: the truncated SHA-256 hex emitted by probe_dirty_digest.
DIGEST_RE = re.compile(r"[0-9a-f]{16}")


def valid_commit_id(value) -> bool:
    """Whether ``value`` is exactly a 40-hex commit id. Never raises."""
    try:
        return type(value) is str and bool(HEAD_RE.fullmatch(value))
    except Exception:  # noqa: BLE001 - a hostile argument
        return False


def valid_short_ref(value) -> bool:
    """Whether ``value`` is a valid short ref name. Never raises."""
    try:
        if type(value) is not str or not value:
            return False
        _require_short_ref(value, "ref")
        return True
    except Exception:  # noqa: BLE001 - a hostile argument
        return False


def valid_abs_path(value) -> bool:
    """Whether ``value`` is an absolute, canonical, NUL-free path."""
    try:
        if type(value) is not str:
            return False
        _require_abs_path(value, "path")
        return ".." not in value.replace("\\", "/").split("/")
    except Exception:  # noqa: BLE001 - a hostile argument
        return False


def valid_relpath(value) -> bool:
    """Whether ``value`` is a canonical repo-relative path. Never raises."""
    try:
        if type(value) is not str:
            return False
        _require_relpath(value, "path")
        return True
    except Exception:  # noqa: BLE001 - a hostile argument
        return False


def valid_status_pair(index, worktree) -> bool:
    """Whether ``XY`` is a real porcelain-v1 status pair. Never raises."""
    try:
        if type(index) is not str or type(worktree) is not str:
            return False
        _validate_status_pair(index, worktree)
        return True
    except Exception:  # noqa: BLE001 - a hostile argument
        return False


def valid_digest(value) -> bool:
    """Whether ``value`` is a dirty-digest hex value. Never raises."""
    try:
        return type(value) is str and bool(DIGEST_RE.fullmatch(value))
    except Exception:  # noqa: BLE001 - a hostile argument
        return False


def _validate_status_pair(index: str, worktree: str) -> None:
    """Validate an ``XY`` porcelain-v1 status pair against the real table."""
    pair = index + worktree
    if pair in NORMAL_STATUS_PAIRS or pair in UNMERGED_STATUS_PAIRS:
        return
    _invalid("git status record has an invalid status pair")


def _name_status_code(record: str) -> str:
    """Validate a complete ``--name-status`` token and return its change code.

    A single-character code must be exactly that character; a rename/copy token
    must be the letter plus a bounded ASCII decimal similarity score in
    ``0..100``. ``str.isdigit`` and ``int`` are deliberately not used directly:
    they accept non-ASCII digits and can raise outside the malformed-output path
    for huge or non-decimal strings.
    """
    if len(record) == 1 and record in SIMPLE_NAME_STATUS:
        return record
    if len(record) >= 2 and record[0] in ("R", "C"):
        score = record[1:]
        if 1 <= len(score) <= 3 and all(ch in "0123456789" for ch in score) \
                and 0 <= int(score) <= 100:
            return record[0]
    _invalid("git name-status record has an invalid token")


def _looks_like_status_record(field: str) -> bool:
    """Whether a field could itself be a status record rather than a path.

    Used only to reject an ambiguous rename/copy endpoint: consuming the next
    record as an endpoint is how a truncated document read as a shorter healthy
    change set.
    """
    if len(field) < 3 or field[2] != " ":
        return False
    pair = field[:2]
    return pair in NORMAL_STATUS_PAIRS or pair in UNMERGED_STATUS_PAIRS


def _looks_like_name_status_token(field: str) -> bool:
    """Whether a field could itself be a name-status token rather than a path."""
    try:
        _name_status_code(field)
    except MalformedOutput:
        return False
    return True


def _split_worktree_records(text: str) -> list[str]:
    """Split ``worktree list --porcelain -z`` into fields, keeping separators.

    Unlike status/name-status, worktree records are separated by an EMPTY
    field (the blank line of the line-oriented format), so an interior empty
    field is meaningful framing here rather than a hole. The document is still
    required to be NUL-terminated, so an unterminated reading is refused.
    """
    if text == "":
        return []
    parts = text.split("\0")
    if parts[-1] != "":
        _invalid("git worktree output is not NUL-terminated")
    return parts[:-1]


def _split_nul_records(text: str) -> list[str]:
    """Split a NUL-terminated ``-z`` document, preserving empty fields.

    Git terminates every field with NUL, including the last. Empty fields are
    NOT filtered out before grammar validation: an empty field is either an
    interior hole (a rename source that is missing, so the next record would be
    consumed as its source) or a missing terminator, and both are malformed.
    """
    if text == "":
        return []
    parts = text.split("\0")
    if parts[-1] != "":
        _invalid("git output is not NUL-terminated")
    fields = parts[:-1]
    if any(field == "" for field in fields):
        _invalid("git output has an empty field between records")
    return fields


# --------------------------------------------------------------------------
# Choke point 2: structured evidence construction.
#
# One place builds every probe failure, always with a fixed, non-echoing detail.
# --------------------------------------------------------------------------

def _unknown(reason: str, detail: str) -> dict:
    """A probe that could not be read. Never equivalent to an empty result."""
    return {"ok": False, "source": GIT, "reason": reason, "error": detail,
            "items": None}


def _malformed(what: str) -> dict:
    return _unknown(PROBE_MALFORMED, "%s is malformed" % what)


def _failed(what: str) -> dict:
    return _unknown(PROBE_FAILED, "%s could not be read" % what)


def _guarded(name: str):
    """Choke point 3: turn any unexpected error into structured evidence.

    The probe layer's contract is that it never raises: a caller deciding
    authority must receive ``ok=False`` and ``items=None``, never an exception.
    Expected malformed output is already converted to ``PROBE_MALFORMED`` inside
    each probe; this converts everything else to ``PROBE_FAILED`` with a fixed
    diagnostic that never echoes the exception.
    """
    def decorate(fn):
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception:  # noqa: BLE001 - convert, never propagate
                return _failed(name)
        return wrapper
    return decorate


# Fixed git subcommand allowlist for the private runner. Every call site in
# COMPLETE per-command argv allowlists for the private runner. Every command
# this module builds is either one of these exact tuples, or one of the dynamic
# shapes validated by ``_allowed_git_argv`` with its operand checked. There is
# no prefix denylist: an argument not explicitly allowed cannot reach git.
_STATIC_GIT_ARGV = frozenset({
    ("rev-parse", "--path-format=absolute", "--git-common-dir"),
    ("worktree", "list", "--porcelain", "-z"),
    ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
    ("diff", "--no-color", "--no-ext-diff", "--no-textconv", "HEAD"),
    ("diff", "--no-color", "--no-ext-diff", "--no-textconv", "--cached"),
    ("ls-files", "--others", "--exclude-standard", "-z"),
    ("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"),
})
_BASELINE_CANDIDATES = ("origin/main", "origin/master", "main", "master")


def _allowed_git_argv(args) -> bool:
    """Whether ``args`` is EXACTLY a permitted git command.

    Every argument must be an exact built-in ``str`` (checked BEFORE tuple
    conversion and allowlist matching, so a ``str`` subclass is never compared
    against or placed into an allowed argv), and the command must be an exact
    static tuple or a dynamic shape whose operand passes its own validator.
    Anything else — an extra flag, a single-dash or double-dash option, an
    ``=``-joined option, or a smuggled tail — is rejected.
    """
    if type(args) is not list and type(args) is not tuple:
        return False
    for argument in args:
        if type(argument) is not str:
            return False
    argv = tuple(args)
    if argv in _STATIC_GIT_ARGV:
        return True
    if len(argv) == 3 and argv[0] == "merge-base" and argv[2] == "HEAD":
        return valid_short_ref(argv[1])
    if len(argv) == 8 and argv[:6] == (
            "diff", "--name-status", "-z", "--find-renames", "--no-ext-diff",
            "--no-textconv") and argv[7] == "HEAD":
        return valid_commit_id(argv[6])
    if len(argv) == 4 and argv[:3] == ("rev-parse", "--verify", "--quiet"):
        return argv[3] in _BASELINE_CANDIDATES
    return False


def _run_git(args, cwd: str | None = None) -> tuple[int | None, str]:
    """Run one ALLOWLISTED git observation. Private; never raises.

    This is deliberately NOT a public entry point and is not exported. ``args``
    must match a COMPLETE per-command allowlist (see ``_allowed_git_argv``): no
    caller-supplied argument that is not explicitly permitted can reach git.
    ``argv`` is constructed and validated INSIDE the guard, so even evaluating a
    hostile argument list cannot escape.

    ``rc is None`` means the process could not be executed or its bytes could
    not be decoded: that is an UNAVAILABLE reading, distinct from git's ordinary
    nonzero exit (a ref that simply does not exist).

    ``GIT_OPTIONAL_LOCKS=0`` and ``diff.autoRefreshIndex=false`` together stop
    both ``git status`` and ``git diff`` from rewriting ``.git/index`` while
    they are only being read.
    """
    try:
        if not _allowed_git_argv(args):
            return None, ""
        prefix = ["git", "--no-optional-locks",
                  "-c", "diff.autoRefreshIndex=false"]
        argv = prefix + list(args)
        env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except Exception:  # noqa: BLE001 - a probe never propagates
        return None, ""
    # Strict decoding: invalid UTF-8 is unreadable evidence, never a lossy
    # replacement-character reading that could look like a valid path.
    try:
        return proc.returncode, proc.stdout.decode("utf-8")
    except Exception:  # noqa: BLE001 - undecodable or malformed process result
        return None, ""


def common_dir(path: str) -> str:
    """The repository identity shared by a checkout and all linked worktrees.

    Resolves through ``git rev-parse --git-common-dir``, so a checkout and a
    linked worktree of one repository are one repository. Returns ``""`` when
    the path is not inside a Git repository or git is unavailable, so each
    caller can decide whether that is fatal for it. Output is required to be
    exactly one absolute path line; anything else is fail-closed ``""``. A
    hostile path argument cannot make it raise.
    """
    try:
        real = os.path.realpath(os.path.expanduser(path))
        rc, out = _run_git(
            ["rev-parse", "--path-format=absolute", "--git-common-dir"], real)
        if rc != 0:
            return ""
        common = _one_line(out, "git common dir")
        _require_abs_path(common, "git common dir")
        return os.path.realpath(common)
    except Exception:  # noqa: BLE001 - fail-closed identity
        return ""


def normalize_repo(path: str) -> str:
    """Canonical repository identity, insensitive to symlinks and trailing ``/``.

    A Git checkout is identified by its common dir, so linked worktrees of one
    repository share one identity and cannot each acquire an independent writer
    on overlapping scope. A path outside Git falls back to its resolved path.

    Deliberately uncached: a cached negative would freeze "not a repository"
    for a directory about to be ``git init``-ed, and a cached positive would
    outlive the ``.git`` it described. Never raises for an unexpected or hostile
    input: the resolved path, or "" when even that fails, is the fail-closed
    identity.
    """
    try:
        if type(path) is not str:
            # Reject a non-exact string BEFORE calling any method on it.
            return ""
        if not path or not path.strip():
            raise ValueError("empty repository path")
        real = os.path.realpath(os.path.expanduser(path.strip()))
        return common_dir(real) or real
    except ValueError:
        raise
    except Exception:  # noqa: BLE001 - fail-closed identity
        return ""


def normalize_worktree(path: str | None) -> str:
    """Canonical checkout identity. Empty means "not declared".

    A worktree is a physical checkout, so this stays the resolved path and is
    deliberately *not* collapsed to the common dir: two different checkouts of
    one repository are two different places to write. Never raises for a hostile
    input.
    """
    try:
        if path is None or not path.strip():
            return ""
        return os.path.realpath(os.path.expanduser(path.strip()))
    except Exception:  # noqa: BLE001 - fail-closed identity
        return ""


@_sanitized
def parse_worktree_porcelain(text: str) -> list[dict]:
    """Parse ``git worktree list --porcelain -z`` into worktree records.

    Record framing is enforced. A field before the first ``worktree`` line, an
    unrecognized field, a malformed flag label, a relative worktree path, a
    non-ref branch, a duplicate ``HEAD``/``branch`` line, an unterminated record
    or a final record without its terminating separator raises
    :class:`MalformedOutput` instead of being silently discarded, because a
    dropped field is how an unterminated or orphan-prefixed document read as a
    shorter valid one.

    ``HEAD``/``branch`` presence is tracked independently of their value, so an
    empty ``HEAD`` field cannot be masked by a following valid ``HEAD``.
    """
    fields = _split_worktree_records(text)
    out: list[dict] = []
    current: dict | None = None
    for raw in fields:
        if raw == "":
            # A blank field closes the current worktree record.
            if current is None:
                _invalid("git worktree record separator before any record")
            current = None
            continue
        if raw.startswith("worktree "):
            if current is not None:
                _invalid("git worktree record is not terminated before the next")
            path = _require_abs_path(raw[len("worktree "):], "worktree path")
            current = {
                "path": path,
                "head": None,
                "branch": None,
                "detached": False,
                "bare": False,
                "prunable": False,
                "locked": False,
            }
            out.append(current)
        elif current is None:
            _invalid("git worktree field is orphaned before any record")
        elif raw.startswith("HEAD "):
            if current["head"] is not None:
                _invalid("git worktree record has a duplicate HEAD field")
            # Preserved raw, then validated: trimming would accept edge
            # whitespace that Git would not have emitted.
            head = _require_no_edge_ws(raw[len("HEAD "):], "worktree HEAD")
            current["head"] = _require_commit_id(head, "worktree HEAD")
        elif raw.startswith("branch "):
            if current["branch"] is not None:
                _invalid("git worktree record has a duplicate branch field")
            current["branch"] = _require_ref(
                raw[len("branch "):], "worktree branch")
        elif raw == "detached":
            current["detached"] = True
        elif raw == "bare":
            current["bare"] = True
        elif raw == "prunable" or raw.startswith("prunable "):
            current["prunable"] = True
        elif raw == "locked" or raw.startswith("locked "):
            current["locked"] = True
        else:
            _invalid("git worktree record has an unrecognized field")
    if current is not None:
        # Every record, including the last, ends with its blank separator.
        _invalid("git worktree final record is not terminated")
    return out


def short_branch(ref: str) -> str:
    """``refs/heads/task/x`` -> ``task/x``. Non-branch refs are unchanged.

    Fail-closed and total: a non-exact-string or hostile input returns "".
    """
    try:
        if type(ref) is not str:
            return ""
        for prefix in ("refs/heads/", "refs/remotes/"):
            if ref.startswith(prefix):
                return ref[len(prefix):]
        return ref
    except Exception:  # noqa: BLE001 - a public boundary never propagates
        return ""


@_guarded("probe_worktrees")
def probe_worktrees(repo: str) -> dict:
    """Every worktree of ``repo``, with its actual branch and HEAD."""
    real = os.path.realpath(os.path.expanduser(repo))
    if not os.path.isdir(real):
        return _failed("repository path")
    rc, out = _run_git(["worktree", "list", "--porcelain", "-z"], real)
    if rc != 0:
        return _failed("git worktree list")
    try:
        trees = parse_worktree_porcelain(out)
    except MalformedOutput:
        return _malformed("git worktree list")
    if not trees:
        # Parsed successfully but yielded nothing: a real answer only if git
        # said so. An empty parse of a non-empty repository is malformed.
        return _malformed("git worktree list")
    seen: set[str] = set()
    for tree in trees:
        if not tree.get("path"):
            return _malformed("git worktree list")
        head = tree.get("head")
        if not head or not HEAD_RE.fullmatch(head):
            # A worktree whose HEAD is missing or not a commit id cannot be
            # placed, so the whole reading is malformed rather than partially
            # trustworthy.
            return _malformed("git worktree HEAD")
        # Presence, not truthiness: an empty ``branch`` field is still a
        # declared branch, and an empty ``HEAD`` was already rejected above.
        # The attached/detached/bare state is judged by the ONE shared helper
        # the Registry validator also calls, so the two cannot disagree.
        if not valid_worktree_state(tree.get("branch") or "",
                                    tree.get("detached", False),
                                    tree.get("bare", False)):
            return _malformed("git worktree state")
        identity = os.path.realpath(tree["path"])
        if identity in seen:
            return _malformed("git worktree identity")
        seen.add(identity)
    for tree in trees:
        tree["branch"] = short_branch(tree["branch"]) if tree["branch"] else ""
        tree["path"] = os.path.realpath(tree["path"])
    return {"ok": True, "source": GIT, "reason": "", "error": "", "items": trees}


@_sanitized
def parse_status_porcelain(text: str) -> list[dict]:
    """Parse ``git status --porcelain=v1 -z`` into per-path change records.

    A rename or copy is one entry followed by a second NUL-separated field
    holding the ORIGINAL path. Both endpoints are recorded: scope is declared
    on repo-relative paths, and a file renamed out of a claimed path has
    escaped it even when the destination alone looks untouched.

    Validation is fail-closed. A record that is too short, carries a status
    token Git cannot emit, has no path, or is a rename whose original path is
    missing raises :class:`MalformedOutput`. Skipping such a record would turn
    a partially readable status into a shorter, still-"healthy" change set, so
    the caller reports ``PROBE_MALFORMED`` instead.
    """
    fields = _split_nul_records(text)
    out: list[dict] = []
    seen: set = set()
    i = 0
    while i < len(fields):
        entry = fields[i]
        if len(entry) < 3:
            _invalid("git status record is too short")
        index, worktree, separator, path = (
            entry[0], entry[1], entry[2], entry[3:])
        if separator != " ":
            _invalid("git status record has a bad field separator")
        _validate_status_pair(index, worktree)
        _require_relpath(path, "status path")
        staged, unstaged, untracked = status_flags(index, worktree)
        record = {
            "index": index,
            "worktree": worktree,
            "path": path,
            "orig_path": "",
            "staged": staged,
            "unstaged": unstaged,
            "untracked": untracked,
        }
        i += 1
        if index in ("R", "C") or worktree in ("R", "C"):
            # R/C in EITHER column marks a rename/copy and consumes two paths.
            if i >= len(fields):
                _invalid("git status rename/copy is missing its original path")
            orig = fields[i]
            if _looks_like_status_record(orig):
                # The "original path" is itself a status record: a truncated
                # rename would otherwise consume the NEXT record as its source.
                _invalid("git status rename/copy endpoint is ambiguous")
            _require_relpath(orig, "rename/copy original path")
            if orig == path:
                _invalid("git status rename/copy has equal endpoints")
            record["orig_path"] = orig
            i += 1
        key = (index, worktree, path, record["orig_path"])
        if key in seen:
            _invalid("git status record is duplicated")
        seen.add(key)
        out.append(record)
    return out


def touched_paths(changes: list[dict]) -> list[str]:
    """Every path a change set touches, counting BOTH rename endpoints.

    Fail-closed and total: only an exact list of exact dicts with exact-string
    paths contributes, and a hostile input returns ``[]`` rather than raising.
    """
    try:
        if type(changes) is not list:
            return []
        out: set[str] = set()
        for change in changes:
            if type(change) is not dict:
                continue
            path = change.get("path")
            if type(path) is str and path:
                out.add(path)
            orig = change.get("orig_path")
            if type(orig) is str and orig:
                out.add(orig)
        return sorted(out)
    except Exception:  # noqa: BLE001 - a public boundary never propagates
        return []


@_guarded("probe_status")
def probe_status(worktree: str) -> dict:
    """Actual staged/unstaged/untracked/renamed paths in one checkout."""
    real = os.path.realpath(os.path.expanduser(worktree))
    if not os.path.isdir(real):
        return _failed("worktree path")
    rc, out = _run_git(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"], real)
    if rc != 0:
        return _failed("git status")
    try:
        changes = parse_status_porcelain(out)
    except MalformedOutput:
        return _malformed("git status")
    return {"ok": True, "source": GIT, "reason": "", "error": "",
            "worktree": real, "items": changes}


@_sanitized
def parse_committed_name_status(text: str) -> list[str]:
    """Paths from ``git diff --name-status -z``, counting BOTH rename endpoints.

    A committed rename has two endpoints and containment needs both: a file
    renamed out of a claimed path escaped that path, and the destination alone
    does not show it. ``--name-only`` reports only the destination, which is
    how a rename out of scope became invisible.

    Validation is fail-closed, mirroring :func:`parse_status_porcelain`: a
    record with an unknown change code, or a rename/copy missing either
    endpoint, raises :class:`MalformedOutput` so the caller reports
    ``PROBE_MALFORMED`` rather than a subset that reads as contained.
    """
    fields = _split_nul_records(text)
    out: set[str] = set()
    seen: set[str] = set()
    i = 0
    while i < len(fields):
        record = fields[i]
        i += 1
        if not record:
            _invalid("git name-status record is empty")
        code = _name_status_code(record)
        if code in ("R", "C"):
            if i + 1 >= len(fields):
                _invalid("git name-status rename/copy is missing an endpoint")
            src, dst = fields[i], fields[i + 1]
            i += 2
            if _looks_like_name_status_token(src) \
                    or _looks_like_name_status_token(dst):
                # An endpoint that is itself a change token means the record is
                # truncated and the next record was consumed as an endpoint.
                _invalid("git name-status rename/copy endpoint is ambiguous")
            _require_relpath(src, "rename/copy source path")
            _require_relpath(dst, "rename/copy destination path")
            if src == dst:
                _invalid("git name-status rename/copy has equal endpoints")
            if src in seen or dst in seen:
                _invalid("git name-status record is duplicated")
            seen.update((src, dst))
            out.update((src, dst))
            continue
        if i >= len(fields):
            _invalid("git name-status record is missing its path")
        path = fields[i]
        i += 1
        _require_relpath(path, "name-status path")
        if path in seen:
            _invalid("git name-status record is duplicated")
        seen.add(path)
        out.add(path)
    return sorted(out)


@_guarded("probe_committed_diff")
def probe_committed_diff(worktree: str, base_ref: str) -> dict:
    """Paths this worktree's HEAD commits relative to a baseline.

    A clean worktree can still hold work that was already committed. Without
    this, registered-but-uncommitted work looks identical to an idle branch.

    ``--name-status`` rather than ``--name-only``: rename detection reports only
    the destination in a name-only diff, so a committed rename out of a claimed
    path left that path looking untouched. Both endpoints are recorded here, so
    containment is decided on the whole move.
    """
    real = os.path.realpath(os.path.expanduser(worktree))
    if not base_ref:
        return _failed("baseline ref")
    try:
        _require_short_ref(base_ref, "baseline ref")
    except MalformedOutput:
        return _malformed("baseline ref")
    rc, merge_base = _run_git(["merge-base", base_ref, "HEAD"], real)
    if rc != 0:
        return _failed("git merge-base")
    # No usable merge base means committed scope cannot be established. Falling
    # back to ``diff HEAD`` here would present unknown committed scope as clean:
    # the fail-open this layer exists to prevent. The output must be an exact
    # commit id; anything else is malformed, not a usable base.
    try:
        base = _one_line(merge_base, "git merge-base output")
        _require_commit_id(base, "git merge-base output")
    except MalformedOutput:
        return _malformed("git merge-base")
    rc, out = _run_git(
        ["diff", "--name-status", "-z", "--find-renames", "--no-ext-diff",
         "--no-textconv", base, "HEAD"], real)
    if rc != 0:
        return _failed("git diff")
    try:
        paths = parse_committed_name_status(out)
    except MalformedOutput:
        return _malformed("git diff")
    return {"ok": True, "source": GIT, "reason": "", "error": "",
            "worktree": real, "base": base, "items": paths}


@_guarded("probe_dirty_digest")
def probe_dirty_digest(worktree: str) -> dict:
    """A short, content-bound digest of a checkout's uncommitted state.

    A path list cannot distinguish "file touched once" from "the same file
    edited again", so a decision made against a first edit would survive a
    second, different edit of the same path. Untracked files are streamed in
    because git produces no diff for them.

    Three rules for untracked entries:

    * a symlink contributes its TARGET TEXT, never the bytes it points at, so
      hashing cannot walk out of the worktree;
    * anything that is neither a regular file nor a symlink is recorded by type
      alone, because it has no defined byte stream;
    * a file that cannot be stat-ed or read is UNKNOWN, never a placeholder
      size with ``ok`` true, which would report an unverified reading as a
      measured one.

    Only the truncated hex digest leaves this layer; no content is returned.
    """
    real = os.path.realpath(os.path.expanduser(worktree))
    if not os.path.isdir(real):
        return _failed("worktree path")
    hasher = hashlib.sha256()
    empty = True
    for args in (["diff", "--no-color", "--no-ext-diff", "--no-textconv",
                  "HEAD"],
                 ["diff", "--no-color", "--no-ext-diff", "--no-textconv",
                  "--cached"]):
        rc, out = _run_git(args, real)
        if rc != 0:
            return _failed("git diff")
        if out.strip():
            empty = False
        hasher.update(out.encode("utf-8", "replace"))
    rc, out = _run_git(["ls-files", "--others", "--exclude-standard", "-z"], real)
    if rc != 0:
        return _failed("git ls-files")
    try:
        untracked = _split_nul_records(out)
    except MalformedOutput:
        return _malformed("git ls-files")
    for path in sorted(untracked):
        # Validate BEFORE any filesystem access: an absolute or escaping path
        # from a malformed reading must never reach open()/lstat().
        try:
            _require_relpath(path, "ls-files path")
        except MalformedOutput:
            return _malformed("git ls-files")
        full = os.path.join(real, path)
        try:
            mode = os.lstat(full).st_mode
        except OSError:
            return _failed("untracked file")
        empty = False
        kind = mode & 0o170000
        if kind == 0o120000:
            try:
                target = os.readlink(full)
            except OSError:
                return _failed("untracked symlink")
            hasher.update(b"link\x00%s\x00%s" % (
                path.encode("utf-8", "replace"),
                target.encode("utf-8", "replace")))
            continue
        if kind != 0o100000:
            hasher.update(b"special\x00%s\x00%o" % (
                path.encode("utf-8", "replace"), kind))
            continue
        hasher.update(b"file\x00%s\x00" % path.encode("utf-8", "replace"))
        content = hashlib.sha256()
        try:
            with open(full, "rb") as handle:
                while True:
                    block = handle.read(65536)
                    if not block:
                        break
                    content.update(block)
        except OSError:
            # An unreadable untracked file is UNKNOWN, never a placeholder.
            return _failed("untracked file")
        hasher.update(content.hexdigest().encode("ascii"))
    return {"ok": True, "source": GIT, "reason": "", "error": "",
            "worktree": real, "clean": empty,
            "digest": "" if empty else hasher.hexdigest()[:16]}


@_guarded("probe_baseline_ref")
def probe_baseline_ref(repo: str) -> dict:
    """The usable default baseline for this repository, as structured evidence.

    A remote default branch is the shared truth; a local ``main`` may be stale,
    so it is used only as a last resort and reported as such by
    :func:`baseline_warnings`. ``ok=True, items=[]`` means the probe ran and
    there is genuinely no usable baseline (unknown committed scope). An
    unexpected failure is ``ok=False`` with ``items=None``. The symbolic-ref
    output is validated as a ref name before it is used, so arbitrary raw Git
    output cannot be accepted as a baseline.
    """
    real = os.path.realpath(os.path.expanduser(repo))
    rc, out = _run_git(
        ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        real)
    if rc is None:
        return _failed("git baseline ref")
    if rc == 0:
        try:
            name = _one_line(out, "baseline ref")
            _require_short_ref(name, "baseline ref")
        except MalformedOutput:
            return _malformed("baseline ref")
        return {"ok": True, "source": GIT, "reason": "", "error": "",
                "items": [name]}
    for candidate in ("origin/main", "origin/master", "main", "master"):
        rc, _ = _run_git(["rev-parse", "--verify", "--quiet", candidate], real)
        if rc is None:
            return _failed("git baseline ref")
        if rc == 0:
            return {"ok": True, "source": GIT, "reason": "", "error": "",
                    "items": [candidate]}
    return {"ok": True, "source": GIT, "reason": "", "error": "", "items": []}


def baseline_warnings(repo: str, baseline: str) -> list[str]:
    """Fixed diagnostic categories about a baseline that may not be shared truth.

    Deliberately does not interpolate the ref: a baseline value is caller- or
    git-derived, and a public warning must not echo it. The categories are
    fixed strings. An exact built-in string is required, so a hostile string
    subclass cannot raise here.
    """
    if type(baseline) is not str or baseline == "":
        return ["no usable baseline branch: committed scope is unknown"]
    if baseline.startswith("origin/"):
        return []
    return ["baseline is a local branch and may be stale; committed scope is "
            "measured against it anyway"]


def valid_worktree_state(branch, detached, bare) -> bool:
    """Whether an attached/detached/bare worktree identity is self-consistent.

    THE one definition, matching what the worktree probe accepts: a bare record
    is not a usable checkout; a non-bare record is attached (non-empty branch,
    not detached) or detached (empty branch, detached). Both the probe and the
    Registry validator call this, so they cannot disagree.
    """
    if type(branch) is not str or type(detached) is not bool \
            or type(bare) is not bool:
        return False
    if bare:
        return False
    return (branch == "") == detached


def status_flags(index, worktree):
    """``(staged, unstaged, untracked)`` derived from a status pair.

    THE one derivation, used by the status parser and the Registry validator so
    the flags cannot drift apart. The caller-supplied flags are never trusted.
    Returns ``None`` for a non-string pair (a hostile argument), so a caller
    treats it as invalid rather than raising.
    """
    if type(index) is not str or type(worktree) is not str:
        return None
    return (index not in (" ", "?"), worktree not in (" ", "?"),
            index == "?" and worktree == "?")
