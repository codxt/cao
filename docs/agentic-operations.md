# Agentic operations: target architecture and decision record

**Document status: `TARGET_DESIGN` / `ACCEPTED FOR IMPLEMENTATION PLANNING` on
2026-10-17. This document is not shipped behavior.** It records the
operator-accepted *target* architecture for CAO's agentic Decision Agent,
operational memory, self-correction and governed self-development, the decision
that governs it, and the boundaries that any implementation must preserve. It
implements nothing, activates nothing and promotes no existing capability.

## How to read this document

Three layers stay explicit and are never blurred:

| Layer | Meaning | Where it lives |
|---|---|---|
| **Source evidence** | What committed code, tests and shipped docs actually do | `src/cao/*.py`, `tests/test_*.py`, `docs/*.md` |
| **Accepted plan / target** | What the owner has decided CAO *should* build | this document, and the execution briefs in [`agentic-operations-a-b.md`](agentic-operations-a-b.md) |
| **Live implementation** | What actually runs and is verified | the modules and contracts named in §3 |

A statement here is a **target** unless it cites committed source or a live
test. Reviewers must not read a target as shipped. The completion vocabulary of
[`intake-roadmap.md`](intake-roadmap.md) applies unchanged: `SOURCE_COMPLETE`,
`TEAM_COMPLETE`, `MERGED`, `DEPLOYED` and `LIVE_ACCEPTED` are five distinct
states that are never collapsed. Nothing in this document promotes a commit to a
merge, a merge to a deploy, or a deploy to live acceptance.

The doctrine basis is the owner's generic cross-project work-execution contract
(objective, accepted Plan, work package, Capability, qualified Performer,
Authority, Evidence). That contract is owned outside this repository and
establishes **no universal task database and no new authority concept**; neither
does this document.

## 1. Scope and non-goals

This document covers:

- the **decision record** for how CAO should add model-backed judgment, durable
  operational memory and governed self-development (§4);
- the **responsibility flow** from a request to accepted work, learning and
  correction (§5, §6);
- the **effect classes** a decision may and may not cross (§6);
- the **memory layers** and what may become authoritative (§7);
- **event-driven AI** and its bounded budget (§8);
- **governed self-development** of CAO itself (§9);
- the measurable **success criteria** and the acceptance rule (§11, §12);
- the **phased plan** A → B → C/D and its cross-links (§10).

It explicitly does **not**:

- implement, activate or schedule anything;
- add a new authority concept, a second canonical store, a second Registry, a
  second scheduler, a new controller or a new manager process;
- claim unattended portfolio management, unattended WRITE, or any reserved
  consequential effect;
- certify any provider, model, entitlement or data class;
- forecast a date or a cost.

## 2. The problem

CAO today is deliberately deterministic and fail-closed, and that determinism
is load-bearing:

- **Deterministic request routing.** `cao request` makes one finite routing
  decision (`BACKLOG` / `INVESTIGATE` / `IMPLEMENT`, plus refusal) from a
  rule table with no model and no hidden state
  (`src/cao/task_router.py`, `cao.request.v1`; see
  [`intake.md`](intake.md) §"The finite routing decision").
- **B3 triage is deterministic and performs no egress.** Advisory triage reads
  bounded context and emits one of five dispositions, recording `ai.used =
  false` and `ai.egress = "none"`; no provider credential exists and no AI
  result is fabricated (`src/cao/intake_triage.py`, `cao.intake.triage.v1`; see
  [`intake-roadmap.md`](intake-roadmap.md) §4).
- **B4 is proposal-only.** The routing compiler emits an immutable, reviewable
  `cao.team.v1` draft with `authority = "none"` and `dispatch = false`; it
  starts, merges, claims, sends and deploys nothing
  (`src/cao/intake_compile.py`).
