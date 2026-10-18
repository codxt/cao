"""Model-routing policy for read-only Codex roles and non-ZDR providers.

Pins the fail-closed additions to the optional ``team_model_allowlist`` gate:

* a Codex agent-profile route resolves the canonical allowlist identity
  ``codex/<model>`` from a native ``--model`` token, binds agent_kind and
  provider.cli to ``codex``, admits exactly one unambiguous model, and is
  READ-only (a WRITE route is refused);
* the Gemini Argon Contributor route is known not to be zero-data-retention
  and is refused at every NEW dispatch, because the clean-public-input
  admission gate this build requires is not technically verifiable;
* the canonical Step-5 Preview Free route is READ-only under a configured
  allowlist, and every pre-existing other OpenCode route keeps its exact
  behavior.
"""
import tempfile
import unittest
from pathlib import Path

from cao import product_config, team

CODEX_MODEL = "gpt-6.1-sol"
CODEX_IDENTITY = "codex/gpt-6.1-sol"
GEMINI_ROUTE = "opencode-go/gemini-4-argon-contributor"
OPENCODE_ROUTES = ("opencode/step-5-preview-free",
                   "opencode-go/glm-5.3-flash",
                   "opencode-go/deepseek-v4.1-flash")


def policy(routes):
    return product_config.team_model_policy(product_config.validate_config({
        "schema": product_config.SCHEMA,
        "team_model_allowlist": list(routes),
    }))


def route(kind, cli, provider_model, args):
    return {"agent_kind": kind,
            "agent_args": list(args),
            "provider": {"cli": cli, "model": provider_model}}


def codex_error(pol, args, mode, kind="codex", cli="codex"):
    return product_config.model_policy_error(
        route(kind, cli, None, args), pol, mode=mode)


