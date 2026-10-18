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


#: The runtime's real ``status server`` plain-text block (envelope shape only).
SERVER_OK = (
    "status: running\n"
    "endpoint_compatible: yes\n"
    "private_protocol_compatible: yes\n"
)


def _runner_with_server(*, tool_version: str = "1.2.3", server: str = SERVER_OK):
    """A runner that answers the version probe and the status handshake."""

    def run(argv):
        if tuple(argv[1:]) == ("status", "server"):
            return bootstrap.CommandResult(0, server)
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


    def test_prerelease_or_build_suffix_is_not_exact_pin(self) -> None:
        self.assertFalse(bootstrap.version_matches("tool 0.9.3-rc1", "0.9.3"))
        self.assertFalse(bootstrap.version_matches("tool 0.9.3+build.2", "0.9.3"))

class InstalledCompatibilityTests(unittest.TestCase):
    """A different installed version is adopted only on positive, read-only
    identity and live protocol attestation.

    The pinned asset identity (version and digest) stays authoritative for the
    managed download path; an adopted runtime's real release is reported
    separately. No binary is ever replaced or stopped.
    """

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = temp.name
        self.managed_dir = os.path.join(self.dir, "managed")

    def _asset(self, *, name: str = "tool", version: str = "0.9.3") -> bootstrap.Asset:
        return bootstrap.build_asset(
            _spec(b"binary", name=name, version=version), "linux-x86_64"
        )

    def _external(self, payload: bytes = b"system-binary") -> Path:
        path = Path(self.dir) / "external"
        path.write_bytes(payload)
        return path

    def _ensure(self, asset, external, runner, *, policy: str = "adopt"):
        def opener(url, timeout):  # pragma: no cover - adoption never downloads
            raise AssertionError("adoption must not download")

        return bootstrap.ensure(
            asset,
            install_dir=self.managed_dir,
            policy=policy,
            opener=opener,
            runner=runner,
            which=lambda name: str(external),
        )

    def test_newer_version_adopted_on_identity_and_live_protocol(self) -> None:
        asset = self._asset(version="0.9.3")
        external = self._external()
        outcome = self._ensure(
            asset, external, _runner_with_server(tool_version="0.9.5")
        )
        self.assertEqual(outcome.state, "ADOPTED")
        self.assertEqual(outcome.path, str(external))
        # The pinned install asset identity is unchanged; the real release is
        # reported separately.
        self.assertEqual(outcome.version, "0.9.3")
        self.assertEqual(outcome.observed_version, "0.9.5")
        self.assertEqual(external.read_bytes(), b"system-binary")
        self.assertFalse(os.path.exists(self.managed_dir))

    def test_unknown_future_version_adopted_on_attestation(self) -> None:
        asset = self._asset(version="0.9.3")
        external = self._external()
        outcome = self._ensure(
            asset, external, _runner_with_server(tool_version="9.9.9")
        )
        self.assertEqual(outcome.state, "ADOPTED")
        self.assertEqual(outcome.observed_version, "9.9.9")

    def test_verify_policy_adopts_attested_without_download(self) -> None:
        asset = self._asset(version="0.9.3")
        external = self._external()
        outcome = self._ensure(
            asset, external, _runner_with_server(tool_version="0.9.5"), policy="verify"
        )
        self.assertEqual(outcome.state, "ADOPTED")
        self.assertFalse(os.path.exists(self.managed_dir))

    def test_foreign_identity_is_refused_even_with_live_protocol(self) -> None:
        asset = self._asset()
        external = self._external()

        def runner(argv):
            if tuple(argv[1:]) == ("status", "server"):
                return bootstrap.CommandResult(0, SERVER_OK)
            return bootstrap.CommandResult(0, "evilcli 9.9.9\n")

        outcome = self._ensure(asset, external, runner)
        self.assertEqual(outcome.state, "INCOMPATIBLE")
        self.assertEqual(outcome.observed_version, "9.9.9")
        self.assertEqual(external.read_bytes(), b"system-binary")

    def test_version_probe_failure_is_refused(self) -> None:
        asset = self._asset()
        external = self._external()

        def runner(argv):
            if tuple(argv[1:]) == ("status", "server"):
                return bootstrap.CommandResult(0, SERVER_OK)
            return bootstrap.CommandResult(1, "", "boom")

        outcome = self._ensure(asset, external, runner)
        self.assertEqual(outcome.state, "INCOMPATIBLE")
        self.assertIsNone(outcome.observed_version)

    def test_protocol_refusal_is_refused_even_for_a_matching_name(self) -> None:
        asset = self._asset()
        external = self._external()
        refusal = "status: not running\nendpoint_compatible: yes\n"
        outcome = self._ensure(
            asset, external, _runner_with_server(tool_version="0.9.5", server=refusal)
        )
        self.assertEqual(outcome.state, "INCOMPATIBLE")

    def test_arbitrary_successful_server_output_is_refused(self) -> None:
        asset = self._asset()
        external = self._external()
        outcome = self._ensure(
            asset, external, _runner_with_server(tool_version="0.9.5", server="all good\n")
        )
        self.assertEqual(outcome.state, "INCOMPATIBLE")

    def test_missing_version_is_refused_before_server_probe(self) -> None:
        asset = self._asset()
        external = self._external()
        calls = []

        def runner(argv):
            calls.append(tuple(argv))
            return bootstrap.CommandResult(0, "tool\n")

        outcome = self._ensure(asset, external, runner)
        self.assertEqual(outcome.state, "INCOMPATIBLE")
        self.assertIsNone(outcome.observed_version)
        self.assertFalse(any(argv[-2:] == ("status", "server") for argv in calls))
        self.assertEqual(external.read_bytes(), b"system-binary")

    def test_incidental_version_in_diagnostic_text_is_refused(self) -> None:
        asset = self._asset()
        external = self._external()

        def runner(argv):
            if tuple(argv[1:]) == ("status", "server"):
                return bootstrap.CommandResult(0, SERVER_OK)
            return bootstrap.CommandResult(0, "tool ready\nbuild 2026.10\n")

        self.assertEqual(
            self._ensure(asset, external, runner).state, "INCOMPATIBLE"
        )

    def test_missing_private_protocol_is_refused(self) -> None:
        asset = self._asset()
        external = self._external()
        server = "status: running\nendpoint_compatible: yes\n"
        self.assertEqual(
            self._ensure(
                asset, external, _runner_with_server(tool_version="0.9.5", server=server)
            ).state, "INCOMPATIBLE"
        )
        self.assertEqual(external.read_bytes(), b"system-binary")

    def test_duplicate_protocol_field_is_refused(self) -> None:
        asset = self._asset()
        external = self._external()
        server = SERVER_OK + "private_protocol_compatible: no\n"
        self.assertEqual(
            self._ensure(
                asset, external, _runner_with_server(tool_version="0.9.5", server=server)
            ).state, "INCOMPATIBLE"
        )

    def test_arbitrary_json_type_is_refused(self) -> None:
        asset = self._asset()
        external = self._external()
        server = '{"result":{"type":"anything"}}'
        self.assertEqual(
            self._ensure(
                asset, external, _runner_with_server(tool_version="0.9.5", server=server)
            ).state, "INCOMPATIBLE"
        )

    def test_json_server_status_requires_all_explicit_proofs(self) -> None:
        asset = self._asset()
        external = self._external()
        cases = (
            '{"result":{"type":"server_status","status":"running",'
            '"endpoint_compatible":"yes"}}',
            '{"result":{"type":"server_status","status":"running",'
            '"endpoint_compatible":1,"private_protocol_compatible":"yes"}}',
            '{"result":{"type":"server_status","status":"running",'
            '"endpoint_compatible":true,"private_protocol_compatible":false}}',
            '{"result":{"type":"server_status","status":"running",'
            '"endpoint_compatible":"yes","endpoint_compatible":"no",'
            '"private_protocol_compatible":"yes"}}',
            '{"error":{"code":"down"},"result":{"type":"server_status",'
            '"status":"running","endpoint_compatible":"yes",'
            '"private_protocol_compatible":"yes"}}',
        )
        for server in cases:
            with self.subTest(server=server):
                self.assertEqual(
                    self._ensure(
                        asset, external,
                        _runner_with_server(tool_version="0.9.5", server=server)
                    ).state, "INCOMPATIBLE"
                )

    def test_valid_typed_json_server_status_is_adopted(self) -> None:
        asset = self._asset()
        external = self._external()
        server = ('{"result":{"type":"server_status","status":"running",'
                  '"endpoint_compatible":true,"private_protocol_compatible":true}}')
        outcome = self._ensure(
            asset, external, _runner_with_server(tool_version="0.9.5", server=server)
        )
        self.assertEqual(outcome.state, "ADOPTED")
        self.assertEqual(outcome.observed_version, "0.9.5")

    def test_newer_managed_copy_is_verified_without_reinstallation(self) -> None:
        asset = self._asset()
        managed_path = Path(self.managed_dir) / asset.name
        managed_path.parent.mkdir()
        managed_path.write_bytes(b"existing-newer-runtime")

        def no_download(*_args):
            raise AssertionError("verify must not download or rewrite")

        outcome = bootstrap.ensure(
            asset, install_dir=self.managed_dir, policy="verify",
            runner=_runner_with_server(tool_version="0.9.5"),
            which=lambda name: None, opener=no_download
        )
        self.assertEqual(outcome.state, "ALREADY_INSTALLED")
        self.assertEqual(outcome.observed_version, "0.9.5")
        self.assertEqual(outcome.version, "0.9.3")
        self.assertEqual(managed_path.read_bytes(), b"existing-newer-runtime")

    def test_incompatible_managed_copy_is_not_misreported_missing(self) -> None:
        asset = self._asset()
        managed_path = Path(self.managed_dir) / asset.name
        managed_path.parent.mkdir()
        managed_path.write_bytes(b"existing-newer-runtime")
        outcome = bootstrap.ensure(
            asset, install_dir=self.managed_dir, policy="verify",
            runner=_runner_with_server(
                tool_version="0.9.5",
                server="status: running\nendpoint_compatible: yes\n"
            ),
            which=lambda name: None
        )
        self.assertEqual(outcome.state, "INCOMPATIBLE")
        self.assertEqual(outcome.observed_version, "0.9.5")
        self.assertEqual(managed_path.read_bytes(), b"existing-newer-runtime")

    def test_explicit_managed_install_uses_pinned_checked_asset(self) -> None:
        asset = self._asset()
        managed_path = Path(self.managed_dir) / asset.name
        managed_path.parent.mkdir()
        managed_path.write_bytes(b"previous-copy")
        outcome = bootstrap.ensure(
            asset, install_dir=self.managed_dir, policy="managed",
            runner=_runner_with_server(tool_version="0.9.5"),
            which=lambda name: None, opener=_opener(b"binary")
        )
        self.assertEqual(outcome.state, "INSTALLED")
        self.assertEqual(outcome.version, "0.9.3")
        self.assertEqual(managed_path.read_bytes(), b"binary")

    def test_prerelease_is_not_pinned_without_live_protocol(self) -> None:
        asset = self._asset(version="0.9.3")
        external = self._external()
        outcome = self._ensure(
            asset, external,
            _runner_with_server(
                tool_version="0.9.3-rc1",
                server="status: running\nendpoint_compatible: yes\n"
            )
        )
        self.assertEqual(outcome.state, "INCOMPATIBLE")
        self.assertEqual(outcome.observed_version, "0.9.3-rc1")

    def test_matching_pinned_version_is_adopted_without_probing_the_server(self) -> None:
        asset = self._asset(version="0.9.3")
        external = self._external()
        calls: list[tuple[str, ...]] = []

        def runner(argv):
            calls.append(tuple(argv))
            if argv[-1] in {"--version", "version"}:
                return bootstrap.CommandResult(0, "tool 0.9.3\n")
            return bootstrap.CommandResult(0, "")

        outcome = self._ensure(asset, external, runner)
        self.assertEqual(outcome.state, "ADOPTED")
        self.assertEqual(outcome.observed_version, "0.9.3")
        # A pinned exact match needs no protocol probe (the doctor gates the
        # live server separately).
        self.assertNotIn((str(external), "status", "server"), calls)


class ReportedVersionTests(unittest.TestCase):
    def test_extracts_the_version_token(self) -> None:
        self.assertEqual(bootstrap.reported_version("tool 0.9.5\n"), "0.9.5")

    def test_prerelease_and_build_versions_are_preserved(self) -> None:
        self.assertEqual(bootstrap.reported_version("tool v0.9.5-rc1"), "0.9.5-rc1")
        self.assertEqual(bootstrap.reported_version("tool 0.9.5+build.2"), "0.9.5+build.2")

    def test_arbitrary_text_yields_none(self) -> None:
        self.assertIsNone(bootstrap.reported_version("no version here"))
        self.assertIsNone(bootstrap.reported_version(""))


if __name__ == "__main__":
    unittest.main()
