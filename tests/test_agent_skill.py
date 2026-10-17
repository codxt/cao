"""Focused guards for the portable CAO operator agent skill.

The skill is public instruction text, so it must actually exist, declare
itself, keep the two CLI surfaces distinct, and carry the fail-closed
guardrails an operator depends on. The scan is text-only and synthetic: it
reads repository files and never contacts a runtime, a host or the network.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SKILL_ROOT = REPO_ROOT / "skills" / "cao-operator"
SKILL_MD = SKILL_ROOT / "SKILL.md"
OPENAI_YAML = SKILL_ROOT / "agents" / "openai.yaml"
COMMANDS_REF = SKILL_ROOT / "references" / "commands.md"
RECOVERY_REF = SKILL_ROOT / "references" / "recovery.md"
GUIDE = REPO_ROOT / "docs" / "agent-skill.md"

REQUIRED_FILES = (SKILL_MD, OPENAI_YAML, COMMANDS_REF, RECOVERY_REF, GUIDE)

EXPECTED_SKILL_FILES = {
    "SKILL.md",
    "agents/openai.yaml",
    "references/commands.md",
    "references/recovery.md",
}

#: Host traces and private portfolio labels that must never appear in a public
#: artifact. The skill ships as OSS documentation, so it carries none of them.
PRIVATE_TOKENS = (
    "albr",
    "sentinelx",
    "resident",
    "brain",
    "tolest",
    "companyos",
    "/home/",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class AgentSkillTests(unittest.TestCase):
    def test_required_files_exist(self) -> None:
        missing = [str(p.relative_to(REPO_ROOT)) for p in REQUIRED_FILES if not p.is_file()]
        self.assertEqual(missing, [])

    def test_skill_declares_name_and_description(self) -> None:
        text = _read(SKILL_MD)
        self.assertTrue(text.startswith("---\n"), "SKILL.md must open with YAML frontmatter")
        end = text.find("\n---", 4)
        self.assertNotEqual(end, -1, "SKILL.md frontmatter must be closed")
        frontmatter = text[4:end].splitlines()

        fields: dict[str, str] = {}
        for line in frontmatter:
            key, sep, value = line.partition(":")
            if sep:
                fields[key.strip()] = value.strip()
        self.assertEqual(fields.get("name"), "cao-operator")
        self.assertTrue(fields.get("description"), "description must be non-empty")

    def test_openai_metadata_is_display_only(self) -> None:
        text = _read(OPENAI_YAML)
        self.assertIn("display_name:", text)
        self.assertIn("short_description:", text)

    def test_skill_ships_no_executable_or_runtime_files(self) -> None:
        found = {
            str(path.relative_to(SKILL_ROOT))
            for path in SKILL_ROOT.rglob("*")
            if path.is_file()
        }
        self.assertEqual(found, EXPECTED_SKILL_FILES)
        for path in SKILL_ROOT.rglob("*"):
            if path.is_file():
                self.assertFalse(
                    os.access(path, os.X_OK),
                    f"skill file must not be executable: {path}",
                )

    def test_skill_keeps_the_two_cli_surfaces_distinct(self) -> None:
        text = _read(SKILL_MD)
        self.assertIn("python -m cao", text)
        self.assertIn("./cao", text)
        self.assertIn("read-only", text.lower())
        self.assertIn("effectful", text.lower())

    def test_skill_carries_fail_closed_guardrails(self) -> None:
        text = _read(SKILL_MD)
        for required in (
            "UNAVAILABLE",
            "Registry",
            "TEAM_ROUTE_PRIVACY_UNVERIFIED",
            "TEAM_ROUTE_READ_ONLY",
            "CAO_TEAM_RESULT",
            "dangerously-bypass",
            "doctor",
            "daemon",
        ):
            self.assertIn(required, text, f"SKILL.md must state {required!r}")

    def test_skill_links_both_references(self) -> None:
        text = _read(SKILL_MD)
        self.assertIn("references/commands.md", text)
        self.assertIn("references/recovery.md", text)

    def test_guide_explains_discovery_only_grants_nothing(self) -> None:
        text = _read(GUIDE)
        for directory in (
            "~/.agents/skills",
            "~/.codex/skills",
            "~/.config/opencode/skills",
        ):
            self.assertIn(directory, text)
        lowered = text.lower()
        self.assertIn("discoverable", lowered)
        self.assertIn("no authorization", lowered)
        self.assertIn("no live cao access", lowered)

    def test_no_private_tokens_in_skill_or_guide(self) -> None:
        offenders: list[str] = []
        for path in REQUIRED_FILES:
            if not path.is_file():
                continue
            lowered = _read(path).lower()
            for token in PRIVATE_TOKENS:
                if token in lowered:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {token}")
        self.assertEqual(offenders, [])

    def test_guard_detects_a_planted_token(self) -> None:
        # Synthetic self-check: the scan is only meaningful if it can fail.
        planted = "example private label resident and /home/ trace"
        hits = [token for token in PRIVATE_TOKENS if token in planted.lower()]
        self.assertEqual(sorted(hits), ["/home/", "resident"])


if __name__ == "__main__":
    unittest.main()
