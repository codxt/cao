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

Implemented in source as one small pure classifier
(`src/cao/team_recovery.py`) applied by the existing team tick under the same
`team.dispatch_lock`, over the existing team checkpoint, Work Registry and
runtime observation. No second scheduler, ledger, database, controller, agent
or wake daemon is introduced: the daemon reports each recorded decision and
applies nothing itself.

For every UNKNOWN team task the classifier records exactly one bounded decision
— a classification, a decision, a reason, the next required action and
provenance (task ID, phase, source HEAD, Registry claim ID, nonce-receipt state
and capture timestamp) — into the existing team checkpoint. The receipt is
idempotent: an unchanged evidence snapshot keeps its original fingerprint and
capture stamp instead of rewriting the record. Each task's decision is bounded
to one entry and the per-identity resume ledger is bounded as well.

Classifications: `RUNNING_PROGRESS`, `TERMINAL`, `TERMINAL_RECEIPT_MISSING`,
`INTERACTIVE_GATE`, `READ_TRANSPORT`, `WRITE_COMMITTED_BUT_UNVERIFIED`,
`REGISTRY_CONFLICT`, `GIT_DRIFT`, `SECURITY_FAILURE` and `PR_CI_PENDING`.
Decisions: `CONTINUE`, `RESUME_READ`, `HOLD`, `BLOCK` and `OBSERVE`.

The only automatic transition is one bounded, same-identity READ resume. It is
admitted **only** when every gate is proven from durable evidence: a
recoverable READ transport failure; the SAME original execution identity with
its checkpointed original prompt and agent/pane; an available runtime that
still reports the original worker; a verified active READ Registry row with the
exact claim id, owner, mode, run and work key; and an unchanged pinned Git HEAD
and branch. It reuses the existing `team.resume` transition and the existing
`product_task` reconciliation under the same dispatch lock, so it never starts
a replacement agent, replays an issued prompt, or creates a new claim. At most
one resume is admitted per original execution identity; a repeat holds.

Every other snapshot holds or blocks with its reason: a lost terminal receipt,
an interactive provider/route gate, a Registry conflict or unreadable ledger, a
Git HEAD or branch drift, a platform or tool-layer denial, a pending
exact-head PR CI gate, or an UNKNOWN WRITE — including a
`SOURCE_COMPLETE`-but-unverified WRITE at an already committed head — is never
automatically replayed, retried, stopped, released, merged, published or
impersonated, and its claimed worktree is never mutated. Uncertain evidence
always holds.

Gate: self-healing only where evidence exists, preserving the ORIGINAL task
ID/claim and refusing every unsafe mutation or unknown evidence.

### R4 — engineering queue and PR review (after R3 canary)

Implemented in source as one small pure module
(`src/cao/engineering_metrics.py`) applied by the existing engineering tick and
the existing daemon cadence. The GitHub engineering evaluation queue and the
independent exact-head PR review keep their gates, the separate operator grant
and the release lifecycle unchanged: exact head, evaluation digest, distinct
bot/operator/reviewer identities, protected-branch CI and strict up-to-date
checks, and a single GitHub-native auto-merge request guarded by the persisted
`SUBMITTING`/`REQUESTED` checkpoint. No second ledger, scheduler, watch,
database, worker or wake daemon is introduced, no new credential, permission or
live webhook is added, and nothing is auto-enabled: the metrics change no gate
verdict and the daemon applies none of them.

The module derives SLA and exception metrics on demand from records CAO already
keeps — the engineering checkpoint and tick result, the team recovery decision
receipt (R3), and the durable product task record. It reports, and never
collects into a new store. The covered conditions are:

- **terminal receipt loss** — a held `TERMINAL_RECEIPT_MISSING` decision;
- **review latency** — the exact head's wait for an independent `APPROVED`
  review, measured from the checkpoint's own `first_seen` stamp;
- **retry decisions** — an applied same-identity READ resume is counted, and a
  spent resume budget is reported;
- **claim-release provenance** — a `complete` task must carry a released claim
  with a bounded `complete` outcome and the released source head, and a refused
  release is reported rather than hidden;
- **PR/CI/evaluation waits** — the head's wait on CI, on a measured evaluation
  and on the separate operator grant, each against a bounded ceiling.

Every exception carries its code, its source and the exact head; a condition
that cannot be measured from evidence (a missing capture stamp) is reported
unmeasured and is never rounded up to a breach. An `AWAITING_GITHUB_
RECONCILIATION` state is always reported: the merge request's outcome is
uncertain and must be inspected, never replayed. The daemon surfaces the report
as a bounded effect only when an exception exists, so a healthy tick emits
nothing — this is exception reporting, not an hourly routine message.

Gate: CI/evaluation and an operator-granted GitHub merge; no irreversible
sends, permission changes, production deploy or tenant egress. Reaching this
batch required the accepted, merged R3 source at the live default branch head;
it grants no new authority and admits no new route.

## Reporting

Report `SOURCE_COMPLETE`, `TEAM_COMPLETE`, `MERGED`, `DEPLOYED` and
`LIVE_ACCEPTED` separately, each with its actual source SHA and evidence.
Publication, deployment and release require distinct accountable
authorization.
