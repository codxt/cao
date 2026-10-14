# Automatic Recovery and Unattended Review

CAO's automatic recovery classifies each orchestration tick from durable
evidence and never invents a receipt, bypasses a platform refusal or repeats an
issued prompt. This document records the owner rule for the reviewer and the
recovery contract, and the queued implementation batches that follow it. It
states a policy and a target design, not a description of deployed behavior.

## Non-interrupting reviewer (owner rule)

The default READ advisor must be a qualified **non-interactive** process in a
**pinned, verified-clean checkout**. Prefer a headless Codex `exec review` run
with:

- a READ-only sandbox;
- no interactive approvals;
- no untrusted hooks or user configuration;
- a structured, bounded final review carrying the exact reviewed HEAD and the
  task-bound nonce receipt.

That headless Codex preference is a target transport, not a route grant: it
stays a future optional adapter until a separate owner, provider and cost
authority exists for it (R2), and until then the advisor dispatches on an
authorized route from the allowlist below. It never widens the route set, and
it never carries a broader sandbox or permission grant.

Deterministic tests run through the approved CAO fixture runner in synthetic
temporary fixtures, never through a model's unrestricted shell. Keep per-task
**source, nonce, test and Registry** evidence: the exact reviewed commit, the
task's own completion nonce, the deterministic test result and the released
Registry claim.

Never enable any of the following on a reviewer:

- `--dangerously-bypass-approvals-and-sandbox`
- `--dangerously-bypass-hook-trust`
- `danger-full-access`
- unbounded `--auto` permission

No permission bypass can override a host tool-layer refusal: a refused tool
call stays refused and is reported `UNAVAILABLE` rather than retried with
broader authority. If provider authentication, the exact route, policy or a
trusted source fails, record `UNAVAILABLE` and escalate; never impersonate
approval.

### Authorized dispatch allowlist (owner rule)

The owner's authoritative CAO/OpenCode dispatch routes are exactly these
three, and nothing else:

- `opencode/step-5-preview-free` — free;
- `opencode-go/glm-5.3-flash` — $60 quota;
- `opencode-go/deepseek-v4.1-flash` — $80 quota.

An independent READ advisor must run on one of those three authorized routes,
selected within its own quota, and the exact route is recorded next to the
reviewed HEAD. A Go `$15` GPT-6 Luna route, a Space Bunny route, any other
model, and any automatic substitution when a route is unavailable or out of
quota are all outside this rule: CAO reports the route `UNAVAILABLE` with its
cause and escalates for an owner decision rather than dispatching another
provider or model.

Per installation, the operator may make this rule machine-enforced with the
optional CAO product-config field `team_model_allowlist`:

```json
{"team_model_allowlist": ["opencode/step-5-preview-free", "opencode-go/glm-5.3-flash", "opencode-go/deepseek-v4.1-flash"]}
```

It is a bounded list (at most 16) of unique normalized `provider/model`
identifiers without an `#effort` variant. Only the ABSENCE of the field is the
opt-out: it preserves the existing behavior exactly. A present `[]`, `null`,
wrong-typed, malformed, duplicate or over-long value is refused fail-closed by
`validate_config` — a present value can never silently disable the gate. When
configured it is a deny gate resolved only from each
task's own provider and verified agent argv — never from an untrusted prompt.
A route whose model is missing, ambiguous, flag-trapped or not listed is
refused with no silent fallback. A route id carrying an `#effort` (or
`#effort`-like) suffix is accepted as the same base route. The gate applies to
the read-only plan (as a reported `model_allowed` verdict), to team
initialization and start, and to every NEW dispatch intent before any Registry
claim — for both team tasks and direct `cao run` product tasks. An
already-started execution keeps its exact identity, prompt and claim and is
still reconciled and released; changing the allowlist therefore stops new work
instead of freezing work already in flight.

### Qualification states

- **TEMPORARY.** Until the headless Codex transport is verified, the designated
  temporary READ advisor is an independent reviewer dispatched on one of the
  authorized routes above — Step 5 Preview Free, GLM 5.3 Flash or DeepSeek
  v4.1 Flash. The existing interactive Herdr/Codex review remains a legacy
  compatibility fallback, not a reliable unattended default.
- **Optional pilot.** A ChatGPT OAuth / OpenCode credential route may be tried
  only after the account holder authorizes it themselves **and** a separate
  owner, provider and cost authority for that route is granted explicitly; it
  is never switched on automatically by the dispatch allowlist above. The
  provider must still be shown to qualify: the model, the subscription and the
  private-data policy must all qualify, and a read-only permission-deny profile
  must be tested first. CAO never logs in, copies or stores credentials
  automatically.

A receipt proves a post-prompt response was observed; it is never a security
signature, approval, guarantee of correctness or proof that tests passed.

