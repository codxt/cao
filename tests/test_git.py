"""Focused tests for the read-only Git probe core (``cao.git``)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cao import git


def run_git(cwd: os.PathLike | str, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.stdout.decode("utf-8", "replace")


def make_repo(root: Path, name: str = "repo") -> Path:
    """Create a repository with one commit on ``main`` and a ``src/`` tree."""
    path = root / name
    path.mkdir(parents=True)
    run_git(path, "init", "-b", "main")
    run_git(path, "config", "user.email", "test@example.invalid")
    run_git(path, "config", "user.name", "Test")
    run_git(path, "config", "commit.gpgsign", "false")
    (path / "src").mkdir()
    (path / "src" / "app.py").write_text("print('one')\n", encoding="utf-8")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    (path / "outside.txt").write_text("outside\n", encoding="utf-8")
    run_git(path, "add", "-A")
    run_git(path, "commit", "-m", "init")
    return path


class GitIdentityTests(unittest.TestCase):
    def test_common_dir_unifies_linked_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = make_repo(root)
            linked = root / "linked"
            run_git(repo, "worktree", "add", "-b", "feature", str(linked))

            # A checkout and its linked worktree are one repository identity...
            self.assertEqual(git.common_dir(str(repo)), git.common_dir(str(linked)))
            self.assertEqual(git.normalize_repo(str(repo)),
                             git.normalize_repo(str(linked)))
            # ...but two distinct physical checkouts.
            self.assertNotEqual(git.normalize_worktree(str(repo)),
                                git.normalize_worktree(str(linked)))

    def test_non_git_path_falls_back_to_resolved_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "not-a-repo"
            plain.mkdir()
            self.assertEqual(git.common_dir(str(plain)), "")
            self.assertEqual(git.normalize_repo(str(plain)),
                             os.path.realpath(str(plain)))


class WorktreeProbeTests(unittest.TestCase):
    def test_reports_branch_and_head_per_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = make_repo(root)
            linked = root / "linked"
            run_git(repo, "worktree", "add", "-b", "feature", str(linked))

            probe = git.probe_worktrees(str(repo))
            self.assertTrue(probe["ok"])
            by_branch = {t["branch"]: t for t in probe["items"]}
            self.assertIn("main", by_branch)
            self.assertIn("feature", by_branch)
            self.assertTrue(git.HEAD_RE.fullmatch(by_branch["main"]["head"]))

    def test_missing_path_is_unknown_not_empty(self) -> None:
        probe = git.probe_worktrees("/does/not/exist/really")
        self.assertFalse(probe["ok"])
        self.assertIsNone(probe["items"])
        self.assertTrue(probe["error"])


class StatusAndRenameTests(unittest.TestCase):
    def test_status_porcelain_records_both_rename_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            run_git(repo, "mv", "outside.txt", "src/outside.txt")

            probe = git.probe_status(str(repo))
            self.assertTrue(probe["ok"])
            changes = probe["items"]
            rename = next(c for c in changes if c["index"] == "R")
            self.assertEqual(rename["orig_path"], "outside.txt")
            self.assertEqual(rename["path"], "src/outside.txt")
            self.assertEqual(git.touched_paths(changes),
                             ["outside.txt", "src/outside.txt"])

    def test_committed_diff_counts_both_rename_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            run_git(repo, "checkout", "-b", "work")
            run_git(repo, "mv", "outside.txt", "src/outside.txt")
            run_git(repo, "commit", "-m", "move file into src")

            committed = git.probe_committed_diff(str(repo), "main")
            self.assertTrue(committed["ok"])
            self.assertIn("outside.txt", committed["items"])
            self.assertIn("src/outside.txt", committed["items"])


class DirtyDigestTests(unittest.TestCase):
    def test_clean_tree_has_empty_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            digest = git.probe_dirty_digest(str(repo))
            self.assertTrue(digest["ok"])
            self.assertTrue(digest["clean"])
            self.assertEqual(digest["digest"], "")

    def test_digest_binds_to_untracked_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            (repo / "new.txt").write_text("first\n", encoding="utf-8")
            first = git.probe_dirty_digest(str(repo))
            self.assertTrue(first["ok"])
            self.assertFalse(first["clean"])
            self.assertNotEqual(first["digest"], "")

            # A same-size edit of the same path must still change the digest.
            (repo / "new.txt").write_text("other\n", encoding="utf-8")
            second = git.probe_dirty_digest(str(repo))
            self.assertNotEqual(first["digest"], second["digest"])

    def test_missing_worktree_is_unknown(self) -> None:
        probe = git.probe_dirty_digest("/does/not/exist/really")
        self.assertFalse(probe["ok"])


class ReadOnlyGuaranteeTests(unittest.TestCase):
    def test_probes_do_not_rewrite_the_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            # Make a tracked file stat-dirty without changing its content. This
            # is exactly the case in which ``git status`` refreshes the index
            # unless optional locks are disabled.
            target = repo / "src" / "app.py"
            future = time.time() + 60
            os.utime(target, (future, future))

            index = repo / ".git" / "index"
            before = (index.stat().st_mtime_ns, index.read_bytes())

            git.probe_status(str(repo))
            git.probe_dirty_digest(str(repo))
            git.probe_worktrees(str(repo))

            after = (index.stat().st_mtime_ns, index.read_bytes())
            self.assertEqual(before, after)


class BaselineTests(unittest.TestCase):
    def test_baseline_prefers_local_main_and_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            probe = git.probe_baseline_ref(str(repo))
            self.assertTrue(probe["ok"])
            self.assertEqual(probe["items"], ["main"])
            warnings = git.baseline_warnings(str(repo), "main")
            self.assertTrue(any("local branch" in w for w in warnings))

    def test_no_baseline_is_a_real_empty_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain"
            plain.mkdir()
            probe = git.probe_baseline_ref(str(plain))
            self.assertTrue(probe["ok"])
            self.assertEqual(probe["items"], [])
            self.assertTrue(git.baseline_warnings(str(plain), ""))

    def test_invalid_baseline_ref_is_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            with mock.patch.object(
                    git, "_run_git",
                    side_effect=[(0, "/absolute/file\n")]):
                probe = git.probe_baseline_ref(str(repo))
            self.assertFalse(probe["ok"])
            self.assertEqual(probe["reason"], git.PROBE_MALFORMED)
            self.assertIsNone(probe["items"])


class TextconvTests(unittest.TestCase):
    def test_dirty_digest_disables_textconv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = make_repo(root)
            # A textconv driver that would replace every *.py blob with a
            # constant AND leave a marker file if Git ever executes it. With
            # textconv active, a real content change to a *.py file would be
            # filtered to the same constant on both sides and the diff would
            # look empty, hiding the change.
            marker = root / "textconv-ran"
            script = root / "conv.sh"
            script.write_text(
                "#!/bin/sh\n"
                "touch %r\n"
                "echo CONSTANT\n" % str(marker),
                encoding="utf-8")
            script.chmod(0o755)
            run_git(repo, "config", "diff.myconv.textconv", str(script))
            (repo / ".gitattributes").write_text("*.py diff=myconv\n",
                                                 encoding="utf-8")
            run_git(repo, "add", ".gitattributes")
            run_git(repo, "commit", "-m", "attributes")

            clean = git.probe_dirty_digest(str(repo))
            self.assertTrue(clean["ok"])
            self.assertEqual(clean["digest"], "")

            (repo / "src" / "app.py").write_text("print('changed')\n",
                                                 encoding="utf-8")
            if marker.exists():
                marker.unlink()
            probe = git.probe_dirty_digest(str(repo))
            self.assertTrue(probe["ok"])
            self.assertFalse(probe["clean"])
            self.assertNotEqual(probe["digest"], "")
            self.assertNotEqual(probe["digest"], clean["digest"])
            self.assertFalse(marker.exists(),
                             "textconv must not execute while hashing")

            # Stage the change and exercise the --cached command on its own:
            # the cached path must carry --no-textconv too, or a staged-only
            # change would be filtered away there.
            run_git(repo, "add", "src/app.py")
            if marker.exists():
                marker.unlink()
            staged = git.probe_dirty_digest(str(repo))
            self.assertTrue(staged["ok"])
            self.assertFalse(staged["clean"])
            self.assertNotEqual(staged["digest"], probe["digest"])
            self.assertFalse(marker.exists(),
                             "textconv must not execute on the cached diff")

            # Prove the cached command is what needed the flag: without
            # --no-textconv it executes the converter; with it, it does not.
            # This runs real git directly (the private runner only permits the
            # safe, --no-textconv form).
            marker.unlink(missing_ok=True)
            run_git(str(repo), "diff", "--no-color", "--no-ext-diff",
                    "--cached")
            self.assertTrue(marker.exists())
            marker.unlink(missing_ok=True)
            run_git(str(repo), "diff", "--no-color", "--no-ext-diff",
                    "--no-textconv", "--cached")
            self.assertFalse(marker.exists())


class MalformedOutputTests(unittest.TestCase):
    def test_status_parser_rejects_unterminated_and_bad_records(self) -> None:
        cases = [
            "M",                          # unterminated, too short
            "?? a",                       # unterminated
            "ZZ bogus\0",                 # invalid status token
            "?M b\0",                     # invalid status pair
            "MMfoo\0",                    # bad field separator
            "M\0",                        # too short
            "?? \0",                      # empty path
            "R  new.txt\0",               # rename missing its source
            "R  new.txt\0\0M  other\0",   # empty source then next record
            "?? ok\0ZZ bad\0?? ok2\0",    # malformed record between valid ones
            "M\0../outside.py\0",         # path outside the repository
        ]
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(git.MalformedOutput):
                    git.parse_status_porcelain(text)

    def test_status_parser_rejects_invalid_pairs(self) -> None:
        # Made of plausible characters but outside the porcelain-v1 status
        # table. " A" is deliberately NOT here: a blank-index added entry is
        # valid and is covered by the positive test below.
        invalid = ("UM", "XX", "?M", "M?", "!M", " U", "?A",
                   "DM", "DT", "DR", "MR", "AC")
        for pair in invalid:
            with self.subTest(pair=pair):
                with self.assertRaises(git.MalformedOutput):
                    git.parse_status_porcelain("%s path\0" % pair)
        # The normal-column rejects must also fail BETWEEN valid records.
        for pair in ("DM", "DT", "DR", "MR", "AC"):
            with self.subTest(pair=pair, between=True):
                with self.assertRaises(git.MalformedOutput):
                    git.parse_status_porcelain("?? ok\0%s bad\0?? ok2\0" % pair)

    def test_status_parser_accepts_every_valid_pair(self) -> None:
        # pair -> extra NUL fields after the path (rename/copy source).
        cases = [
            ("  ", []),
            ("M ", []), (" M", []), ("MM", []), ("MT", []), ("MD", []),
            ("T ", []), (" T", []), ("TT", []), ("TM", []), ("TD", []),
            ("A ", []), (" A", []), ("AA", []), ("AM", []), ("AT", []),
            ("AD", []),
            ("D ", []), (" D", []),
            ("R ", ["old"]), (" R", ["old"]), ("RR", ["old"]),
            ("RM", ["old"]), ("RT", ["old"]), ("RD", ["old"]),
            ("C ", ["old"]), (" C", ["old"]), ("CC", ["old"]),
            ("CM", ["old"]), ("CT", ["old"]), ("CD", ["old"]),
            ("DD", []), ("DU", []), ("UD", []), ("AU", []),
            ("UA", []), ("UU", []),
            ("??", []), ("!!", []),
        ]
        for pair, extra in cases:
            with self.subTest(pair=pair):
                text = "\0".join([pair + " p"] + extra) + "\0"
                records = git.parse_status_porcelain(text)
                self.assertEqual(len(records), 1)
                self.assertEqual(
                    records[0]["index"] + records[0]["worktree"], pair)
                if pair[0] in ("R", "C") or pair[1] in ("R", "C"):
                    self.assertEqual(records[0]["orig_path"], "old")

    def test_status_parser_accepts_valid_pairs(self) -> None:
        records = git.parse_status_porcelain(
            "?? new.txt\0 M src/app.py\0M  staged\0R  new\0old\0"
            "UU conflicted\0AU added\0 A blank-index-added\0")
        self.assertEqual(
            [r["path"] for r in records],
            ["new.txt", "src/app.py", "staged", "new", "conflicted", "added",
             "blank-index-added"])
        self.assertEqual(records[3]["orig_path"], "old")
        self.assertEqual(records[4]["index"] + records[4]["worktree"], "UU")
        self.assertEqual(records[5]["index"] + records[5]["worktree"], "AU")
        self.assertEqual(records[6]["index"] + records[6]["worktree"], " A")

    def test_name_status_parser_rejects_unterminated_and_bad_tokens(self) -> None:
        cases = [
            "Q\0path\0",                # invalid change code
            "Mgarbage\0path\0",         # token with trailing garbage + a path
            "Rgarbage\0old\0new\0",     # rename score is not numeric
            "R999\0old\0new\0",         # rename score out of range
            "R\u0661\u0660\u0660\0old\0new\0",  # Arabic-Indic digits are not ASCII
            "R\u00b2\0old\0new\0",      # superscript two is not a decimal digit
            "R" + "0" * 5000 + "\0old\0new\0",  # oversized score
            "R100\0only-src\0",         # rename missing its destination
            "R100\0\0dst\0",            # empty rename source endpoint
            "M\0\0",                    # empty path
            "M\0path",                  # unterminated
            "M\0../outside.py\0",       # path outside the repository
            "M\0a\0Rgarbage\0b\0c\0",   # malformed between valid records
            "M\0a\0R\u0661\u0660\u0660\0b\0c\0",  # non-ASCII between records
            "M\0a\0R" + "0" * 5000 + "\0b\0c\0",   # oversized between records
        ]
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(git.MalformedOutput):
                    git.parse_committed_name_status(text)

    def test_name_status_parser_accepts_valid_records(self) -> None:
        self.assertEqual(git.parse_committed_name_status("M\0src/app.py\0"),
                         ["src/app.py"])
        self.assertEqual(git.parse_committed_name_status("R100\0old\0new\0"),
                         ["new", "old"])
        self.assertEqual(git.parse_committed_name_status("A\0x\0D\0y\0"),
                         ["x", "y"])

    def test_probe_status_reports_malformed_not_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            with mock.patch.object(git, "_run_git",
                                   return_value=(0, "?? ok\0UM bad\0?? ok2\0")):
                probe = git.probe_status(str(repo))
            self.assertFalse(probe["ok"])
            self.assertEqual(probe["reason"], git.PROBE_MALFORMED)
            self.assertIsNone(probe["items"])

    @staticmethod
    def _record(path, head, branch=None, detached=False, extra=()):
        """One completely framed worktree record (fields + blank separator)."""
        parts = ["worktree " + str(path), "HEAD " + head]
        if branch is not None:
            parts.append("branch " + branch)
        if detached:
            parts.append("detached")
        parts.extend(extra)
        return "\0".join(parts) + "\0\0"

    def _worktree_probe(self, repo: Path, text: str) -> dict:
        with mock.patch.object(git, "_run_git", return_value=(0, text)):
            return git.probe_worktrees(str(repo))

    def test_probe_worktrees_rejects_bad_framing_and_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            sha = "a" * 40
            good = self._record(repo, sha, "refs/heads/main")
            cases = {
                "orphan prefix":
                    "HEAD %s\0" % sha + good,
                "unterminated":
                    "worktree %s\0HEAD %s\0branch refs/heads/main" % (repo, sha),
                "final record without separator":
                    "worktree %s\0HEAD %s\0branch refs/heads/main\0"
                    % (repo, sha),
                "bad head":
                    self._record(repo, "notahash", "refs/heads/main"),
                "empty head then valid head":
                    "worktree %s\0HEAD \0HEAD %s\0branch refs/heads/main\0\0"
                    % (repo, sha),
                "relative worktree path":
                    self._record("relative/path", sha, "refs/heads/main"),
                "branch garbage":
                    self._record(repo, sha, "garbage"),
                "bare refs/heads/":
                    self._record(repo, sha, "refs/heads/"),
                "ref with a space":
                    self._record(repo, sha, "refs/heads/my branch"),
                "ref with dotdot":
                    self._record(repo, sha, "refs/heads/main..other"),
                "ref with a double slash":
                    self._record(repo, sha, "refs/heads//main"),
                "ref with a leading space":
                    self._record(repo, sha, " refs/heads/main"),
                "ref with a trailing space":
                    self._record(repo, sha, "refs/heads/main "),
                "lockedgarbage":
                    self._record(repo, sha, "refs/heads/main",
                                 extra=["lockedgarbage"]),
                "prunablegarbage":
                    self._record(repo, sha, "refs/heads/main",
                                 extra=["prunablegarbage"]),
                "missing branch or detached":
                    "worktree %s\0HEAD %s\0\0" % (repo, sha),
                "both branch and detached":
                    self._record(repo, sha, "refs/heads/main", detached=True),
                "bare with a branch":
                    self._record(repo, sha, "refs/heads/main", extra=["bare"]),
                "duplicate identity":
                    good + good,
                "unrecognized field":
                    self._record(repo, sha, "refs/heads/main",
                                 extra=["weird"]),
                "unterminated record before next":
                    "worktree %s\0HEAD %s\0branch refs/heads/main\0" % (repo, sha)
                    + self._record(repo / "second", sha, "refs/heads/other"),
            }
            for label, text in cases.items():
                with self.subTest(label=label):
                    probe = self._worktree_probe(repo, text)
                    self.assertFalse(probe["ok"])
                    self.assertEqual(probe["reason"], git.PROBE_MALFORMED)
                    self.assertIsNone(probe["items"])

    def test_probe_worktrees_rejects_bad_record_between_valid_records(self) -> None:
        # Three DISTINCT worktree identities, so only the malformed middle
        # record can cause the failure: duplicate-identity rejection cannot
        # make this pass for the wrong reason.
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            sha = "a" * 40
            first = self._record(repo / "one", sha, "refs/heads/main")
            third = self._record(repo / "three", sha, "refs/heads/main")

            # Prove the surrounding records are genuinely valid.
            valid_middle = self._record(repo / "two", sha, "refs/heads/feature")
            ok = self._worktree_probe(repo, first + valid_middle + third)
            self.assertTrue(ok["ok"], ok)
            self.assertEqual(len(ok["items"]), 3)

            for label, bad in (
                    ("bad branch",
                     self._record(repo / "two", sha, "garbage")),
                    ("bare ref",
                     self._record(repo / "two", sha, "refs/heads/")),
                    ("space in ref",
                     self._record(repo / "two", sha, "refs/heads/my branch")),
                    ("dotdot ref",
                     self._record(repo / "two", sha, "refs/heads/main..other")),
                    ("leading-space ref",
                     self._record(repo / "two", sha, " refs/heads/main")),
                    ("trailing-space ref",
                     self._record(repo / "two", sha, "refs/heads/main ")),
                    ("bad head",
                     self._record(repo / "two", "notahash", "refs/heads/main")),
            ):
                with self.subTest(label=label):
                    probe = self._worktree_probe(repo, first + bad + third)
                    self.assertFalse(probe["ok"])
                    self.assertEqual(probe["reason"], git.PROBE_MALFORMED)
                    self.assertIsNone(probe["items"])

    def test_probe_worktrees_accepts_valid_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            sha = "a" * 40
            branch = self._record(repo, sha, "refs/heads/main")
            detached = self._record(repo / "linked", sha, detached=True)
            probe = self._worktree_probe(repo, branch + detached)
            self.assertTrue(probe["ok"])
            self.assertEqual(len(probe["items"]), 2)
            self.assertEqual(probe["items"][0]["branch"], "main")
            self.assertTrue(probe["items"][1]["detached"])

    def test_probe_committed_diff_reports_malformed(self) -> None:
        good_base = "a" * 40 + "\n"
        payloads = [
            "Rgarbage\0a\0b\0",
            "R\u0661\u0660\u0660\0old\0new\0",   # non-ASCII digits
            "R\u00b2\0old\0new\0",                # superscript two
            "R" + "0" * 5000 + "\0old\0new\0",    # oversized score
            # Invalid scores BETWEEN valid name-status records, so the parser
            # actually reaches the bad token instead of failing on framing.
            "M\0first.txt\0Rgarbage\0old\0new\0M\0last.txt\0",
            "M\0first.txt\0R\u0661\u0660\u0660\0old\0new\0M\0last.txt\0",
            "M\0first.txt\0R" + "0" * 5000 + "\0old\0new\0M\0last.txt\0",
        ]
        for payload in payloads:
            with self.subTest(payload=payload[:16]):
                with tempfile.TemporaryDirectory() as tmp:
                    repo = make_repo(Path(tmp))
                    with mock.patch.object(
                            git, "_run_git",
                            side_effect=[(0, good_base), (0, payload)]):
                        probe = git.probe_committed_diff(str(repo), "main")
                    self.assertFalse(probe["ok"])
                    self.assertEqual(probe["reason"], git.PROBE_MALFORMED)
                    self.assertIsNone(probe["items"])


class ProbeNeverRaisesTests(unittest.TestCase):
    def test_probes_never_raise_on_unexpected_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            calls = [
                ("probe_status", lambda: git.probe_status(str(repo))),
                ("probe_worktrees", lambda: git.probe_worktrees(str(repo))),
                ("probe_committed_diff",
                 lambda: git.probe_committed_diff(str(repo), "main")),
                ("probe_dirty_digest",
                 lambda: git.probe_dirty_digest(str(repo))),
                ("probe_baseline_ref",
                 lambda: git.probe_baseline_ref(str(repo))),
            ]
            for name, call in calls:
                with self.subTest(probe=name):
                    with mock.patch.object(git, "_run_git",
                                           side_effect=RuntimeError("injected")):
                        probe = call()
                    self.assertFalse(probe["ok"])
                    self.assertEqual(probe["reason"], git.PROBE_FAILED)
                    self.assertIsNone(probe["items"])

    def test_run_git_common_dir_and_normalize_never_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            with mock.patch.object(git.subprocess, "run",
                                   side_effect=RuntimeError("injected")):
                self.assertEqual(git._run_git(["status"], str(repo)), (None, ""))
                self.assertEqual(git.common_dir(str(repo)), "")
                self.assertEqual(git.normalize_repo(str(repo)),
                                 os.path.realpath(str(repo)))


class StructuralGuardTests(unittest.TestCase):
    def test_status_rejects_ambiguous_rename_endpoint(self) -> None:
        # A truncated rename would otherwise consume the NEXT record as its
        # source and return a shorter healthy set.
        with self.assertRaises(git.MalformedOutput):
            git.parse_status_porcelain(
                "?? first\0R  new\0?? second\0?? third\0")
        with self.assertRaises(git.MalformedOutput):
            git.parse_status_porcelain(
                "?? ok\0R  new\0?? second\0?? third\0")

    def test_name_status_rejects_ambiguous_endpoint(self) -> None:
        with self.assertRaises(git.MalformedOutput):
            git.parse_committed_name_status("R100\0M\0path\0")
        with self.assertRaises(git.MalformedOutput):
            git.parse_committed_name_status("M\0a\0R100\0M\0b\0")

    def test_dirty_digest_rejects_bad_ls_files_paths_without_reading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            for payload in ("/etc/passwd\0", "../outside\0", "ok\0missing-nul"):
                with self.subTest(payload=payload):
                    with mock.patch.object(
                            git, "_run_git",
                            side_effect=[(0, ""), (0, ""), (0, payload)]):
                        with mock.patch.object(os, "lstat",
                                               wraps=os.lstat) as spy:
                            probe = git.probe_dirty_digest(str(repo))
                    self.assertFalse(probe["ok"])
                    self.assertEqual(probe["reason"], git.PROBE_MALFORMED)
                    self.assertIsNone(probe["items"])
                    for call in spy.call_args_list:
                        self.assertNotIn("passwd", str(call))
                        self.assertNotIn("outside", str(call))

    def test_committed_diff_rejects_bad_baseline_and_merge_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            # An invalid supplied baseline ref is malformed before any git call.
            probe = git.probe_committed_diff(str(repo), "/absolute/file")
            self.assertFalse(probe["ok"])
            self.assertEqual(probe["reason"], git.PROBE_MALFORMED)
            self.assertIsNone(probe["items"])
            # Arbitrary merge-base output is not a usable base.
            for merge_out in ("/absolute/file\n", "not-a-commit\n"):
                with self.subTest(merge_out=merge_out):
                    with mock.patch.object(git, "_run_git",
                                           side_effect=[(0, merge_out)]):
                        probe = git.probe_committed_diff(str(repo), "main")
                    self.assertFalse(probe["ok"])
                    self.assertEqual(probe["reason"], git.PROBE_MALFORMED)
                    self.assertIsNone(probe["items"])

    def test_committed_diff_disables_diff_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            with mock.patch.object(
                    git, "_run_git",
                    side_effect=[(0, "a" * 40 + "\n"), (0, "")]) as mocked:
                git.probe_committed_diff(str(repo), "main")
            diff_args = mocked.call_args_list[1].args[0]
            self.assertIn("--no-ext-diff", diff_args)
            self.assertIn("--no-textconv", diff_args)

    def test_probe_errors_never_echo_input(self) -> None:
        secret = "SECRET-PAYLOAD-VALUE"
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            # A malformed record carrying a secret must not surface in the error.
            with mock.patch.object(git, "_run_git",
                                   return_value=(0, "%s\0" % secret)):
                probe = git.probe_status(str(repo))
            self.assertFalse(probe["ok"])
            self.assertNotIn(secret, str(probe))
            # An unexpected exception's text must not surface either.
            with mock.patch.object(git, "_run_git",
                                   side_effect=RuntimeError(secret)):
                probe = git.probe_worktrees(str(repo))
            self.assertFalse(probe["ok"])
            self.assertNotIn(secret, str(probe))


    def test_short_ref_rejects_option_like_and_malformed_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            for bad in ("origin/main.lock", "origin/.hidden", "-foo",
                        "refs/heads/-foo"):
                with self.subTest(ref=bad):
                    probe = git.probe_committed_diff(str(repo), bad)
                    self.assertFalse(probe["ok"])
                    self.assertEqual(probe["reason"], git.PROBE_MALFORMED)
                    self.assertIsNone(probe["items"])
            # ... and via the symbolic-ref baseline probe.
            for out in ("origin/main.lock\n", "origin/.hidden\n", "-foo\n"):
                with self.subTest(out=out):
                    with mock.patch.object(git, "_run_git",
                                           side_effect=[(0, out)]):
                        probe = git.probe_baseline_ref(str(repo))
                    self.assertFalse(probe["ok"])
                    self.assertEqual(probe["reason"], git.PROBE_MALFORMED)
                    self.assertIsNone(probe["items"])

    def test_worktree_branch_ref_rejects_dash_lock_and_dot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            sha = "a" * 40
            for ref in ("refs/heads/-foo", "refs/heads/main.lock",
                        "refs/heads/.hidden"):
                with self.subTest(ref=ref):
                    text = "worktree %s\0HEAD %s\0branch %s\0\0" % (repo, sha,
                                                                   ref)
                    with mock.patch.object(git, "_run_git",
                                           return_value=(0, text)):
                        probe = git.probe_worktrees(str(repo))
                    self.assertFalse(probe["ok"])
                    self.assertEqual(probe["reason"], git.PROBE_MALFORMED)

    def test_multiline_output_is_not_trimmed_into_validity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            # merge-base with an extra newline
            with mock.patch.object(git, "_run_git",
                                   side_effect=[(0, "a" * 40 + "\n\n")]):
                probe = git.probe_committed_diff(str(repo), "main")
            self.assertFalse(probe["ok"])
            self.assertEqual(probe["reason"], git.PROBE_MALFORMED)
            # symbolic-ref with an extra newline
            with mock.patch.object(git, "_run_git",
                                   side_effect=[(0, "origin/main\n\n")]):
                probe = git.probe_baseline_ref(str(repo))
            self.assertFalse(probe["ok"])
            self.assertEqual(probe["reason"], git.PROBE_MALFORMED)
            # common-dir output is exactly one absolute path or fail-closed ""
            for out in ("/tmp\n\n", "/tmp\njunk\n", "relative\n", "/tmp"):
                with self.subTest(out=out):
                    with mock.patch.object(git, "_run_git",
                                           return_value=(0, out)):
                        self.assertEqual(git.common_dir(str(repo)), "")

    def test_invalid_utf8_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            fake = mock.Mock(returncode=0, stdout=b"M  bad\xff\0")
            with mock.patch.object(git.subprocess, "run", return_value=fake):
                self.assertEqual(git._run_git(["status"], str(repo)), (None, ""))
            with mock.patch.object(git.subprocess, "run", return_value=fake):
                probe = git.probe_status(str(repo))
            self.assertFalse(probe["ok"])
            self.assertEqual(probe["reason"], git.PROBE_FAILED)
            self.assertIsNone(probe["items"])


    def test_baseline_probe_distinguishes_execution_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            # A runner failure is UNAVAILABLE, not "no baseline".
            with mock.patch.object(git.subprocess, "run",
                                   side_effect=RuntimeError("injected")):
                probe = git.probe_baseline_ref(str(repo))
            self.assertFalse(probe["ok"])
            self.assertEqual(probe["reason"], git.PROBE_FAILED)
            self.assertIsNone(probe["items"])
            # ... as is an undecodable response.
            fake = mock.Mock(returncode=0, stdout=b"origin/main\xff\0")
            with mock.patch.object(git.subprocess, "run", return_value=fake):
                probe = git.probe_baseline_ref(str(repo))
            self.assertFalse(probe["ok"])
            self.assertEqual(probe["reason"], git.PROBE_FAILED)
            self.assertIsNone(probe["items"])

    def test_parse_error_traceback_has_no_raw_secret(self) -> None:
        secret = "SECRET-PAYLOAD-VALUE"
        cases = [
            (git.parse_status_porcelain, "?? ok\0%s\0" % secret),
            (git.parse_worktree_porcelain, "worktree %s\0\0" % secret),
            (git.parse_committed_name_status, "%s\0" % secret),
        ]
        for fn, text in cases:
            with self.subTest(fn=fn.__name__):
                try:
                    fn(text)
                except git.MalformedOutput as exc:
                    frames = []
                    tb = exc.__traceback__
                    while tb is not None:
                        # Only library frames matter; the caller's own frame
                        # legitimately holds the argument it passed in.
                        if tb.tb_frame.f_code.co_filename.rsplit("/", 1)[-1] == "git.py":
                            frames.append(repr(list(
                                tb.tb_frame.f_locals.items())))
                        tb = tb.tb_next
                    reachable = "%r %r %r %s" % (
                        exc, exc.__cause__, exc.__context__, " ".join(frames))
                    self.assertIsNone(exc.__cause__)
                    self.assertIsNone(exc.__context__)
                    self.assertNotIn(secret, reachable)
                else:
                    self.fail("expected MalformedOutput")


    def test_parsers_reject_duplicate_and_equal_records(self) -> None:
        for text in ("R  same\0same\0", "?? a\0?? a\0", "M  a//b\0",
                     "M  a\x00b\0"):
            with self.subTest(text=text):
                with self.assertRaises(git.MalformedOutput):
                    git.parse_status_porcelain(text)
        for text in ("R100\0same\0same\0", "M\0a\0M\0a\0", "M\0a//b\0"):
            with self.subTest(text=text):
                with self.assertRaises(git.MalformedOutput):
                    git.parse_committed_name_status(text)

    def test_baseline_warnings_do_not_interpolate_ref(self) -> None:
        secret = "SYNTH_SECRET"
        for baseline in ("", secret, "origin/" + secret):
            with self.subTest(baseline=baseline):
                self.assertNotIn(
                    secret,
                    " ".join(git.baseline_warnings("/tmp", baseline)))


    def test_public_helpers_do_not_propagate_hostile_str(self) -> None:
        secret = "SYNTH_SECRET"

        class BoomStr(str):
            def strip(self):
                raise RuntimeError(secret)

            def split(self, *args, **kwargs):
                raise RuntimeError(secret)

            def __getitem__(self, index):
                raise RuntimeError(secret)

            def __iter__(self):
                raise RuntimeError(secret)

        value = BoomStr("x")
        for fn in (git.valid_commit_id, git.valid_short_ref, git.valid_abs_path,
                   git.valid_relpath, git.valid_digest):
            with self.subTest(fn=fn.__name__):
                self.assertFalse(fn(value))
        self.assertFalse(git.valid_status_pair(value, value))
        self.assertEqual(git.normalize_repo(value), "")
        self.assertEqual(git.normalize_worktree(value), "")
        self.assertEqual(git.common_dir(value), "")
        for fn, text in (
                (git.parse_status_porcelain, BoomStr("?? a\0")),
                (git.parse_worktree_porcelain, BoomStr("worktree /a\0\0")),
                (git.parse_committed_name_status, BoomStr("M\0a\0"))):
            with self.subTest(fn=fn.__name__):
                try:
                    fn(text)
                except git.MalformedOutput as exc:
                    frames = []
                    tb = exc.__traceback__
                    while tb is not None:
                        if tb.tb_frame.f_code.co_filename.rsplit(
                                "/", 1)[-1] == "git.py":
                            frames.append(repr(list(
                                tb.tb_frame.f_locals.items())))
                        tb = tb.tb_next
                    self.assertNotIn(secret, " ".join(frames))
                else:
                    self.fail("expected MalformedOutput")


    def test_run_git_is_private_and_allowlisted(self) -> None:
        secret = "SYNTH_SECRET"
        self.assertNotIn("run_git", git.__all__)
        self.assertFalse(hasattr(git, "run_git"))
        # No caller-supplied injection reaches subprocess.
        for args in (["status", "-c", "evil=1"],
                     ["-c", "evil=1", "status"],
                     ["status", "--upload-pack=" + secret],
                     ["fetch", "--upload-pack=" + secret],
                     ["status", "--config", "evil"]):
            with self.subTest(args=args):
                with mock.patch.object(git.subprocess, "run") as mocked:
                    result = git._run_git(args, "/tmp")
                self.assertEqual(result, (None, ""))
                mocked.assert_not_called()
        # A hostile argument list fails closed instead of raising.
        class BoomList(list):
            def __iter__(self):
                raise RuntimeError(secret)

        with mock.patch.object(git.subprocess, "run") as mocked:
            result = git._run_git(BoomList(["status"]), "/tmp")
        self.assertEqual(result, (None, ""))
        mocked.assert_not_called()
        # A legitimate allowlisted command still runs.
        fake = mock.Mock(returncode=0, stdout=b"ok")
        with mock.patch.object(git.subprocess, "run", return_value=fake):
            self.assertEqual(
                git._run_git(["status", "--porcelain=v1", "-z",
                              "--untracked-files=all"], "/tmp"),
                (0, "ok"))

    def test_baseline_warnings_rejects_hostile_str(self) -> None:
        secret = "SYNTH_SECRET"

        class BoomStr(str):
            def startswith(self, *args, **kwargs):
                raise RuntimeError(secret)

            def __bool__(self):
                raise RuntimeError(secret)

        warnings = git.baseline_warnings("/tmp", BoomStr(secret))
        self.assertTrue(warnings)
        self.assertNotIn(secret, " ".join(warnings))


    def test_run_git_rejects_unallowlisted_arguments(self) -> None:
        secret = "SYNTH_SECRET"
        hostile = [
            ["status", "--evil"],
            ["status", "--evil=1"],
            ["status", "-x"],
            ["status", "--upload-pack=" + secret],
            ["-c", "evil=1", "status"],
            ["diff", "--ext-diff"],
            ["diff", "--textconv"],
            ["diff", "--no-color", "--no-ext-diff", "--textconv", "HEAD"],
            ["diff", "--no-color", "--no-ext-diff", "--no-textconv",
             "--ext-diff", "HEAD"],
            ["status", "--porcelain=v1", "-z", "--untracked-files=all",
             "--evil"],
            ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "HEAD",
             "--evil"],
            ["diff", "--name-status", "-z", "--find-renames", "--no-ext-diff",
             "--no-textconv", "a" * 40, "HEAD", "--evil"],
            ["merge-base", "main", "HEAD", "--evil"],
            ["merge-base", "--evil", "HEAD"],
            ["merge-base", "main", "HEAD2"],
            ["merge-base", "bad ref", "HEAD"],
            ["ls-files", "--others", "--exclude-standard", "-z", "--evil"],
            ["rev-parse", "--verify", "--quiet", "main", "--evil"],
            ["rev-parse", "--evil"],
            ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD",
             "--evil"],
            ["worktree", "list", "--porcelain", "-z", "--evil"],
            ["diff", "--name-status", "-z", "--find-renames", "--no-ext-diff",
             "--no-textconv", "not-a-commit", "HEAD"],
        ]
        for args in hostile:
            with self.subTest(args=args):
                with mock.patch.object(git.subprocess, "run") as mocked:
                    result = git._run_git(args, "/tmp")
                self.assertEqual(result, (None, ""))
                mocked.assert_not_called()

    def test_normalize_repo_rejects_hostile_str(self) -> None:
        secret = "SYNTH_SECRET"

        class BoomStr(str):
            def strip(self):
                raise ValueError(secret)

        self.assertEqual(git.normalize_repo(BoomStr(secret)), "")
        with self.assertRaises(ValueError):
            git.normalize_repo("")

    def test_short_branch_and_touched_paths_are_total(self) -> None:
        secret = "SYNTH_SECRET"

        class BoomStr(str):
            def startswith(self, *args, **kwargs):
                raise RuntimeError(secret)

            def __getitem__(self, index):
                raise RuntimeError(secret)

        self.assertEqual(git.short_branch(BoomStr(secret)), "")

        class BoomList(list):
            def __iter__(self):
                raise RuntimeError(secret)

        self.assertEqual(git.touched_paths(BoomList([{"path": secret}])), [])

        class BoomDict(dict):
            def get(self, *args, **kwargs):
                raise RuntimeError(secret)

        self.assertEqual(git.touched_paths([BoomDict()]), [])
        self.assertEqual(git.short_branch("refs/heads/main"), "main")
        self.assertEqual(
            git.touched_paths([{"path": "a", "orig_path": "b"}]), ["a", "b"])


    def test_run_git_rejects_str_subclass_arguments(self) -> None:
        class Sneaky(str):
            pass

        legitimate = [
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            ["worktree", "list", "--porcelain", "-z"],
            ["ls-files", "--others", "--exclude-standard", "-z"],
            ["merge-base", "main", "HEAD"],
            ["rev-parse", "--verify", "--quiet", "main"],
            ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        ]
        for args in legitimate:
            with self.subTest(legit=args):
                fake = mock.Mock(returncode=0, stdout=b"ok")
                with mock.patch.object(git.subprocess, "run",
                                       return_value=fake) as mocked:
                    self.assertEqual(git._run_git(args, "/tmp"), (0, "ok"))
                self.assertTrue(mocked.called)
        hostile = [
            [Sneaky("status"), "--porcelain=v1", "-z",
             "--untracked-files=all"],
            [Sneaky("diff"), "--no-color", "--no-ext-diff", "--no-textconv",
             "HEAD"],
            [Sneaky("worktree"), "list", "--porcelain", "-z"],
            ["merge-base", Sneaky("main"), "HEAD"],
            ["rev-parse", "--verify", "--quiet", Sneaky("main")],
            ["status", Sneaky("--porcelain=v1"), "-z",
             "--untracked-files=all"],
        ]
        for args in hostile:
            with self.subTest(hostile=[type(a).__name__ for a in args]):
                with mock.patch.object(git.subprocess, "run") as mocked:
                    result = git._run_git(args, "/tmp")
                self.assertEqual(result, (None, ""))
                mocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