class CodexModeRouteTests(unittest.TestCase):
    def test_native_argv_model_resolves_to_the_canonical_identity(self):
        # Medium and High share the model and differ only by the bounded CLI
        # reasoning-effort switch; both resolve the same base identity.
        pol = policy([CODEX_IDENTITY])
        for effort in ("medium", "high"):
            args = ["--model", CODEX_MODEL, "-c",
                    f"model_reasoning_effort={effort}"]
            with self.subTest(effort=effort):
                self.assertIsNone(codex_error(pol, args, "read"))

    def test_unqualified_write_and_unknown_mode_are_refused_closed(self):
        pol = policy([CODEX_IDENTITY])
        self.assertEqual(
            codex_error(pol, ["--model", CODEX_MODEL], "write"),
            "TEAM_ROUTE_WRITE_UNQUALIFIED")
        self.assertEqual(
            codex_error(pol, ["--model", CODEX_MODEL], None),
            "TEAM_ROUTE_READ_ONLY")

    def test_effort_variant_does_not_escape_read_only(self):
        # The bounded reasoning-effort variant is still the same Codex route, so
        # a WRITE dispatch on it stays refused; it is not a new identity.
        pol = policy([CODEX_IDENTITY])
        self.assertEqual(
            codex_error(pol, ["--model", CODEX_MODEL + "#high"], "write"),
            "TEAM_ROUTE_READ_ONLY")

    def test_foreign_alias_never_stands_in_for_the_identity(self):
        pol = policy([CODEX_IDENTITY])
        # The same bare token through a foreign provider CLI gets a foreign
        # canonical identity and is not permitted.
        self.assertEqual(
            codex_error(pol, ["--model", CODEX_MODEL], "read", cli="claude"),
            "TEAM_MODEL_NOT_ALLOWED")
        # The canonical model string through a foreign provider CLI is a
        # spoofed runtime identity, refused as an identity mismatch.
        self.assertEqual(
            product_config.model_policy_error(
                route("claude", "claude", None, ["--model", CODEX_IDENTITY]),
                pol, mode="read"),
            "TEAM_ROUTE_IDENTITY_MISMATCH")

    def test_foreign_prefix_does_not_grant_codex_write(self):
        # A foreign-prefixed Codex-bound model stays READ-only even when
        # operator allowlisting includes that foreign identity.
        pol = policy(["openai/gpt-6.1-sol"])
        self.assertEqual(
            product_config.model_policy_error(
                route("codex", "codex", None, ["--model", "openai/gpt-6.1-sol"]),
                pol, mode="write"),
            "TEAM_ROUTE_READ_ONLY")
        self.assertIsNone(product_config.model_policy_error(
            route("codex", "codex", None, ["--model", "openai/gpt-6.1-sol"]),
            pol, mode="read"))

    def test_spoofed_agent_kind_is_refused(self):
        pol = policy([CODEX_IDENTITY])
        # kind=claude while the CLI is codex: the canonical identity is
        # permitted but the runtime identity is not codex.
        self.assertEqual(
            codex_error(pol, ["--model", CODEX_MODEL], "read", kind="claude"),
            "TEAM_ROUTE_IDENTITY_MISMATCH")

    def test_divergent_provider_and_argv_model_is_unresolved(self):
        # Exactly one unambiguous model: a provider selection that disagrees
        # with the verified argv is unresolved, never silently preferred.
        pol = policy([CODEX_IDENTITY])
        self.assertEqual(
            product_config.model_policy_error(
                route("codex", "codex", CODEX_IDENTITY,
                      ["--model", CODEX_MODEL]),
                pol, mode="read"),
            "TEAM_MODEL_UNRESOLVED")

    def test_provider_only_route_without_native_model_is_refused(self):
        # A Codex route whose model appears only in the provider entry would
        # launch Codex with no explicit --model, so its implicit default could
        # stand in for the allowlisted selection: it is unresolved, never a
        # silent pass on the provider entry alone.
        pol = policy([CODEX_IDENTITY])
        for provider_model in (CODEX_IDENTITY, CODEX_MODEL):
            with self.subTest(provider_model=provider_model):
                self.assertEqual(
                    product_config.model_policy_error(
                        route("codex", "codex", provider_model, []),
                        pol, mode="read"),
                    "TEAM_MODEL_UNRESOLVED")

    def test_explicit_native_model_argument_is_admitted(self):
        # Exactly one unambiguous native --model argument is the accepted form,
        # whether or not the provider entry also carries the selection.
        pol = policy([CODEX_IDENTITY])
        self.assertIsNone(codex_error(pol, ["--model", CODEX_MODEL], "read"))
        self.assertIsNone(product_config.model_policy_error(
            route("codex", "codex", CODEX_MODEL, ["--model", CODEX_MODEL]),
            pol, mode="read"))

    def test_ambiguous_or_misleading_argv_is_never_a_model(self):
        pol = policy([CODEX_IDENTITY])
        cases = (
            (["--model", CODEX_MODEL, "--model", CODEX_MODEL],
             "TEAM_MODEL_UNRESOLVED"),
            (["--model=" + CODEX_MODEL], "TEAM_MODEL_UNRESOLVED"),
            (["--config", "--model", CODEX_MODEL], "TEAM_MODEL_UNRESOLVED"),
            (["-c", "--model", CODEX_MODEL], "TEAM_MODEL_UNRESOLVED"),
            (["--model"], "TEAM_MODEL_UNRESOLVED"),
            (["--model", "not a safe token"], "TEAM_MODEL_UNRESOLVED"),
            (["--model", "gpt-6.1-sol-2"], "TEAM_MODEL_NOT_ALLOWED"),
        )
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(codex_error(pol, args, "read"), expected)

    def test_exact_qualified_native_write_profiles(self):
        for model in ("gpt-6.1-sol", "gpt-6-luna"):
            pol = policy(["codex/" + model])
            for effort in ("medium", "high"):
                args = ["--model", model, "-c", "model_reasoning_effort=" + effort,
                        "--sandbox", "workspace-write", "-c",
                        "approval_policy=on-request"]
                with self.subTest(model=model, effort=effort):
                    self.assertIsNone(codex_error(pol, args, "write"))

    def test_codex_write_rejects_loose_alias_and_privilege_escalation(self):
        pol = policy([CODEX_IDENTITY, "codex/gpt-6-luna",
                      "codex/gpt-6-luna-plus"])
        exact = ["--model", CODEX_MODEL, "-c", "model_reasoning_effort=high",
                 "--sandbox", "workspace-write", "-c",
                 "approval_policy=on-request"]
        cases = [
            (["--model", CODEX_MODEL], "TEAM_ROUTE_WRITE_UNQUALIFIED"),
            (exact[:-1] + ["approval_policy=never"], "TEAM_ROUTE_WRITE_UNQUALIFIED"),
            (exact[:5] + ["danger-full-access"] + exact[6:],
             "TEAM_ROUTE_WRITE_UNQUALIFIED"),
            (exact + ["--full-auto"], "TEAM_ROUTE_WRITE_UNQUALIFIED"),
            (exact[:3] + ["model_reasoning_effort=max"] + exact[4:],
             "TEAM_ROUTE_WRITE_UNQUALIFIED"),
            (exact[:1] + [CODEX_MODEL + "#high"] + exact[2:],
             "TEAM_ROUTE_READ_ONLY"),
            (exact[:1] + ["gpt-6-luna-plus"] + exact[2:],
             "TEAM_ROUTE_READ_ONLY"),
        ]
        for args, reason in cases:
            with self.subTest(args=args):
                self.assertEqual(codex_error(pol, args, "write"), reason)
        # Model identity and sandbox alone cannot grant WRITE.
        self.assertEqual(codex_error(pol, exact, None), "TEAM_ROUTE_READ_ONLY")


