"""Focused tests for the CAO source bootstrap and readiness reporting.

Everything is isolated: XDG roots point at a temporary directory, the download
opener, PATH lookup and command runner are injected, and no network or real
binary is ever touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cao import bootstrap, product_config, product_state

REPO_ROOT = Path(__file__).resolve().parents[1]


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _spec(payload: bytes, *, name: str = "rt", version: str = "1.2.3") -> dict:
    key = bootstrap.platform_key()
    return {
        "name": name,
        "version": version,
        "size_cap_bytes": 1 << 20,
        "timeout_seconds": 5,
        "allowed_hosts": ["example.com"],
        "probe_argv": ["--version"],
        "platforms": {
            key: {
                "filename": f"{name}-linux",
                "url": "https://example.com/tool",
                "sha256": _sha(payload),
            }
        },
    }


class FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0
        self.headers: dict = {}

    def read(self, n: int = -1) -> bytes:
        chunk = self._data[self._pos :] if n < 0 else self._data[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:
        pass


def _opener(data: bytes):
    return lambda url, timeout: FakeResponse(data)


class BootstrapCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.env = {
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
        }

    def write_config(self, payload: dict) -> str:
        path = product_config.config_path(self.env)
        product_config.write_config(payload, path)
        return path


class InstallTests(BootstrapCase):
    def test_verify_policy_writes_config_idempotently(self) -> None:
        env = dict(self.env, CAO_MANAGED_SPEC=json.dumps(_spec(b"payload")))
        first = bootstrap.install(
            policy="verify", env=env, which=lambda name: None
        )
        self.assertTrue(first["config_created"])
        config = product_config.config_path(self.env)
        written = Path(config).read_bytes()
        second = bootstrap.install(
            policy="verify", env=env, which=lambda name: None
        )
        self.assertFalse(second["config_created"])
        self.assertEqual(written, Path(config).read_bytes())

    def test_managed_install_records_and_is_idempotent(self) -> None:
        payload = b"pinned-binary"
        env = dict(self.env, CAO_MANAGED_SPEC=json.dumps(_spec(payload)))
        runner = lambda argv: bootstrap.CommandResult(0, "rt 1.2.3\n")
        first = bootstrap.install(
            policy="managed",
            env=env,
            opener=_opener(payload),
            runner=runner,
            which=lambda name: None,
        )
        self.assertEqual(first["status"], "OK")
        self.assertEqual(first["runtime"]["state"], "INSTALLED")
        record = product_state.read_managed(self.env)
        self.assertEqual(record["state"], "INSTALLED")

        def opener_never(*_args):  # pragma: no cover - must not be called
            raise AssertionError("second install must reuse the managed copy")

        second = bootstrap.install(
            policy="managed",
            env=env,
            opener=opener_never,
            runner=runner,
            which=lambda name: None,
        )
        self.assertEqual(second["runtime"]["state"], "ALREADY_INSTALLED")

    def test_no_spec_and_no_config_reports_not_ready(self) -> None:
        payload = bootstrap.install(policy="verify", env=self.env)
        self.assertEqual(payload["status"], "NOT_READY")
        self.assertEqual(payload["error"]["code"], "NO_SPEC")

    def test_malformed_config_fails_closed_without_overwrite(self) -> None:
        path = product_config.config_path(self.env)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text("{ this is not json", encoding="utf-8")
        env = dict(self.env, CAO_MANAGED_SPEC=json.dumps(_spec(b"payload")))
        payload = bootstrap.install(policy="verify", env=env, which=lambda name: None)
        self.assertEqual(payload["status"], "NOT_READY")
        self.assertEqual(Path(path).read_text(encoding="utf-8"), "{ this is not json")

    def test_unsupported_platform_reports_not_ready(self) -> None:
        env = dict(self.env, CAO_MANAGED_SPEC=json.dumps(_spec(b"payload")))
        with mock.patch.object(
            bootstrap,
            "platform_key",
            side_effect=bootstrap.UnsupportedPlatform("no asset"),
        ):
            payload = bootstrap.install(policy="verify", env=env, which=lambda n: None)
        self.assertEqual(payload["status"], "NOT_READY")
        self.assertEqual(payload["error"]["code"], "MANAGED_PLATFORM_UNSUPPORTED")


class DoctorTests(BootstrapCase):
    def test_absent_config_is_not_ready(self) -> None:
        payload, code = bootstrap.doctor(env=self.env)
        self.assertEqual(code, bootstrap.EXIT_UNAVAILABLE)
        self.assertEqual(payload["status"], "NOT_READY")
        self.assertIn("runtime", payload["sections"])
        self.assertIn("provider", payload["sections"])
        self.assertIn("credentials", payload["sections"])
        self.assertIn("daemon", payload["sections"])
        self.assertIn("registry", payload["sections"])

    def test_malformed_config_is_not_ready_with_evidence(self) -> None:
        path = product_config.config_path(self.env)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text("not json", encoding="utf-8")
        payload, code = bootstrap.doctor(env=self.env)
        self.assertEqual(code, bootstrap.EXIT_UNAVAILABLE)
        self.assertEqual(payload["status"], "NOT_READY")
        self.assertEqual(payload["sections"]["configuration"]["status"], "INVALID")

    def test_missing_provider_is_not_ready(self) -> None:
        self.write_config(
            {
                "schema": product_config.SCHEMA,
                "runtime_id": "rt",
                "managed": _spec(b"payload"),
            }
        )
        payload, code = bootstrap.doctor(
            env=self.env, which=lambda name: None, runner=lambda argv: bootstrap.CommandResult(0, "")
        )
        self.assertEqual(code, bootstrap.EXIT_UNAVAILABLE)
        self.assertFalse(payload["gates"]["provider_found"])
        self.assertIn("provider is NOT_CONFIGURED", payload["reasons"])

    def test_unverified_credentials_hold_not_ready(self) -> None:
        self.write_config(
            {
                "schema": product_config.SCHEMA,
                "runtime_id": "rt",
                "provider": {"cli": "opencode", "id": "acct", "model": "m1"},
                "managed": _spec(b"payload"),
            }
        )

        def runner(argv):
            if tuple(argv[1:]) == ("auth", "list"):
                return bootstrap.CommandResult(0, "someone-else\n")
            return bootstrap.CommandResult(0, "opencode 9\n")

        payload, code = bootstrap.doctor(
            env=self.env, which=lambda name: f"/fake/{name}", runner=runner
        )
        self.assertEqual(code, bootstrap.EXIT_UNAVAILABLE)
        self.assertEqual(payload["sections"]["credentials"]["status"], "UNVERIFIED")
        self.assertFalse(payload["gates"]["credentials_verified"])


class CredentialProbeTests(BootstrapCase):
    """The provider id is matched against the CLI's own printed identifiers.

    A provider's authenticated-list entry can print a multi-word display name
    (``OpenCode Go``) while the operator configures the CLI's id
    (``opencode-go``); only what the CLI printed may ever match.
    """

    def _doctor(self, provider_id: str, auth_list: str) -> dict:
        self.write_config(
            {
                "schema": product_config.SCHEMA,
                "runtime_id": "rt",
                "provider": {"cli": "opencode", "id": provider_id, "model": "m1"},
                "managed": _spec(b"payload"),
            }
        )

        def runner(argv):
            if tuple(argv[1:]) == ("auth", "list"):
                return bootstrap.CommandResult(0, auth_list)
            return bootstrap.CommandResult(0, "opencode 9\n")

        payload, _code = bootstrap.doctor(
            env=self.env, which=lambda name: f"/fake/{name}", runner=runner
        )
        return payload["sections"]["credentials"]

    def test_multi_word_display_name_matches_the_configured_id(self) -> None:
        section = self._doctor(
            "opencode-go", "OpenCode Go  OpenCode Go   stored\nNous Portal  stored\n"
        )
        self.assertEqual(section["status"], "VERIFIED")

    def test_case_and_separator_differences_still_match(self) -> None:
        section = self._doctor("OpenCode-Go", "OpenCode Go  stored\n")
        self.assertEqual(section["status"], "VERIFIED")

    def test_printed_single_token_matches(self) -> None:
        section = self._doctor("acct", "acct other\n")
        self.assertEqual(section["status"], "VERIFIED")

    def test_unprinted_id_is_unverified(self) -> None:
        section = self._doctor("not-listed", "OpenCode Go  stored\n")
        self.assertEqual(section["status"], "UNVERIFIED")

    def test_punctuation_only_id_is_unverified(self) -> None:
        section = self._doctor("---", "OpenCode Go  stored\n")
        self.assertEqual(section["status"], "UNVERIFIED")

    def test_window_bound_refuses_a_five_token_span(self) -> None:
        section = self._doctor("abcde", "a b c d e\n")
        self.assertEqual(section["status"], "UNVERIFIED")

    def test_verified_provider_and_runtime_is_ready(self) -> None:
        self.write_config(
            {
                "schema": product_config.SCHEMA,
                "runtime_id": "rt",
                "provider": {"cli": "opencode", "id": "acct", "model": "m1"},
                "managed": _spec(b"payload"),
            }
        )

        def runner(argv):
            if argv[0] == "opencode" and tuple(argv[1:]) == ("--version",):
                return bootstrap.CommandResult(0, "opencode 9\n")
            if argv[0] == "opencode" and tuple(argv[1:]) == ("auth", "list"):
                return bootstrap.CommandResult(0, "acct other\n")
            if argv[0].endswith("/rt"):
                return bootstrap.CommandResult(0, "rt 1.2.3\n")
            return bootstrap.CommandResult(0, "")

        payload, code = bootstrap.doctor(
            env=self.env, which=lambda name: f"/fake/{name}", runner=runner
        )
        self.assertEqual(code, bootstrap.EXIT_OK)
        self.assertEqual(payload["status"], "READY")
        self.assertEqual(payload["sections"]["runtime"]["status"], "ADOPTED")


class SetupScriptTests(BootstrapCase):
    def _env(self) -> dict:
        env = os.environ.copy()
        env.update(self.env)
        env["CAO_PYTHON"] = sys.executable
        env.pop("CAO_MANAGED_SPEC", None)
        return env

    def test_no_download_is_offline_idempotent_and_pins_the_release(self) -> None:
        env = self._env()
        first = subprocess.run(
            ["sh", str(REPO_ROOT / "setup.sh"), "--no-download"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertIn(first.returncode, (0, 3))
        config = product_config.config_path(self.env)
        self.assertTrue(os.path.isfile(config))
        raw_first = Path(config).read_bytes()

        second = subprocess.run(
            ["sh", str(REPO_ROOT / "setup.sh"), "--no-download"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(second.returncode, first.returncode)
        self.assertEqual(raw_first, Path(config).read_bytes())

        managed = product_state.managed_bin_dir(self.env)
        self.assertFalse(os.path.isdir(managed) and os.listdir(managed))

        pinned = json.loads(Path(config).read_text(encoding="utf-8"))["managed"]
        self.assertEqual(pinned["version"], "0.9.3")
        for entry in pinned["platforms"].values():
            self.assertTrue(
                entry["url"].startswith(
                    "https://github.com/herdrdev/herdr/releases/download/v0.9.3/"
                )
            )
            self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
