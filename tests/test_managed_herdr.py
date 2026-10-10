"""Focused tests for the generic managed-dependency installer.

No network and no real binary: the opener, the command runner and the PATH
lookup are all injected. The installer lives in :mod:`cao.bootstrap` (kept
runtime-neutral), so these tests drive its public surface directly.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from cao import bootstrap


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _spec(
    payload: bytes,
    *,
    name: str = "tool",
    version: str = "1.2.3",
    size_cap: int = 1 << 20,
    hosts: tuple[str, ...] = ("example.com",),
    url: str = "https://example.com/tool-linux",
    probe: tuple[str, ...] = ("--version",),
    sha: str | None = None,
) -> dict:
    return {
        "name": name,
        "version": version,
        "size_cap_bytes": size_cap,
        "timeout_seconds": 5,
        "allowed_hosts": list(hosts),
        "probe_argv": list(probe),
        "platforms": {
            "linux-x86_64": {
                "filename": f"{name}-linux",
                "url": url,
                "sha256": sha if sha is not None else _sha(payload),
            }
        },
    }


class FakeResponse:
    def __init__(self, data: bytes, headers: dict | None = None) -> None:
        self._data = data
        self._pos = 0
        self.headers = headers or {}
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            chunk = self._data[self._pos :]
        else:
            chunk = self._data[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


def _opener(data: bytes, headers: dict | None = None):
    def open_url(url: str, timeout: float) -> FakeResponse:
        return FakeResponse(data, headers)

    return open_url


def _runner(*, tool_version: str = "1.2.3"):
    """A runner that answers the managed tool's version probe."""

    def run(argv):
        if argv[-1] in {"--version", "version"}:
            return bootstrap.CommandResult(0, f"tool {tool_version}\n")
        return bootstrap.CommandResult(0, "")

    return run


class PlatformKeyTests(unittest.TestCase):
    def test_normalizes_known_architectures(self) -> None:
        self.assertEqual(bootstrap.platform_key("Linux", "x86_64"), "linux-x86_64")
        self.assertEqual(bootstrap.platform_key("Linux", "amd64"), "linux-x86_64")
        self.assertEqual(bootstrap.platform_key("Linux", "arm64"), "linux-aarch64")
        self.assertEqual(bootstrap.platform_key("Linux", "aarch64"), "linux-aarch64")

    def test_unknown_architecture_raises(self) -> None:
        with self.assertRaises(bootstrap.UnsupportedPlatform):
            bootstrap.platform_key("Linux", "sparc")


class BuildAssetTests(unittest.TestCase):
    def test_builds_validated_asset(self) -> None:
        spec = _spec(b"payload")
        asset = bootstrap.build_asset(spec, "linux-x86_64")
        self.assertEqual(asset.name, "tool")
        self.assertEqual(asset.version, "1.2.3")
        self.assertEqual(asset.allowed_hosts, ("example.com",))
        self.assertEqual(asset.probe_argv, ("--version",))

    def test_missing_platform_raises(self) -> None:
        with self.assertRaises(bootstrap.UnsupportedPlatform):
            bootstrap.build_asset(_spec(b"p"), "darwin-x86_64")

    def test_bad_digest_rejected(self) -> None:
        with self.assertRaises(bootstrap.SpecError):
            bootstrap.build_asset(_spec(b"p", sha="not-a-digest"), "linux-x86_64")

    def test_non_https_url_rejected(self) -> None:
        with self.assertRaises(bootstrap.SpecError):
            bootstrap.build_asset(
                _spec(b"p", url="http://example.com/tool-linux"), "linux-x86_64"
            )

    def test_disallowed_host_rejected(self) -> None:
        with self.assertRaises(bootstrap.SpecError):
            bootstrap.build_asset(
                _spec(b"p", url="https://evil.example.net/tool-linux"),
                "linux-x86_64",
            )

    def test_unsafe_name_and_filename_rejected(self) -> None:
        with self.assertRaises(bootstrap.SpecError):
            bootstrap.build_asset(_spec(b"p", name="../tool"), "linux-x86_64")
        bad = _spec(b"p")
        bad["platforms"]["linux-x86_64"]["filename"] = "sub/dir"
        with self.assertRaises(bootstrap.SpecError):
            bootstrap.build_asset(bad, "linux-x86_64")


class DownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = temp.name

    def test_size_cap_from_content_length(self) -> None:
        asset = bootstrap.build_asset(
            _spec(b"x", size_cap=4), "linux-x86_64"
        )
        with self.assertRaises(bootstrap.DownloadError):
            bootstrap.download(
                asset,
                self.dir,
                opener=_opener(b"x", headers={"Content-Length": "999"}),
            )
        self.assertEqual(os.listdir(self.dir), [])

    def test_size_cap_enforced_while_streaming(self) -> None:
        asset = bootstrap.build_asset(_spec(b"x", size_cap=4), "linux-x86_64")
        with self.assertRaises(bootstrap.DownloadError):
            bootstrap.download(asset, self.dir, opener=_opener(b"0123456789"))
        self.assertEqual(os.listdir(self.dir), [])

    def test_url_checked_before_opener_runs(self) -> None:
        asset = bootstrap.build_asset(_spec(b"x"), "linux-x86_64")
        asset = bootstrap.Asset(**{**asset.__dict__, "url": "http://example.com/x"})
        calls: list[str] = []

        def opener(url, timeout):
            calls.append(url)
            return FakeResponse(b"x")

        with self.assertRaises(bootstrap.DownloadError):
            bootstrap.download(asset, self.dir, opener=opener)
        self.assertEqual(calls, [])