class NonZdrRouteTests(unittest.TestCase):
    def test_gemini_is_refused_even_when_allowlisted(self):
        # The provider is known not to be zero-data-retention; no value of the
        # allowlist or mode may admit it because the public-input admission
        # gate this build requires cannot be technically verified.
        pol = policy([GEMINI_ROUTE])
        args = ["mini", "--standalone", "--model", GEMINI_ROUTE]
        for mode in ("read", "write", None):
            with self.subTest(mode=mode):
                self.assertEqual(
                    product_config.model_policy_error(
                        route("opencode", "opencode", None, args),
                        pol, mode=mode),
                    "TEAM_ROUTE_PRIVACY_UNVERIFIED")

    def test_bare_gemini_token_cannot_launder_a_foreign_prefix(self):
        pol = policy(["claude/gemini-4-argon-contributor"])
        self.assertEqual(
            product_config.model_policy_error(
                route("claude", "claude", None,
                      ["--model", "gemini-4-argon-contributor"]),
                pol, mode="read"),
            "TEAM_ROUTE_PRIVACY_UNVERIFIED")

    def test_non_canonical_casing_cannot_launder_the_privacy_refusal(self):
        # A route id spelled with different casing names the same upstream
        # provider, so it is refused exactly like the canonical id.
        pol = policy(["opencode-go/Gemini-4-Argon-Contributor"])
        selected = "opencode-go/Gemini-4-Argon-Contributor"
        for mode in ("read", "write", None):
            with self.subTest(mode=mode):
                self.assertEqual(
                    product_config.model_policy_error(
                        route("opencode", "opencode", selected,
                              ["mini", "--standalone", "--model", selected]),
                        pol, mode=mode),
                    "TEAM_ROUTE_PRIVACY_UNVERIFIED")

    def test_unlisted_gemini_route_is_still_denied(self):
        pol = policy(["opencode/step-5-preview-free"])
        self.assertEqual(
            product_config.model_policy_error(
                route("opencode", "opencode", None,
                      ["mini", "--standalone", "--model", GEMINI_ROUTE]),
                pol, mode="read"),
            "TEAM_MODEL_NOT_ALLOWED")

    def test_effort_variant_cannot_bypass_the_privacy_refusal(self):
        # A bounded ``#effort`` variant never changes the upstream provider, so
        # an allowed base id reached with an ``…#high`` selection must be
        # refused exactly like the bare id instead of slipping the gate.
        pol = policy([GEMINI_ROUTE])
        for selected in (GEMINI_ROUTE + "#high", GEMINI_ROUTE + "#xhigh"):
            for mode in ("read", "write", None):
                with self.subTest(selected=selected, mode=mode):
                    self.assertEqual(
                        product_config.model_policy_error(
                            route("opencode", "opencode", selected,
                                  ["mini", "--standalone", "--model", selected]),
                            pol, mode=mode),
                        "TEAM_ROUTE_PRIVACY_UNVERIFIED")

    def test_effort_variant_of_a_bare_non_zdr_token_is_refused(self):
        # The bare model name behind the route is matched regardless of the
        # provider prefix and of an effort variant, so a foreign runtime cannot
        # launder it into a permitted route.
        pol = policy(["claude/gemini-4-argon-contributor"])
        self.assertEqual(
            product_config.model_policy_error(
                route("claude", "claude", None,
                      ["--model", "gemini-4-argon-contributor#high"]),
                pol, mode="read"),
            "TEAM_ROUTE_PRIVACY_UNVERIFIED")