## Failure classification

For each tick, classify from evidence:

- **RUNNING** with observable changes: continue; no duplicate worker.
- **TERMINAL_RECEIPT_CAPTURE_LOST**: inspect both bounded, task-bound and
  time-stamped output windows; accept only an exact post-prompt nonce plus a
  terminal state and Registry/Git/permission evidence. Never invent a receipt.
- **INTERACTIVE_PROVIDER_GATE**: report the provider unavailable and choose the
  qualified non-interactive route for future tasks; never auto-approve hooks,
  permission changes or external scripts.
- **RETRYABLE_READ_TRANSPORT**: one bounded, idempotent same-identity read
  recovery when evidence proves it safe; never repeat a model prompt or spawn a
  competing worker.
- **WRITE_COMMITTED_BUT_UNVERIFIED**: verify the claimed HEAD, declared scope,
  Registry release and tests; preserve pending acceptance; never replay a
  completed writer and never treat a valid commit as approval.
- **REGISTRY_CONFLICT / GIT_DRIFT / SECURITY_FAILURE / PLATFORM_TOOL_DENIAL**:
  hold UNKNOWN/BLOCKED with a cause and an operator action. Never bypass a
  platform tool refusal, invent access, release a foreign claim or restart a
  retired coordinator.
- **PR_CI_PENDING**: poll the existing PR, its exact HEAD and its required
  statuses; merge only with a distinct qualified operator grant, source review
  and green CI, never automatically deploy.

## Bounded terminal observations

A runtime pane is a scrolling window: an earlier, longer read can omit the
receipt that a later, shorter read carries, and the converse. `product_task`
therefore keeps each bounded read as its own provenance-tagged observation --
kept/limit/captured/dropped sizes plus the capture timestamp and the
prompt/stage provenance current at capture -- in addition to the primary
capture, and Team Execution evaluates all of them. A receipt is accepted only
from a **post-prompt terminal** observation whose text is exactly the window it
claims and carries the **exact task-bound nonce**. A pre-prompt or
provenance-unknown window, a prompt echo, a changed nonce and another task's
receipt are all refused. The Registry, Git scope, HEAD-drift and release checks
are unchanged.

## Implementation queue

Dependencies and gates are cumulative; each batch starts only after the
previous one is accepted.

### R2 — headless Codex READ adapter (after R1 accepted/merged)

Qualify a headless Codex Sol High READ adapter through the CAO/Herdr owner
contract: bounded subprocess and timeouts, a JSON result, a sanitized
environment, explicit no-hooks, no repository writes, an exact source SHA and
an authenticated model. Compare the OpenCode OpenAI OAuth optional fallback;
never modify credentials automatically. Run a disposable mock-driver and a
source-free smoke first, then an actual trial.

This batch is a **future optional** adapter, not an entitlement of the
authorized dispatch allowlist above: it may begin only after a separate owner,
provider and cost authority for the Codex route is granted, and until then the
three authorized routes remain the only routes this rule dispatches. It is
qualified under a READ-only sandbox with no interactive approvals and no
untrusted hooks — never `--dangerously-bypass-approvals-and-sandbox`,
`--dangerously-bypass-hook-trust`, `danger-full-access` or an unbounded
`--auto` grant.

Gate: zero Codex TUI trust, hook or command prompt for authorized READ review
under sandbox; no unauthorized write; a reproducible bounded structured result;
no silent model or provider fallback.

### R3 — safe deterministic recovery transitions (after R2 release)

Add safe recovery transitions to the existing CAO daemon: a durable
budget/decision receipt, observations, per-team idempotency and classification.
READ-only resumes may be automatic when the evidence is exact; WRITE is never
automatically replayed and authority, claim and approval remain externally
proven. Add crash/restart and competing-worker tests. No second scheduler or
ledger.

Gate: self-healing only where evidence exists, preserving the ORIGINAL task
ID/claim and refusing every unsafe mutation or unknown evidence.

### R4 — engineering queue and PR review (after R3 canary)

Integrate the existing GitHub engineering evaluation queue and independent PR
review with exact-head gates, a separate operator grant and the release
lifecycle unchanged; add SLA/metrics for a lost receipt, review latency,
retries and terminal provenance. Monitor and report exceptions rather than
sending hourly routine messages.

Gate: CI/evaluation and an operator-granted GitHub merge; no irreversible
sends, permission changes, production deploy or tenant egress.

## Reporting

Report `SOURCE_COMPLETE`, `TEAM_COMPLETE`, `MERGED`, `DEPLOYED` and
`LIVE_ACCEPTED` separately, each with its actual source SHA and evidence.
Publication, deployment and release require distinct accountable
authorization.