class VerifyTests(unittest.TestCase):
    def test_hash_mismatch(self) -> None:
        temp = tempfile.NamedTemporaryFile(delete=False)
        self.addCleanup(os.unlink, temp.name)
        temp.write(b"payload")
        temp.close()
        with self.assertRaises(bootstrap.HashMismatch):
            bootstrap.verify_sha256(temp.name, _sha(b"other"))


class EnsureTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = temp.name

    def _asset(self, payload: bytes, **kwargs) -> bootstrap.Asset:
        return bootstrap.build_asset(_spec(payload, **kwargs), "linux-x86_64")

    def test_installs_verified_copy(self) -> None:
        payload = b"fresh-binary"
        asset = self._asset(payload)
        outcome = bootstrap.ensure(
            asset,
            install_dir=self.dir,
            policy="managed",
            opener=_opener(payload),
            runner=_runner(),
            which=lambda name: None,
        )
        self.assertEqual(outcome.state, "INSTALLED")
        installed = Path(self.dir) / "tool"
        self.assertEqual(installed.read_bytes(), payload)
        self.assertTrue(os.access(installed, os.X_OK))

    def test_wrong_hash_refuses_and_keeps_existing_binary(self) -> None:
        payload = b"expected-binary"
        asset = self._asset(payload)
        existing = Path(self.dir) / "tool"
        existing.write_bytes(b"OLD-BINARY")

        with self.assertRaises(bootstrap.HashMismatch):
            bootstrap.ensure(
                asset,
                install_dir=self.dir,
                policy="managed",
                opener=_opener(b"corrupted"),
                runner=_runner(tool_version="0.0.1"),
                which=lambda name: None,
            )
        # The existing executable is byte-for-byte intact and no temp remains.
        self.assertEqual(existing.read_bytes(), b"OLD-BINARY")
        self.assertEqual(os.listdir(self.dir), ["tool"])

    def test_idempotent_when_managed_copy_matches(self) -> None:
        payload = b"binary"
        asset = self._asset(payload)
        installed = Path(self.dir) / "tool"
        installed.write_bytes(payload)

        def opener(url, timeout):  # pragma: no cover - must never run
            raise AssertionError("download must not run for an existing copy")

        outcome = bootstrap.ensure(
            asset,
            install_dir=self.dir,
            policy="managed",
            opener=opener,
            runner=_runner(),
            which=lambda name: None,
        )
        self.assertEqual(outcome.state, "ALREADY_INSTALLED")

    def test_adopts_compatible_external_binary_without_touching_it(self) -> None:
        payload = b"binary"
        asset = self._asset(payload)
        external = Path(self.dir) / "external"
        external.write_bytes(b"system-binary")

        def opener(url, timeout):  # pragma: no cover - must never run
            raise AssertionError("adoption must not download")

        outcome = bootstrap.ensure(
            asset,
            install_dir=os.path.join(self.dir, "managed"),
            policy="adopt",
            opener=opener,
            runner=_runner(),
            which=lambda name: str(external),
        )
        self.assertEqual(outcome.state, "ADOPTED")
        self.assertEqual(outcome.path, str(external))
        self.assertEqual(external.read_bytes(), b"system-binary")

    def test_incompatible_external_binary_is_never_replaced(self) -> None:
        asset = self._asset(b"binary")
        external = Path(self.dir) / "external"
        external.write_bytes(b"system-binary")
        outcome = bootstrap.ensure(
            asset,
            install_dir=os.path.join(self.dir, "managed"),
            policy="adopt",
            runner=_runner(tool_version="0.0.1"),
            which=lambda name: str(external),
        )
        self.assertEqual(outcome.state, "INCOMPATIBLE")
        self.assertEqual(external.read_bytes(), b"system-binary")

    def test_verify_policy_never_downloads(self) -> None:
        asset = self._asset(b"binary")
        outcome = bootstrap.ensure(
            asset,
            install_dir=self.dir,
            policy="verify",
            runner=_runner(),
            which=lambda name: None,
        )
        self.assertEqual(outcome.state, "MISSING")


class VersionMatchTests(unittest.TestCase):
    def test_exact_token_matches(self) -> None:
        self.assertTrue(bootstrap.version_matches("tool 1.2.3\n", "1.2.3"))

    def test_longer_version_does_not_match(self) -> None:
        self.assertFalse(bootstrap.version_matches("tool 1.2.30", "1.2.3"))


if __name__ == "__main__":
    unittest.main()