class Step5PreviewFreeReadOnlyTests(unittest.TestCase):
    """The canonical owner policy restricts Step-5 Preview Free to READ.

    The route may read its own bounded checkout but must never be escalated
    into a writer; a WRITE or absent/unknown mode is refused before any claim
    or agent effect, and no alias spelling or provider prefix restores write.
    """

    ROUTE = "opencode/step-5-preview-free"

    def selected(self, pol, model, mode="read", cli="opencode",
                 kind="opencode"):
        args = ["mini", "--standalone", "--model", model]
        return product_config.model_policy_error(
            route(kind, cli, model, args), pol, mode=mode)

    def test_read_is_admitted_including_bounded_effort_variants(self):
        pol = policy([self.ROUTE])
        for model in (self.ROUTE, self.ROUTE + "#high", self.ROUTE + "#xhigh"):
            with self.subTest(model=model):
                self.assertIsNone(self.selected(pol, model, "read"))

    def test_write_and_unknown_mode_are_refused_closed(self):
        pol = policy([self.ROUTE])
        for model in (self.ROUTE, self.ROUTE + "#high"):
            for mode in ("write", None):
                with self.subTest(model=model, mode=mode):
                    self.assertEqual(self.selected(pol, model, mode),
                                     "TEAM_ROUTE_READ_ONLY")

    def test_alias_spelling_or_foreign_prefix_cannot_restore_write(self):
        # A bare token qualified by the runtime CLI, a non-canonical casing and
        # a foreign-prefixed alias all resolve to the same restricted base
        # model: each is READ-admitted and each WRITE dispatch stays refused.
        for listed, model, cli in (
                (self.ROUTE, "step-5-preview-free", "opencode"),
                ("opencode/Step-5-Preview-Free",
                 "opencode/Step-5-Preview-Free", "opencode"),
                ("claude/step-5-preview-free",
                 "claude/step-5-preview-free", "claude")):
            with self.subTest(model=model):
                pol = policy([listed])
                self.assertIsNone(self.selected(pol, model, "read", cli=cli))
                self.assertEqual(
                    self.selected(pol, model, "write", cli=cli),
                    "TEAM_ROUTE_READ_ONLY")

    def test_other_opencode_routes_keep_write(self):
        routes = ("opencode-go/glm-5.3-flash",
                  "opencode-go/deepseek-v4.1-flash")
        pol = policy(routes)
        for model in routes:
            with self.subTest(model=model):
                self.assertIsNone(self.selected(pol, model, "write"))


