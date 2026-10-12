"""Public-surface hygiene guards for this OSS batch.

The runtime-neutral core and the product CLI are public artifacts. This guard
keeps private portfolio labels and host-specific traces out of the top-level
public surfaces (README, AGENTS, setup.sh, pyproject, examples) and out of the
public extraction/provenance record. It complements ``test_boundaries.py``,
which already scans ``src/cao/**`` and ``docs/*.md`` for host traces, by
covering the surfaces that readme/example readers see first.

The scan is text-only and synthetic: it reads repository files and never
contacts a runtime, a host, or the network.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Private portfolio labels and host traces that must not appear in a public
#: artifact. Attribution for the extracted source stays in its accountable
#: private source; the public record carries only attested facts.
PRIVATE_TOKENS = (
    "albr",
    "sentinelx",
    "resident",
    "brain",
    "tolest",
    "companyos",
    "/home/",
)


def public_surfaces() -> list[Path]:
    """The public files this batch owns or publishes into."""

    surfaces = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "AGENTS.md",
        REPO_ROOT / "setup.sh",
        REPO_ROOT / "pyproject.toml",
        REPO_ROOT / "docs" / "extraction-provenance.md",
    ]
    examples = REPO_ROOT / "examples"
    if examples.is_dir():
        surfaces.extend(sorted(path for path in examples.glob("*") if path.is_file()))
    return [path for path in surfaces if path.is_file()]


def find_private_tokens(
    paths: list[Path], tokens: tuple[str, ...] = PRIVATE_TOKENS
) -> list[str]:
    """Offenders as ``<path>: <token>`` for every token found (case-insensitive)."""

    offenders: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8").lower()
        for token in tokens:
            if token in text:
                try:
                    where = path.relative_to(REPO_ROOT)
                except ValueError:
                    where = path
                offenders.append(f"{where}: {token}")
    return offenders


class PublicSurfaceHygieneTests(unittest.TestCase):
    def test_no_private_labels_or_host_traces_in_public_surfaces(self) -> None:
        self.assertEqual(find_private_tokens(public_surfaces()), [])

    def test_guard_detects_a_planted_token(self) -> None:
        # Synthetic fixture: the scan is only meaningful if it can fail.
        with tempfile.TemporaryDirectory() as directory:
            planted = Path(directory) / "planted.md"
            planted.write_text("Synthetic BRAIN label and /home/ trace\n", encoding="utf-8")
            offenders = find_private_tokens([planted], ("brain", "/home/"))
        self.assertEqual(len(offenders), 2)
        self.assertIn("brain", offenders[0])
        self.assertIn("/home/", offenders[1])


if __name__ == "__main__":
    unittest.main()