- **B8 is a READ-scoped autonomous pipeline, opt-in and default-disabled.**
  With an explicit `daemon.intake_pipeline = true` and a provisioned,
  root-owned grant in the fixed protected directory, the existing daemon runs
  bounded discovery → B3 triage → B6 eligibility → B7 queue on one tick. B4
  stays draft-only, B5 stays a projection, and a B3 disposition is never
  permission. On a host with no provisioned grant, every first dispatch is
  `HOLD` (`src/cao/intake_authority.py`, `cao.intake.authority.v1`;
  `src/cao/intake_pipeline.py`, `cao.intake.pipeline.tick.v1`; see
  [`intake.md`](intake.md) §"Standing Intake authority and the autonomous
  pipeline (B8)").
- **Team Execution is the only execution path.** Bounded dependency graphs of
  specialist agents run through the existing task runner, one shared Work
  Registry and the existing daemon; immutable identities, independent review,
  bounded correction and a receipt protocol are already defined
  (`src/cao/team.py`, `cao.team.v1`; see
  [`team-execution.md`](team-execution.md)).
- **Feedback and engineering loops are opt-in and non-authoritative.**
  The feedback watch admits bounded READ investigations
  (`src/cao/feedback_loop.py`, `cao.feedback.watch.v1`);
  `improvement.learn` is `PROPOSALS_ONLY` and modifies no rule, prompt, policy
  or deployment (`src/cao/improvement.py`, `cao.improvement.learning.v1`; see
  [`evidence-to-investigation.md`](evidence-to-investigation.md)); the
  engineering watch/queue keep their exact-head, independent-review and
  separate-operator-grant gates and change no gate verdict themselves
  (`src/cao/engineering_metrics.py`; see
  [`engineering-automations.md`](engineering-automations.md)).

The gap this design addresses is **judgment**: recognizing a contradiction, a
stall, a recurring failure or an ambiguous request, and proposing a grounded,
scoped next action — without granting that judgment any authority and without
building a parallel orchestration system. The risk the design guards against is
the opposite failure: **overbuilding self-improvement before unattended task
completion is itself proven**, which would add authority surface before the
existing pipeline is trusted to complete routine work unattended.

## 3. What is implemented, tested, deployed and operating

The distinction is enforced, not asserted. For every named capability, a
reviewer must read the **exact state reached** from source and test evidence:

| Capability | Source | Documented state |
|---|---|---|
| Deterministic request routing | `src/cao/task_router.py` | implemented; deterministic; no model |
| B3 advisory triage | `src/cao/intake_triage.py` | implemented, merged and independently accepted; `ai.used = false` |
| B4 routing compiler | `src/cao/intake_compile.py` | implemented (`SOURCE_COMPLETE`); draft only; acceptance pending |
| B5 resolution projection | `src/cao/intake_resolution.py` | implemented (`SOURCE_COMPLETE`); reads only; acceptance pending |
| B6 governed handoff | `src/cao/intake_handoff.py` | implemented (`SOURCE_COMPLETE`); `PLAN_ONLY`; acceptance pending |
| B7 durable READ queue | `src/cao/intake_queue.py` | implemented (`SOURCE_COMPLETE`); opt-in, default-disabled; first dispatch held |
| B8 standing authority + pipeline | `src/cao/intake_authority.py`, `src/cao/intake_pipeline.py` | implemented (`SOURCE_COMPLETE`); opt-in, default-disabled; no grant ⇒ `HOLD` |
| Team Execution | `src/cao/team.py` | shipped contract |
| R3 deterministic recovery | `src/cao/team_recovery.py` | implemented in source |
| R4 engineering metrics | `src/cao/engineering_metrics.py` | implemented in source |
| Feedback watch | `src/cao/feedback_loop.py` | opt-in; READ investigations only |
| `improvement.learn` | `src/cao/improvement.py` | `PROPOSALS_ONLY` |

"Implemented" is not "operating": a module that exists, imports and passes its
tests is not a deployment, and a deployment is not `LIVE_ACCEPTED`. No row in
this table is evidence of unattended operation.

## 4. Decision record

**Decision (accepted for implementation planning, 2026-10-17).** CAO adds
model-backed judgment, operational memory and self-correction **by composing the
components it already owns**, and by adding at most two small, isolated, stdlib
modules that project existing evidence and emit advisory plans:

1. **Reuse the existing CAO daemon** (`src/cao/product_daemon.py`) as the only
   cadence owner. No new timer, thread, socket, wake daemon or service.
2. **Reuse the managed-runtime execution path** (the Herdr/OpenCode product
   task path and `cao team`) for every model call. Judgment runs as a normal
   READ CAO task inside the existing bounds, receipts and privacy gates — never
   through a new privileged model gateway.
3. **Reuse the one shared Work Registry** (`src/cao/registry.py`) for all
   writer ownership. No shadow Registry, no second ledger, no parallel claim
   store.
4. **Reuse Team Execution** (`src/cao/team.py`) for all immutable manifests,
   review, correction and receipts.
5. **Reuse the feedback and engineering loops** (`src/cao/feedback_loop.py`,
   `src/cao/improvement.py`, `src/cao/engineering_metrics.py`) for diagnosis,
   proposal and evaluation instead of inventing a second learning system.

**Explicitly rejected.**

- **A new external manager/supervisor process above the daemon.** Rejected: the
  daemon already owns cadence, and a second supervisor duplicates authority and
  introduces a new failure domain with no new capability.
- **A new controller or central reasoning service.** Rejected: reconciliation
  already lives in the daemon tick and the team loop; a new controller would
  either duplicate them or bypass their gates.
- **A shadow Registry or second canonical memory store.** Rejected: two sources
  of writer truth or operator truth is exactly the failure CAO's separation of
  concerns exists to prevent (§7).
- **Unrestricted self-evolution.** Rejected: an agent that can widen its own
  authority, thresholds or scope is an unbounded authority escalation. CAO's
  self-development is governed and human-merged (§9).
- **A second canonical memory store.** Rejected in favour of recomputable,
  private, scoped episode projections plus accountable owner documents (§7).

**Model-agnostic by construction.** The design names no concrete model,
provider or entitlement. Any model choice resolves at runtime from the
installation's own operator-local allowlist (`team_model_allowlist`) and a
data-class/privacy gate; CAO ships no product-wide default and never substitutes
a provider when a route is unavailable.

### 4.1 Strongest challenger and how it is answered

**Challenger (kept, not dismissed): build self-improvement later.** The
strongest counter-argument is that adding model-backed judgment, memory and
self-correction **before CAO reliably completes routine unattended READ work**
is premature: a system that is still qualifying its own recovery and receipts
will make worse decisions with a model in the loop, and the model path adds
cost and privacy surface before the deterministic path is trusted.

**Answer.** The design accepts the challenger's ordering:

- **Phase A (operational memory) ships first and alone.** It is a
  deterministic, stdlib-only projection of evidence CAO already produces. It
  makes no model call, grants no authority and changes no existing contract, so
  it cannot regress unattended completion.
- **Phase B (model-backed decision and recovery) is opt-in and default
  disabled.** The actual model stage stays off until privacy and route are
  qualified, and the deterministic path (B3 rules, R3 recovery) remains the
  fallback. A model result is always advisory and never self-authorizing (§6).
- **Phases C/D are gated on measured success (§11).** They start only after the
  criteria are met on evidence, not on enthusiasm.

The challenger therefore does not block the decision; it **sets the order and
the off-switch**.

## 5. Responsibility flow

The target flow, with each stage's authority kept explicit:

```text
intake / request (admitted; intent only)
  -> deterministic preflight + source evidence
       (cao.request rule table; B3 deterministic packet; B6 eligibility)
  -> qualified READ Decision Agent            [ADVISORY, model-backed, opt-in]
       (runs as a normal READ CAO task; allowlist + data-class gate)
  -> structured advisory plan + risk classification   [ADVISORY]
  -> deterministic authority + repository/Registry validation   [DECIDES]
       (B6/B7/B8 operator gate; live Registry preflight; exact HEAD/scope)
  -> immutable CAO Team Execution             [EXECUTES, READ first]
  -> independent reviewer / bounded correction   [ADVISORY verdict]
  -> exact-head tests / CI / acceptance         [INDEPENDENT GATES]
  -> learning (feedback + improvement; proposals only)
```

Invariants that hold at every arrow:

- **Intake is intent, not work.** Admission, a decision, a plan and a model
  result are never approval, never a claim and never canonical truth.
- **The model stage is advisory only.** It produces a plan and a risk class; it
  cannot authorize, dispatch, claim, merge, deploy or mutate memory.
- **Determinism decides.** The authority and validation stage is deterministic
  and consumes only attributable evidence; a model's confidence never enters it
  as authority.
- **Execution is immutable and READ-first.** The first dispatch of any new scope
  is a READ task; a WRITE requires a separate approved manifest, a live
  Registry preflight and an atomic claim.
- **Review and correction are independent.** A reviewer is a separate agent
  against the latest accepted source HEAD; a correction is a new, separately
  identified bounded task.
- **Learning proposes.** Diagnosis and lessons never overwrite a rule, prompt,
  detector, policy, threshold or deployment.

## 6. Effect classes

Every request is classified before anything is authorized. The classes mirror
the existing reserved-effect classifier in B6/`cao request`
(`src/cao/intake_handoff.py`, `src/cao/task_router.py`):

| Class | Examples | Who may perform it |
|---|---|---|
| **Routine READ** | triage, investigation, review, status projection, memory retrieval | existing deterministic path and, when qualified, a READ Decision Agent; first dispatch still behind the operator gate |
| **Scoped WRITE** | a bounded source/test change inside a declared path scope | an approved immutable team manifest, a live Registry preflight and an atomic claim; never a model decision alone |
| **Reserved consequential** | `merge`, `deploy`, `release`, `publish`, `credential`, `permission`, `destructive` | a distinct accountable operator authorization; the agent proposes, it never performs |

**Mixed requests are decomposed, not frozen whole.** A request that names both
a read scope and a reserved effect is **never** downgraded to a READ plan and is
**never** refused wholesale on that basis alone. The deterministic classifier
splits the actionable READ portion, which may proceed through the normal READ
path, from the reserved portion, which is held with a precise reason and an
actionable next step. This preserves the B6 rule that a mixed
effect-and-read request is not silently reduced to a read plan while adding the
target refinement that the read portion is not blocked by the reserved portion.

**Models never confer authority.** No classifier output, plan field,
confidence score or model rationale changes the class, widens a scope, satisfies
a gate or substitutes for an operator authorization. Authority is bound to the
exact requested action and re-verified at dispatch time.

## 7. Operational memory layers

Memory is layered by what it may become, and only the owner documents are
authoritative.

### 7.1 Working team state (existing; authoritative for its own run)

The durable, private team checkpoint and product-task records own the run:
identities, immutable manifests, claims, receipts and status
(`src/cao/team.py`, `src/cao/product_task.py`, `src/cao/state.py`). This layer
is already the idempotency authority and is not duplicated.

### 7.2 Operational episodes (new; private, recomputable, non-authoritative)

A scoped **episode** is a projection, rebuilt from evidence CAO already
produces (Intake/triage, team checkpoints, Registry rows, review verdicts,
CI/evaluation reports, engineering and feedback records). Each episode carries,
at minimum:

- **objective and case** — a bounded statement of what was attempted;
- **context snapshot** — the evidence set that grounded it, with an explicit
  cutoff;
- **actor/team** — the team and role identity, never a credential;
- **evidence references** — locations and content digests, not raw payloads;
- **event/observed time** — RFC 3339 UTC stamps, distinct from the event time;
- **repo scope/base/head** — repository, path scope, base and event HEAD;
- **event type and verdict** — a bounded vocabulary (for example a routing,
  review, recovery or engineering verdict);
- **hypothesis** — a stated, reversible explanation, marked as a hypothesis;
- **action/outcome** — what was attempted and what the evidence shows;
- **correction and validated lesson** — the correction applied and the lesson
  only if a later independent acceptance supports it.

Rules:

- **Rebuildable.** Episodes are a projection; the source records remain the
  truth. Deleting the episode projection loses no authoritative state.
- **No secrets, ever.** An episode references evidence by location and digest.
  It never stores raw secrets, credentials, tokens, private prompts, raw
  transcripts, unredacted logs or personal identifiers.
- **Scoped and isolated.** Retrieval is bounded to the requesting
  operator/repository/data class; a cross-scope request returns nothing rather
  than a partial match.
- **Evidence status is preserved.** Every referenced fact keeps its verdict
  (`SUPPORTED`, `UNAVAILABLE`, `UNKNOWN`, `REFUSED`, `INVALID`); an unreadable
  or unestablished fact stays `UNKNOWN` and is never rounded up.
- **Stale invalidation.** A reference whose exact HEAD/commit no longer matches
  is marked stale on read rather than silently served as current.
- **Bounded retention.** Retention is explicit and bounded; expiry is a
  deliberate, testable invariant, not an accident.

### 7.3 Accepted procedural lessons (existing owner documents; authoritative)

A validated lesson becomes authoritative **only** when an accountable owner
records it in the document that owns that procedure (for example
[`team-execution.md`](team-execution.md), [`automatic-recovery.md`](automatic-recovery.md),
[`evidence-to-investigation.md`](evidence-to-investigation.md)). This is the
existing rule: `improvement.learn` proposes detection checks and modifies
nothing; the owner document is the only accountable home for an accepted
procedural change.

**No implicit promotion.** A model output, a high-confidence advisory plan, a
repeated incident or an episode is **never** promoted to authoritative memory
by assertion. Promotion requires independent acceptance by an accountable
owner and, where it changes behavior, a separately reviewed change.

## 8. Event-driven AI and budget

Model judgment runs **only on judgment events**, not on a fixed cadence:

- a new request that the deterministic path cannot ground;
- a detected contradiction between recorded evidence and observed state;
- a stall or an `UNKNOWN`/`BLOCKED` task the deterministic recovery cannot
  resolve under its existing rules;
- a recurring failure across independent incidents.

For every other tick the deterministic preflight runs and **no model is
called**. When a judgment event fires, the AI stage is:

- **bounded** — at most one bounded call per event, under the existing
  per-tick budget and dispatch lock;
- **privacy-gated** — performed only against a route and data class the
  installation's own policy permits; an unqualified route is `UNAVAILABLE`, not
  substituted;
- **cost-recorded** — the call records its bounded cost evidence so cost per
  accepted outcome is measurable (§11);
- **fallback-safe** — if the model path is disabled, unavailable or refused,
  the deterministic path's result stands.

There is no always-on agent loop, no polling model and no background
reasoning.

## 9. Governed self-development

CAO may propose and implement changes to **CAO itself** only under the ordinary
governed path, with no special authority:

- an agent **proposes** a scoped CAO fix (objective, evidence, exact path
  scope, hypothesis, tests);
- the change is developed and reviewed by **separate** developer, reviewer and
  worktree identities, under a **separate** Work Registry claim;
- it requires **tests and evaluation** on the exact head;
- it merges only through **independent source merge** (the existing
  GitHub/CI/operator-grant gates), never self-merged;
- rollout and rollback are **external** — an authorized owner performs them;
- the agent **cannot** change its own authority, its evaluation thresholds or
  the live daemon it runs under.

A model may suggest a hypothesis; it never grants scope, changes a threshold,
edits evaluation criteria, merges or deploys.

## 10. Phasing

| Phase | Content | Gate to start |
|---|---|---|
| **A — operational memory** | isolated `src/cao/operational_memory.py`, a rebuildable episode projection | repository gates met; no overlapping writer on `team.py`/`product_daemon.py` |
| **B — model-backed decision & recovery** | isolated `src/cao/decision_agent.py`, an advisory Decision Agent over the existing READ task path | Phase A accepted **and** B8 (or any overlapping work) reconciled; privacy/route qualified |
| **C — self-correction** | bounded, evidence-gated correction proposals feeding the existing review/correction loop | Phase B measured success (§11) |
| **D — governed self-development** | §9 applied to CAO changes | Phase C measured success and an independent owner decision |

**A integrates before B.** Phase B's integration into the daemon/team path
waits for Phase A to land and for any overlapping writer on the shared
`team.py`/`product_daemon.py` surface to settle, so B never merges against a
moving shared interface. C and D are named here only to fix the order; they are
not authorized by this document.

## 11. Measurable success criteria

These are the criteria against which the phases are judged. They are
**definitions**, not measurements: no value below is claimed as achieved.

| Criterion | Definition | Evidence source |
|---|---|---|
| **Grounded triage accuracy** | agreement of the advisory classification with independently reviewed ground truth over a bounded case set | B3 packets vs. independent review |
| **Unattended completion** | fraction of qualifying READ intents that reach a verified terminal receipt with no operator intervention | team checkpoints and receipts |
| **Recovery correctness** | fraction of automatic recovery transitions that are correct and reversible, with zero incorrect automatic WRITEs | R3 decision receipts; Registry releases |
| **Repeat incidents** | count of incidents recurring after an accepted correction | feedback/engineering records |
| **Regressions** | count of goal/guardrail regressions after an accepted change | `improvement.verify` outcomes |
| **Cost per accepted outcome** | bounded model cost divided by independently accepted outcomes | recorded bounded cost evidence |
| **Unauthorized effects** | count of effects performed without the required gate — the target is exactly **0** | Registry rows, team records, CI/merge state |
| **Memory retrieval utility** | fraction of retrieved episodes that an independent reviewer judges relevant and correctly scoped | episode retrieval logs vs. review |

A phase advances only when its criteria are met on evidence and independently
reviewed. The criteria are not self-graded.

## 12. Explicit acceptance rule

**No release, no memory promotion and no canonical acceptance occurs on an
agent's assertion.** The following are never acceptance, on their own or
together:

- an agent's statement that it is done, confident, correct or approved;
- a completion receipt (it proves a post-prompt response was observed, nothing
  more);
- a model advisory plan or risk classification;
- a B3 disposition, a B4 draft, a B5 projection or a B7 queue intent;
- green CI alone or a review verdict alone.

Acceptance requires an accountable operator to inspect tests, CI, the reviewed
diff, security, operating state and authorization, and a distinct authorization
for any reserved effect.

## 13. Navigation

- Execution briefs for Phases A and B:
  [`agentic-operations-a-b.md`](agentic-operations-a-b.md)
- Current shipped intake contract: [`intake.md`](intake.md)
- Governed intake roadmap (B3–B8): [`intake-roadmap.md`](intake-roadmap.md)
- Team execution contract: [`team-execution.md`](team-execution.md)
- Automatic recovery and unattended review: [`automatic-recovery.md`](automatic-recovery.md)
- Evidence-to-investigation and the feedback watch:
  [`evidence-to-investigation.md`](evidence-to-investigation.md)
- Engineering evaluations and guarded automation:
  [`engineering-automations.md`](engineering-automations.md)
- Runtime plugin contract: [`runtime-plugin.md`](runtime-plugin.md)
- Core architecture: [`architecture.md`](architecture.md)
- Repository overview: [`../README.md`](../README.md)

## 14. Provenance

| Item | Source | Standing |
|---|---|---|
| Deterministic request routing | `src/cao/task_router.py`, `docs/intake.md` | implemented; deterministic |
| B3 advisory triage | `src/cao/intake_triage.py`, `docs/intake-roadmap.md` | implemented, merged, accepted; no egress |
| B4/B5/B6/B7/B8 intake middle | `src/cao/intake_*.py`, `docs/intake.md`, `docs/intake-roadmap.md` | implemented (`SOURCE_COMPLETE`) as documented; no promotion |
| Team Execution | `src/cao/team.py`, `docs/team-execution.md` | shipped contract |
| R3/R4 recovery and engineering gauges | `src/cao/team_recovery.py`, `src/cao/engineering_metrics.py`, `docs/automatic-recovery.md` | implemented in source |
| Feedback watch and `improvement.learn` | `src/cao/feedback_loop.py`, `src/cao/improvement.py`, `docs/evidence-to-investigation.md` | opt-in; proposals only |
| Work Registry | `src/cao/registry.py` | shipped; single shared writer ledger |
| Cross-project work-execution contract | owner's contract (owner-referenced v0.4) | owner planning reference; re-verify against canonical home |
| This target architecture | operator-accepted planning decision, 2026-10-17 | `TARGET_DESIGN`; not shipped |