class OpenCodeRoutesUnchangedTests(unittest.TestCase):
    def test_owner_routes_resolve_for_read_and_write(self):
        # The pre-existing OpenCode routes other than the READ-only Step-5
        # Preview Free route keep both modes.
        routes = ("opencode-go/glm-5.3-flash",
                  "opencode-go/deepseek-v4.1-flash")
        pol = policy(routes)
        for selected in routes:
            for mode in ("read", "write"):
                args = ["mini", "--standalone", "--model", selected]
                with self.subTest(route=selected, mode=mode):
                    self.assertIsNone(product_config.model_policy_error(
                        route("opencode", "opencode", selected, args),
                        pol, mode=mode))

    def test_effort_variant_is_the_same_base_route(self):
        pol = policy(["opencode-go/glm-5.3-flash"])
        for variant in ("#high", "#xhigh"):
            with self.subTest(variant=variant):
                self.assertIsNone(product_config.model_policy_error(
                    route("opencode", "opencode",
                          "opencode-go/glm-5.3-flash" + variant,
                          ["mini", "--standalone",
                           "--model", "opencode-go/glm-5.3-flash" + variant]),
                    pol, mode="read"))

    def test_unseen_model_is_not_permitted(self):
        pol = policy(OPENCODE_ROUTES)
        self.assertEqual(
            product_config.model_policy_error(
                route("opencode", "opencode", "opencode-go/luna",
                      ["mini", "--standalone", "--model", "opencode-go/luna"]),
                pol, mode="read"),
            "TEAM_MODEL_NOT_ALLOWED")


