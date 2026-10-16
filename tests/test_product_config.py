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


if __name__ == "__main__":
    unittest.main()
