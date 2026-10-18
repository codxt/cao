"""Tests for the product configuration's daemon section and route policy.

Pins the B8 additions precisely: the fresh-install default keeps both the B7
queue and the B8 standing pipeline disabled, the flags are strict booleans, and
enabling the pipeline leaves the independent feedback/engineering watches
untouched. It also pins that the product ships no global allowed route set and
that the optional operator-local model allowlist is the only route gate.
"""

from __future__ import annotations

import unittest

from cao import product_config


def config(**daemon: object) -> product_config.ProductConfig:
    payload = {"schema": product_config.SCHEMA}
    if daemon:
        payload["daemon"] = daemon
    return product_config.validate_config(payload)


class DaemonDefaultsTests(unittest.TestCase):
    def test_fresh_install_disables_both_intake_flags(self) -> None:
        defaults = product_config.daemon_defaults(config())
        self.assertFalse(defaults.intake_queue)
        self.assertFalse(defaults.intake_pipeline)
        self.assertIsNone(defaults.feedback_watch_file)
        self.assertIsNone(defaults.engineering_watch_file)

    def test_pipeline_flag_can_be_enabled_independently(self) -> None:
        defaults = product_config.daemon_defaults(config(intake_pipeline=True))
        self.assertTrue(defaults.intake_pipeline)
        self.assertFalse(defaults.intake_queue)

    def test_flags_must_be_boolean(self) -> None:
        for value in ("yes", 1, []):
            with self.subTest(value=value):
                with self.assertRaises(product_config.ProductConfigError):
                    product_config.daemon_defaults(config(intake_pipeline=value))
        with self.assertRaises(product_config.ProductConfigError):
            product_config.daemon_defaults(config(intake_queue="yes"))

    def test_pipeline_does_not_require_the_queue_flag(self) -> None:
        # The pipeline is a separate, independent opt-in; enabling it must not
        # silently enable the B7 queue flag (they are reconciled together by the
        # daemon, but the configuration stays explicit).
        defaults = product_config.daemon_defaults(
            config(intake_pipeline=True, intake_queue=False))
        self.assertTrue(defaults.intake_pipeline)
        self.assertFalse(defaults.intake_queue)


class TeamRoutePolicyScopeTests(unittest.TestCase):
    """No global allowed route set; the allowlist is per installation."""

    def test_no_global_allowed_route_set_is_exported(self) -> None:
        # The product ships no product-wide set of allowed dispatch routes.
        # ``SUPPORTED_TEAM_ROUTES`` is retained only as a non-binding,
        # backward-compatible name: it is empty and grants nothing.
        self.assertIn("SUPPORTED_TEAM_ROUTES", product_config.__all__)
        self.assertEqual(product_config.SUPPORTED_TEAM_ROUTES, ())

    def test_absent_allowlist_leaves_routing_unconfigured(self) -> None:
        # Absence preserves the unconfigured behavior; it is not a global grant
        # and it is not derived from any shipped route list.
        unconfigured = product_config.validate_config(
            {"schema": product_config.SCHEMA})
        self.assertIsNone(product_config.team_model_policy(unconfigured))

    def test_configured_allowlist_is_the_only_route_gate(self) -> None:
        configured = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "team_model_allowlist": ["example-provider/example-model"],
        })
        policy = product_config.team_model_policy(configured)
        self.assertIsNotNone(policy)
        self.assertTrue(policy.permits("example-provider/example-model"))
        self.assertTrue(policy.permits("example-provider/example-model#high"))
        self.assertFalse(policy.permits("example-provider/other-model"))


class OpenCodeMiniHarnessTests(unittest.TestCase):
    """Every OpenCode route must launch the non-interactive ``mini`` harness.

    A bare OpenCode launch (a role model, an inherited provider or an
    ``agent_profile`` whose arguments omit ``mini`` or the model, or carry an
    unknown option) would open the interactive TUI at its own default model, so
    it is refused before any Registry claim or pane effect. Native Codex
    behavior is untouched.
    """

    ROUTE = "opencode-go/glm-5.3-flash"

    def policy(self, routes):
        return product_config.team_model_policy(product_config.validate_config({
            "schema": product_config.SCHEMA,
            "team_model_allowlist": list(routes),
        }))

    def route(self, args, *, kind="opencode", cli="opencode", model=None):
        return {"agent_kind": kind, "agent_args": list(args),
                "provider": {"cli": cli, "model": model}}

    def test_non_mini_launch_is_refused_closed(self) -> None:
        pol = self.policy([self.ROUTE])
        for args in (["--model", self.ROUTE],
                     ["--standalone", "--model", self.ROUTE],
                     []):
            with self.subTest(args=args):
                self.assertEqual(
                    product_config.model_policy_error(
                        self.route(args, model=self.ROUTE), pol, mode="read"),
                    "TEAM_ROUTE_TUI_UNQUALIFIED")

    def test_missing_or_ambiguous_model_stays_unresolved(self) -> None:
        pol = self.policy([self.ROUTE])
        for args in (["mini"],
                     ["mini", "--model"],
                     ["mini", "--model=" + self.ROUTE],
                     ["mini", "--model", self.ROUTE, "--model", self.ROUTE]):
            with self.subTest(args=args):
                self.assertEqual(
                    product_config.model_policy_error(
                        self.route(args), pol, mode="read"),
                    "TEAM_MODEL_UNRESOLVED")

    def test_unknown_or_unsafe_options_are_refused(self) -> None:
        pol = self.policy([self.ROUTE])
        for args in (["mini", "--model", self.ROUTE, "--print"],
                     ["mini", "--model", self.ROUTE,
                      "--dangerously-bypass-approvals-and-sandbox"],
                     ["mini", "--standalone", "--standalone",
                      "--model", self.ROUTE]):
            with self.subTest(args=args):
                self.assertEqual(
                    product_config.model_policy_error(
                        self.route(args), pol, mode="read"),
                    "TEAM_ROUTE_TUI_UNQUALIFIED")

    def test_valid_mini_harness_is_admitted(self) -> None:
        pol = self.policy([self.ROUTE])
        for args in (["mini", "--model", self.ROUTE],
                     ["mini", "--standalone", "--model", self.ROUTE]):
            with self.subTest(args=args):
                self.assertIsNone(product_config.model_policy_error(
                    self.route(args), pol, mode="read"))

    def test_provider_only_model_is_refused_because_no_argv_selects_it(self) -> None:
        # The model must reach the actual process: a route that names it only in
        # the provider entry would launch the default TUI selection.
        pol = self.policy([self.ROUTE])
        self.assertEqual(
            product_config.model_policy_error(
                self.route([], model=self.ROUTE), pol, mode="read"),
            "TEAM_ROUTE_TUI_UNQUALIFIED")

    def test_codex_route_is_not_subject_to_the_mini_harness(self) -> None:
        pol = self.policy(["codex/gpt-6.1-sol"])
        self.assertIsNone(product_config.model_policy_error(
            self.route(["--model", "gpt-6.1-sol"], kind="codex", cli="codex"),
            pol, mode="read"))


if __name__ == "__main__":
    unittest.main()