class TeamCodexRoutingTests(unittest.TestCase):
    """The same decision at team plan/init time, before any dispatch effect."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"XDG_STATE_HOME": str(self.root / "state"),
                    "XDG_CONFIG_HOME": str(self.root / "config")}
        self.raw = {
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "id": "local",
                         "model": "opencode/default"},
            "team_approval": {"mode": "chat"},
            "registry_db": str(self.root / "shared.sqlite3"),
            "team_model_allowlist": [CODEX_IDENTITY, "opencode/default"],
            "agent_profiles": {
                "codex_read": {"agent_kind": "codex",
                               "read_args": ["--model", CODEX_MODEL]},
                "codex_dev": {"agent_kind": "codex",
                              "read_args": ["--model", CODEX_MODEL],
                              "write_args": ["--model", CODEX_MODEL,
                                             "--sandbox", "workspace-write"]},
            },
        }
        self.configure()

    def configure(self):
        product_config.write_config(self.raw, product_config.config_path(self.env))
        self.config = product_config.validate_config(self.raw)

    def read_spec(self, team_id, role):
        return {"schema": team.SPEC_SCHEMA, "team_id": team_id,
                "initiative": "i-" + team_id,
                "roles": {"reviewer": {"instruction": "Inspect source",
                                       "agent_profile": role}},
                "tasks": [{"id": "read", "role": "reviewer",
                           "repo": str(self.root / "repo"), "mode": "read",
                           "prompt": "Review exact source"}],
                "max_parallel": 1, "max_writers": 0}

    def write_spec(self, team_id):
        return {"schema": team.SPEC_SCHEMA, "team_id": team_id,
                "initiative": "i-" + team_id,
                "roles": {
                    "worker": {"instruction": "Implement the change",
                               "agent_profile": "codex_dev"},
                    "reviewer": {"instruction": "Review the change",
                                 "model": "opencode/default"}},
                "tasks": [{"id": "impl", "role": "worker",
                           "repo": str(self.root / "repo"), "mode": "write",
                           "write_scope": ["a.txt"], "prompt": "Implement",
                           "reviewer": "reviewer", "max_corrections": 1}],
                "max_parallel": 1, "max_writers": 1}

    def test_codex_read_role_is_admitted_and_bound(self):
        spec = self.read_spec("codex-read", "codex_read")
        record = team.initialize(spec, env=self.env, config=self.config,
                                 approved=True)
        self.assertEqual(record["status"], "PAUSED")
        bound = team._dispatchable_routes(self.config, team.validate_spec(spec))
        work = bound["routes"]["read"]["work"]
        self.assertEqual(work["agent_kind"], "codex")
        self.assertEqual(work["provider"]["cli"], "codex")
        self.assertIsNone(work["provider"]["model"])
        self.assertEqual(product_config.route_model(work),
                         ("resolved", CODEX_MODEL))
        self.assertIs(bound["allowed"]["read"]["work"], True)

    def test_codex_write_requires_exact_mode_specific_args(self):
        spec = self.write_spec("codex-write")
        with self.assertRaises(team.TeamAdmissionError) as caught:
            team.initialize(spec, env=self.env, config=self.config,
                            approved=True)
        self.assertIn("TEAM_ROUTE_WRITE_UNQUALIFIED", str(caught.exception))
        self.assertEqual(team.list_teams(env=self.env), [])
        bound = team._dispatchable_routes(self.config, team.validate_spec(spec))
        self.assertIs(bound["allowed"]["impl"]["work"], False)
        self.assertIs(bound["allowed"]["impl"]["review"], True)

    def test_gemini_role_is_unavailable_at_init(self):
        self.raw["team_model_allowlist"] = [GEMINI_ROUTE]
        self.configure()
        spec = self.read_spec("gemini-read", "gemini_route")
        spec["roles"]["reviewer"] = {"instruction": "Inspect source",
                                     "model": GEMINI_ROUTE}
        with self.assertRaises(team.TeamAdmissionError) as caught:
            team.initialize(spec, env=self.env, config=self.config,
                            approved=True)
        self.assertIn("TEAM_ROUTE_PRIVACY_UNVERIFIED", str(caught.exception))
        self.assertEqual(team.list_teams(env=self.env), [])

    def test_step5_read_role_is_admitted_and_bound(self):
        self.raw["team_model_allowlist"] = ["opencode/step-5-preview-free"]
        self.configure()
        spec = self.read_spec("step5-read", "step5_reader")
        spec["roles"]["reviewer"] = {"instruction": "Inspect source",
                                     "model": "opencode/step-5-preview-free"}
        record = team.initialize(spec, env=self.env, config=self.config,
                                 approved=True)
        self.assertEqual(record["status"], "PAUSED")
        bound = team._dispatchable_routes(self.config, team.validate_spec(spec))
        self.assertIs(bound["allowed"]["read"]["work"], True)

    def test_step5_write_role_is_refused_before_any_effect(self):
        # The canonical policy restricts Step-5 Preview Free to READ, so a
        # team WRITE task bound to it is refused at initialization with no
        # team, Registry claim or agent effect, exactly like the Codex route.
        self.raw["team_model_allowlist"] = ["opencode/step-5-preview-free",
                                            "opencode/default"]
        self.configure()
        spec = self.write_spec("step5-write")
        spec["roles"]["worker"] = {"instruction": "Implement the change",
                                   "model": "opencode/step-5-preview-free"}
        with self.assertRaises(team.TeamAdmissionError) as caught:
            team.initialize(spec, env=self.env, config=self.config,
                            approved=True)
        self.assertIn("TEAM_ROUTE_READ_ONLY", str(caught.exception))
        self.assertEqual(team.list_teams(env=self.env), [])
        bound = team._dispatchable_routes(self.config, team.validate_spec(spec))
        self.assertIs(bound["allowed"]["impl"]["work"], False)
        self.assertIs(bound["allowed"]["impl"]["review"], True)


class TeamOpenCodeMiniHarnessTests(unittest.TestCase):
    """An OpenCode route must launch ``mini`` or be refused before any effect.

    A role model, an inherited provider and an ``agent_profile`` alike are
    governed by the same harness requirement: an OpenCode launch that omits
    ``mini`` or its explicit model, or carries an unknown option, opens the
    interactive TUI at its own default model, so it is refused at
    initialization with zero team, Registry claim or pane effect. Native Codex
    profiles keep their existing behavior.
    """

    ROUTE = "opencode-go/glm-5.3-flash"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"XDG_STATE_HOME": str(self.root / "state"),
                    "XDG_CONFIG_HOME": str(self.root / "config")}

    def configure(self, profile_args, *, kind="opencode", allowlist=None,
                  route=None):
        route = route or self.ROUTE
        self.raw = {
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "id": "local", "model": self.ROUTE},
            "team_approval": {"mode": "chat"},
            "registry_db": str(self.root / "shared.sqlite3"),
            "team_model_allowlist": allowlist or [route],
            "agent_profiles": {
                "reader": {"agent_kind": kind, "read_args": list(profile_args)},
            },
        }
        product_config.write_config(self.raw, product_config.config_path(self.env))
        self.config = product_config.validate_config(self.raw)

    def spec(self, team_id):
        return {"schema": team.SPEC_SCHEMA, "team_id": team_id,
                "initiative": "i-" + team_id,
                "roles": {"reviewer": {"instruction": "Inspect source",
                                       "agent_profile": "reader"}},
                "tasks": [{"id": "read", "role": "reviewer",
                           "repo": str(self.root / "repo"), "mode": "read",
                           "prompt": "Review exact source"}],
                "max_parallel": 1, "max_writers": 0}

    def test_non_mini_profile_is_refused_before_any_effect(self):
        for args in (["--model", self.ROUTE],
                     ["--standalone", "--model", self.ROUTE],
                     ["mini", "--model", self.ROUTE, "--print"]):
            with self.subTest(args=args):
                self.configure(args)
                with self.assertRaises(team.TeamAdmissionError) as caught:
                    team.initialize(self.spec("opencode-tui"), env=self.env,
                                    config=self.config, approved=True)
                self.assertIn("TEAM_ROUTE_TUI_UNQUALIFIED",
                              str(caught.exception))
                self.assertEqual(team.list_teams(env=self.env), [])

    def test_profile_missing_model_is_unresolved_before_any_effect(self):
        self.configure(["mini"], allowlist=[self.ROUTE])
        with self.assertRaises(team.TeamAdmissionError) as caught:
            team.initialize(self.spec("opencode-nomodel"), env=self.env,
                            config=self.config, approved=True)
        self.assertIn("TEAM_MODEL_UNRESOLVED", str(caught.exception))
        self.assertEqual(team.list_teams(env=self.env), [])

    def test_valid_mini_profile_is_admitted_and_bound(self):
        self.configure(["mini", "--standalone", "--model", self.ROUTE])
        spec = self.spec("opencode-mini")
        record = team.initialize(spec, env=self.env, config=self.config,
                                 approved=True)
        self.assertEqual(record["status"], "PAUSED")
        bound = team._dispatchable_routes(self.config, team.validate_spec(spec))
        self.assertIs(bound["allowed"]["read"]["work"], True)
        work = bound["routes"]["read"]["work"]
        self.assertEqual(work["agent_kind"], "opencode")
        self.assertEqual(product_config.route_model(work), ("resolved", self.ROUTE))

    def test_native_codex_profile_is_not_subject_to_the_mini_harness(self):
        # A Codex profile names its model natively and never ``mini``; the
        # OpenCode harness gate must not leak into the Codex path.
        self.configure(["--model", "gpt-6.1-sol"], kind="codex",
                       allowlist=["codex/gpt-6.1-sol"], route="codex/gpt-6.1-sol")
        spec = self.spec("codex-native")
        record = team.initialize(spec, env=self.env, config=self.config,
                                 approved=True)
        self.assertEqual(record["status"], "PAUSED")
        bound = team._dispatchable_routes(self.config, team.validate_spec(spec))
        self.assertIs(bound["allowed"]["read"]["work"], True)


if __name__ == "__main__":
    unittest.main()
