"""Tests for the product configuration's daemon section.

Pins the B8 additions precisely: the fresh-install default keeps both the B7
queue and the B8 standing pipeline disabled, the flags are strict booleans, and
enabling the pipeline leaves the independent feedback/engineering watches
untouched.
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


if __name__ == "__main__":
    unittest.main()
