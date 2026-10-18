# Agentic operations: Batches A and B implementation briefs

**Document status: `READY_FOR_GOVERNED_IMPLEMENTATION` once every gate in §3 is
verified and recorded. No implementation has started.** This document is a pair
of execution-ready, repository-owned briefs for **Batch A (operational
memory)** and **Batch B (model-backed decision and recovery)** of the target
architecture recorded in [`agentic-operations.md`](agentic-operations.md). It
writes no code, adds no test file, activates no stage and changes no existing
contract.

Read [`agentic-operations.md`](agentic-operations.md) first: it holds the
decision record, the responsibility flow, the effect classes, the memory layers
and the success criteria this brief implements. This brief is subordinate to it
and never widens it.

## 1. How to read this document

The same three layers as the architecture apply, plus one:

| Layer | Meaning |
|---|---|
| **Source evidence** | Committed code, tests and docs actually do this |
| **Accepted target** | [`agentic-operations.md`](agentic-operations.md) |
| **Brief** | This document: an execution-ready plan, not performed work |
| **Live result** | The batch's own committed, reviewed, merged state |

Every statement here is a **plan** unless it cites committed source. Proposed
schema names (`cao.operational.episode.v1`, `cao.decision.plan.v1`, module paths
and test paths) are design proposals; the batch that implements them owns the
exact final names and records any change in its own review.

## 2. Shared constraints (inherited, non-negotiable)

These apply to both batches and are inherited from [`../AGENTS.md`](../AGENTS.md)
and the shipped contracts:

- **Standard library only.** No runtime dependency. The `-S` test run proves the
  core imports without third-party packages.
- **No private values.** No private paths, usernames, tenant names, real run IDs,
  entitlements, secrets or internal operational values in code, docs, fixtures or
  examples. `tests/test_boundaries.py` enforces the repository denylist over
  `src/cao/**` and `docs/*.md`.
- **Fail closed.** Required evidence that cannot be read stays `UNKNOWN`; corrupt
  or unreadable state raises and is never rounded up to a valid state.
- **No silent fallback.** An unavailable capability yields explicit
  `UNAVAILABLE`, never a substitute provider, model, runtime or memory.
- **One canonical home per concern.** No second ledger, no shadow Registry, no
  second canonical memory store, no new scheduler, service, controller or
  manager process.
- **Concern separation is enforced.** `tests/test_boundaries.py` guards import
  direction and read-only Registry access; each new module must declare its own
  separation rule and its Registry access stays read-only.
- **Admission is not work; a plan is not approval; a receipt is not acceptance.**
- **One default-branch governed batch per repository.** Each batch lands as a
  single bounded commit on the default branch through the ordinary review, CI
  and separate-operator-grant gates. A commit is not a merge; a merge is not a
  deploy; a deploy is not `LIVE_ACCEPTED`.
- **Batch A integrates before Batch B.** Batch B's daemon/team integration waits
  for A to land and for any overlapping writer on the shared
  `src/cao/team.py` / `src/cao/product_daemon.py` surface to settle.

## 3. Preflight checklist (exact; every item verified before editing)

Record the exact evidence for each item. A missing item blocks the batch; none
is satisfied by assertion.

1. **Canonical live main and CI.** The working target is the canonical live
   default branch at its then-current head, and CI on that head is green. The
   batch's base is that head, not an earlier or local commit.
2. **Clean, isolated worktree.** A dedicated, clean Git worktree for the batch
   only. No uncommitted or unrelated changes are carried in.
3. **Work Registry conflicts.** A live Registry preflight shows no active claim
   overlapping any file the batch will touch, and no live conflicting writer is
   seen.
4. **Old paused teams affecting module scopes.** Any paused, incomplete or
   historical team whose scope touches `src/cao/team.py`,
   `src/cao/product_daemon.py`, `src/cao/registry.py` or the intake modules the
   batch reads is identified and reconciled (terminal or explicitly out of
   scope) before the batch dispatches.
5. **Team-approval policy.** The installation's `team_approval` mode is known.
   Under `{"mode": "chat"}` an unattended tick declares no operator
   confirmation; the batch's admission path is stated accordingly.
6. **Model / privacy / cost.** For Batch B only, the resolved route is listed in
   the installation's own `team_model_allowlist`, the provider's data class is
   qualified for the exact input, and the bounded cost authority is stated. An
   unqualified or unlisted route keeps the model stage disabled.
7. **External standing grant status.** Whether an operator-provisioned,
   root-owned standing grant exists in the fixed protected directory is
   recorded. **Its absence blocks only an unattended first Intake dispatch**; it
   does not block any other part of Batch A or B.
8. **Operator-reviewed rollout and acceptance.** The rollout and acceptance
   plan (§9, §10) is written and the accountable operator has reviewed it. Both
   batches ship default-disabled; enabling any effect is a separate, explicit
   operator action.

## 4. Batch A — operational memory (episode projection)

**Goal.** Add a narrow, stdlib-only, **rebuildable** operational memory: a
private, scoped projection of episodes already evidenced by existing CAO
records, with deterministic, bounded retrieval. It grants no authority, calls
no model and changes no existing contract.

### 4.1 Start by inspecting existing source and adapters

The first task is read-only reconnaissance, not design:

- read the existing evidence producers and their exact schemas — B3
  (`src/cao/intake_triage.py`, `cao.intake.triage.v1`), B6/B7
  (`cao.intake.handoff.plan.v1`, `cao.intake.queue.intent.v1`), Team Execution
  checkpoints (`src/cao/team.py`, `cao.team.v1`), review/receipt records
  (`src/cao/product_task.py`), Registry rows (`src/cao/registry.py`, read-only),
  recovery decisions (`src/cao/team_recovery.py`), engineering reports
  (`cao.engineering.evaluation.v1`, `cao.engineering.metrics.v1`) and feedback
  records (`cao.feedback.tick.v1`);
- identify the exact field each episode field maps to, and record any field with
  **no** attributable source;
- confirm the existing private store is reused unchanged.

Adapters are **read functions over existing records**, never new collectors and
never shell scraping. No episode may be derived by running a shell command, by
parsing free text a model produced, or by reading a file whose provenance is not
attributable.

### 4.2 Proposed module and schema

- **New module:** `src/cao/operational_memory.py`.
- **New runnable check:** `tests/test_operational_memory.py` (to be added by the
  batch).
- **Proposed episode schema:** `cao.operational.episode.v1`, one record carrying
  the source-grounded fields below.

| Field | Grounding | Notes |
|---|---|---|
| `objective` | case/task statement from the source record | bounded text |
| `case` | case/incident/issue identity where one exists | else explicit none |
| `context_snapshot` | the evidence set that grounded the event, with a cutoff | references, not bodies |
| `actor` / `team` | team and role identity from the team checkpoint | never a credential or a person name |
| `evidence` | list of `{location, digest}` references | digest is a content digest; no raw payload |
| `event_time` / `observed_at` | source event time and the observation time | RFC 3339 UTC, distinct |
| `repo_scope` / `base` / `head` | repository, path scope, base and event HEAD | exact ids |
| `event_type` | bounded vocabulary (routing, review, recovery, engineering, feedback) | no free text |
| `verdict` | the source verdict, preserving evidence status | `SUPPORTED`/`UNAVAILABLE`/`UNKNOWN`/`REFUSED`/`INVALID` |
| `hypothesis` | a stated, reversible explanation | explicitly a hypothesis |
| `action` / `outcome` | what was attempted and what evidence shows | no fabrication |
| `correction` | the correction applied, when one exists | references only |
| `validated_lesson` | present only with an independent acceptance reference | never model-asserted |

The record **never** stores raw secrets, credentials, tokens, private prompts,
raw transcripts, unredacted logs or personal identifiers. Evidence is referenced
by location and digest.

### 4.3 Storage

- Private XDG state under `$XDG_STATE_HOME/cao/` in a dedicated directory, one
  file per projection unit, `0700` directories and `0600` files, written through
  the **existing** validated, atomic, fsynced, descriptor-relative private store
  used by the intake paths (`cao.intake.write_private_record`; state root via
  `cao.product_state.state_dir`).
- This is a **private projection**, not canonical Company State, not a second
  Registry and not a queue. The source records remain the truth.

### 4.4 Required invariants (each is one runnable test)

| Invariant | Required behavior |
|---|---|
| **Idempotent ingest** | Re-ingesting the same source evidence yields the same episode id and content; no duplicate record. |
| **Dedup** | Two ingest passes over overlapping evidence produce no duplicate episode; the id is a one-way digest of the immutable inputs. |
| **Replay across restart** | After a crash/restart, replay produces no second record and no changed content; a reused id for different content is a conflict, never a silent merge. |
| **Scope isolation** | Retrieval is bounded to the requesting operator/repository/data class; a cross-scope query returns nothing, never a partial or inferred match. |
| **Stale HEAD** | A reference whose exact HEAD/commit no longer matches is marked stale on read, never served as current. |
| **Bounded retention** | Retention is explicit and bounded; expired episodes are not returned and expiry is deterministic, not incidental. |
| **Deterministic retrieval** | The same query over the same projection returns the same ordered result; ordering is defined (for example event time, then id). |
| **Unknown propagation** | A referenced fact with verdict `UNKNOWN`/`UNAVAILABLE`/`REFUSED`/`INVALID` is preserved as such; it is never rounded up to a positive fact. |
| **Adversarial injection inert** | Hostile evidence text (shell metacharacters, directive-shaped text, oversized input, symlinked path, path traversal) changes no behavior and creates no file outside the private store. |
| **Corrupt store fails closed** | A corrupt or tampered record raises; it is never partially parsed into a valid episode. |

### 4.5 Cross-version, retention and provenance-invalidation invariants

These are the minimum concrete invariants the batch must pin:

1. **Unknown schema is refused.** A record whose `schema` is not the exact
   supported value is refused, never partially parsed.
2. **Version read is explicit.** A later reader resolves an older version only
   through an explicit version map; an unknown field in a known version is
   refused rather than ignored, and a missing required field is refused.
3. **Deterministic enumeration order.** Enumerating the projection is ordered by
   the defined key and is stable across restart and across an unchanged re-read.
4. **Retention is a bound, not a sweep side effect.** A retention window is
   validated fail-closed; a missing, malformed or non-positive window is refused,
   and a record exactly at the boundary is resolved deterministically in one
   direction.
5. **Provenance invalidation.** When a referenced source record's identity no
   longer resolves, or its content digest no longer matches, the episode is
   marked `INVALIDATED` (or the reference `STALE`) on read. An invalidated
   reference is never counted as a positive fact, and invalidation never
   deletes the underlying source record.
6. **No promotion on read.** Retrieval never mutates an episode, promotes a
   lesson, writes authoritative memory or triggers any effect.

**Test fixtures (synthetic only):** an invented repository, invented team ids,
invented relay-style ids, synthetic evidence references with invented digests,
a stale-HEAD fixture, a version-mismatch fixture, a boundary-retention fixture,
a corrupt-record fixture and a hostile-text fixture. No private submission,
real run id, credential or transcript is ever checked in (§8).

### 4.6 Integration sequencing

- Batch A is **isolated** first: it reads existing records and writes only its
  own private projection. It touches no shared interface in its first landing.
- Any integration that reads these projections from the daemon or team path
  (`src/cao/product_daemon.py`, `src/cao/team.py`) happens **only after** those
  overlapping writers settle, and it is a separate, reviewed change. Batch A's
  first landing must not edit those files.

### 4.7 Test plan and proof

- **Runnable check:** `tests/test_operational_memory.py`, covering every
  invariant in §4.4 and §4.5.
- **Boundary check:** extend the new module's separation rule in
  `tests/test_boundaries.py` so it reaches the Registry read-only, opens no
  egress surface and uses no shell.
- **Qualitative proof (separate):** an independent reviewer confirms the module
  is a projection (deleting it loses no truth), the field-to-source mapping is
  exact, no raw payload is stored, and no effect path exists.
- **Quantitative proof (separate):** the invariant tests, including replay,
  determinism, retention-boundary and invalidation counts; a report of any
  source field with no attributable origin.

## 5. Batch B — model-backed decision and recovery

**Goal.** Add one bounded, **advisory** Decision Agent that turns judgment
events into a structured, deterministic-validated plan. It reuses the existing
managed READ task path, grants no authority, and stays **default-disabled** until
privacy and route are qualified.

### 5.1 Proposed module and execution path

- **New module:** `src/cao/decision_agent.py`.
- **New runnable check:** `tests/test_decision_agent.py` (to be added by the
  batch).
- **Execution path:** the model stage runs as a **normal READ CAO task** through
  the existing managed-runtime product task / `cao team` path
  (`src/cao/product_task.py`, `src/cao/team.py`, driven by
  `src/cao/product_daemon.py`). The batch adds **no** subprocess, scheduler,
  timer, thread or privileged model gateway of its own; it composes the existing
  one.
- **Default:** disabled. The actual model call happens only when the
  installation's policy and privacy gate qualify the exact route and data class;
  otherwise the deterministic path's result stands.

### 5.2 Structured advisory output schema (proposed `cao.decision.plan.v1`)

The model stage may emit **only** this structure. Every field is required unless
marked optional, and every field is validated deterministically.

| Field | Meaning |
|---|---|
| `source_refs` | the exact evidence references the judgment relied on, with digests and cutoff |
| `plan` | the bounded proposed approach, in words |
| `subtasks` | bounded subtasks, each with an id |
| `dependencies` | a `depends_on` set over the subtask ids (declared, acyclic) |
| `owner` | the proposed accountable owner (a role identity, never a credential) |
| `capability` | the required capability and performer kind |
| `privacy_and_model_qualification` | the declared data class and the route's qualification evidence |
| `confidence` / `uncertainty` | a bounded, honest statement of what is not known |
| `action_class` | exactly one of routine READ / scoped WRITE / reserved effect |
| `tests` | the tests the change would require |
| `approval_requirements` | the exact gates the action class needs |
| `accepted_plan_ref` | the accepted Plan/objective reference, if one exists (optional) |

### 5.3 Deterministic validator and independent authority binding

- A **deterministic validator** parses the model output against the schema: an
  unknown field, a missing required field, a cyclic or dangling dependency, an
  out-of-vocabulary `action_class`, an unbounded field, a non-RFC-3339 stamp or
  an oversized value is refused. The model's text is data, never instructions.
- **Authority binding is independent.** The validator emits a plan; it never
  authorizes. Authorization is bound to the **exact requested action** by the
  existing deterministic authority path (B6/B7/B8 operator gate; live Registry
  preflight; exact HEAD/scope) and re-verified at dispatch time. A model's
  `confidence` never enters the authority decision.
- **The model cannot authorize itself.** No plan field, blend of fields, retry
  or self-statement turns an advisory plan into approval, widens a scope,
  satisfies a gate or changes an effect class.

### 5.4 Reuse, not reinvention

- Diagnosis and lessons reuse `src/cao/feedback_loop.py` and
  `src/cao/improvement.py` (which stay `PROPOSALS_ONLY`).
- Review and correction reuse `src/cao/team.py`'s independent reviewer and
  bounded correction rounds.
- Evaluation and release reuse `src/cao/engineering_metrics.py` and the
  engineering watch/queue gates.
- **Model choice resolves at runtime** from the installation's own
  `team_model_allowlist` and the data-class gate — never from a hardcoded,
  private or product-wide model choice. An unavailable or out-of-quota route is
  `UNAVAILABLE`, never substituted.

### 5.5 Required tests (each is a distinct case)

| Case | Required behavior |
|---|---|
| **Prompt injection** | Hostile text in evidence is inert: it changes no plan field, triggers no tool, writes no file and cannot alter the schema. |
| **Model-output spoofing** | A spoofed plan (forged source refs, a fake approval, a claimed `SUPPORTED` verdict) is refused; a plan cannot self-assert approval or evidence. |
| **Distinct cases review-only** | A judgment event whose only safe outcome is review yields a READ/review plan, never a WRITE. |
| **Mixed READ / WRITE / deploy** | The classifier splits the read portion from the reserved portion; the read portion proposes a READ plan and the reserved portion is held with a precise reason (never silently downgraded, never wholesale-frozen). |
| **Duplicate / already implemented** | A duplicate or already-implemented request is recognized from existing evidence and not re-proposed as new work. |
| **Stale / unknown** | Stale or unreadable evidence stays `UNKNOWN`/stale; the plan is refused or held, never grounded on stale facts. |
| **Unsupported provider** | An unlisted, unqualified or non-zero-data-retention route leaves the model stage disabled; the deterministic path stands. |
| **Conflicting Registry claim** | An overlapping active Registry claim holds the plan; no claim, task or team is created. |
| **Malformed output** | Malformed, oversized or out-of-vocabulary model output is refused by the validator; the deterministic result stands. |
| **Receipt loss and recovery** | A lost completion receipt holds `UNKNOWN`; recovery follows the existing `cao team resume` / `cao team retry-read` paths and never invents a receipt or re-plays a prompt. |
| **No self-authorization** | A model plan never creates a team, task, Registry claim or dispatch by itself. |

**Boundary check:** extend the module's separation rule in
`tests/test_boundaries.py` so it reaches the Registry read-only and opens no
egress surface of its own.

### 5.6 Integration gate

- Batch B integrates **after** B8 (or any overlapping work) is reconciled, and
  only after Batch A has landed.
- Batch B **preserves the existing B3/B4/B5/B6/B7/B8 contracts**. If a contract
  must change, the change is a **versioned** interface with an explicit
  integration review, never a silent edit to a shared interface.
- The model stage integrates into the existing daemon cadence only through the
  existing tick and dispatch lock.

### 5.7 Test plan and proof

- **Runnable check:** `tests/test_decision_agent.py`, covering every case in
  §5.5 with deterministic, synthetic fixtures only.
- **Qualitative proof (separate):** an independent reviewer confirms the model
  output is advisory only, the validator is deterministic, authority binding is
  independent of the model, and no self-authorization path exists.
- **Quantitative proof (separate):** injection/spoofing refusals, validator
  refusal counts, no-effect counts (teams/tasks/claims created by a plan = 0),
  and the bounded cost per accepted outcome recorded for the pilot (§8).

### 5.8 Batch B2d — the native model-backed READ execution binding (landed)

`src/cao/decision_execution.py` is the narrow adapter between the landed B2a
decision stage, the B2b/B2c native binding and the existing CAO execution path.
It adds **no** executor: it verifies the native READ intent and the protected B8
standing grant through `cao.decision_intake`, re-reads the **same** protected
standing authority through its in-process adapter, asks the B2b stage for the
bounded pseudonymous alias prompt, and hands exactly one READ manifest -- carrying
that exact tagged decision-plan prompt -- to the existing CAO team owner API
through the **same** governed B6 `cao.intake_handoff.handoff_plan` path the B6/B7
route uses. The dispatch is serialized under the **existing** B7 queue dispatch
kernel lock, and the existing daemon reconciles the team. The post-prompt terminal
receipt, the released READ Work Registry claim and the tagged result are then
validated by the existing B2b/B2c `decision_intake.validate_result`.

- **Default off.** `start_read_team` and `validate_read_result` take
  `enabled = False` and return `HELD` before any evidence is read.
- **Trust comes only from the protected grant.** An absent, malformed, revoked,
  expired, out-of-scope or wrong-source/wrong-model B8 grant, or a
  non-zero-retention or out-of-vocabulary route, holds; trust is never inferred
  from provider configuration, a caller dictionary, a flag, a citation, manifest
  text or a model assertion. Authority is the protected in-process adapter's
  plan-digest-bound verdict only, re-read at dispatch after the B2b evidence.
- **One shared quota, no new ledger.** A READ dispatch consumes the same durable
  B8 quota the B6/B7 route consumes. An alias-citing team carries no raw
  admission id in its prompt, so it is attributed to its submission from the
  governor's own durable handoff-plan binding; an id-citing team is attributed
  from its prompt. Either way the same `OperatorAuthority.evaluate`/consumption
  path counts both, and an incomplete, unreadable or corrupt quota evidence holds
  rather than resetting the budget.
- **The exact tagged decision plan reaches the worker.** The immutable
  `cao.team.v1` task prompt is the exact bounded B2a/B2b prompt: the verified
  source refs (location, digest, head, cutoff, scope, verdict), the route and
  data class, and the `<cao-plan-result submission="synthetic-..." task="read-...">
  ... </cao-plan-result>` instruction before the native `CAO_TEAM_RESULT:` receipt
  instruction. The default (no opt-in) B6/B7 prompt is byte-identical.
- **No private data in the model-visible prompt.** The manifest cites the
  one-way `synthetic-` alias of the verified binding in place of the raw
  admission id; the private title, body, credential and absolute checkout path
  are never rendered. The alias, the plan id and the binding digest are the only
  binding tokens the prompt carries.
- **No weakening of the B6/B7 gates.** The alias citation and the embedded
  prompt are explicit opt-in arguments; the default `_manifest` / `_manifest_cites`
  / `_is_read_manifest_for` / `_bound_read_team_hold` behavior is unchanged, and a
  manifest that cites only the alias is still refused by the default foreign-team /
  READ-only / source / approval gates.
- **No duplicate prompt and no bypass.** A restart, an ambiguous audit or a
  second start is refused (`ALREADY_STARTED`) rather than re-prompted; the retired
  public dispatch that accepted an arbitrary caller grant mapping is gone, so a
  caller dictionary is never authority and creates no team. No shadow team, ledger
  or scheduler is created.
- **Boundary check:** `tests/test_boundaries.py` registers
  `decision_execution.py`, which reaches the owner API only through the B6
  dispatch path, mutates no team directly, writes no Registry row and opens no
  egress surface.
- **Runnable check:** `tests/test_decision_execution.py` (plus the citation
  cases in `tests/test_intake_handoff.py` and the consumption/quota cases in
  `tests/test_intake_authority.py`) covers the default-off gate, the authorized
  plan, the real native READ lifecycle to `PROPOSED` with the exact tagged worker
  prompt, the shared-quota refusal across both routes, the caller-dictionary
  refusal, the held queue lock, and every listed hold.

## 6. Success and failure matrix

| Dimension | Success | Failure (hold) |
|---|---|---|
| Phase A projection | deterministic, scoped, rebuildable, replay-stable | any cross-scope leak, non-deterministic retrieval, corrupt-store acceptance or raw-payload storage |
| Phase A provenance | every field attributable to a source record | a field with no attributable origin, or a stale reference served as current |
| Phase B decision | structured, schema-valid, advisory, authority-independent | malformed output accepted, injection acted on, or a plan that authorizes itself |
| Phase B privacy | route and data class qualified before any call | any call on an unqualified or unlisted route |
| Either batch | zero unauthorized effects | any team, task, claim, merge, deploy or permission effect without its gate |
| Governance | one reviewed commit on the default branch | more than one discontinuous batch, or a batch merged before its predecessor |

## 7. Stop conditions

Stop the batch and report the blocker if any of these hold:

- **Scope creep.** The batch would add a scheduler, service, controller,
  manager, second ledger, shadow Registry or second canonical memory store, or
  would edit a shared interface it does not own.
- **Gate missing.** Any §3 preflight item cannot be evidenced, or a required
  authority/privacy/route gate is absent.
- **Inconclusive evidence.** A required source read is unreadable, stale or
  contradictory and cannot be resolved to a valid state.
- **Timeout / repeated failure.** The same failure recurs without new evidence,
  or a bounded budget is exhausted.
- **Concurrent-writer overlap.** A live conflicting writer or overlapping Registry claim
  is seen on a file the batch must touch.

A stop is reported as `UNKNOWN`/`BLOCKED` with a precise reason and the smallest
next step; it never becomes a fabricated success.

## 8. Synthetic and redacted fixtures; the 10-case pilot

- All fixtures are **synthetic or approved redacted**: invented repositories,
  team ids, relay-style ids, digests and identities. **Never check in private
  submissions**, real run ids, transcripts, logs, credentials or operator
  entitlements.
- The Phase B pilot is an **independently judged 10-case set** built from those
  fixtures, one case per §5.5 row plus a control. The judge is a reviewer
  distinct from the developer; the judge's verdict is recorded with the case and
  the plan, never asserted by the model.
- A pilot that cannot be independently judged remains unpublished; it is not
  evidence of operation.

## 9. Rollout and acceptance

- Both batches land default-disabled and change no existing behavior until an
  operator explicitly enables an effect.
- A governed batch reaches the default branch only through the ordinary review,
  CI and separate-operator-grant gates; a commit is not a merge and a merge is
  not a deploy.
- Acceptance is separate and human: an accountable operator inspects the exact
  head, tests, CI, the reviewed diff, security, the pilot verdict and the
  no-effect evidence before any enablement.
- Enabling the Batch B model stage, the Batch A projection readers in the daemon,
  or any reserved effect is a distinct, explicit operator action with its own
  record.

## 10. Ownership, dependencies and proof separation

| Item | Owner (role) | Depends on | Runnable check | Qualitative proof | Quantitative proof |
|---|---|---|---|---|---|
| Batch A module | developer (isolated worktree) | existing record schemas | `tests/test_operational_memory.py` | projection-only review; field-to-source map | invariant, replay, retention, invalidation counts |
| Batch A boundaries | reviewer | Batch A module | `tests/test_boundaries.py` | read-only Registry; no shell/egress | separation-rule pass |
| Batch B module | developer (isolated worktree) | Batch A landed; B8 reconciled; qualified route | `tests/test_decision_agent.py` | advisory-only, validator-deterministic, authority-independent | injection/spoof refusals; zero self-effects; cost/outcome |
| Batch B boundaries | reviewer | Batch B module | `tests/test_boundaries.py` | read-only Registry; no own egress | separation-rule pass |
| Batch B pilot | independent judge | Batch B module | 10-case fixture set | judge verdict per case | case outcomes recorded |

Dependencies are cumulative: Batch B does not start integration before Batch A
has landed and the overlapping writers have settled.

## 11. Remaining implementation and authority blockers

Recorded honestly; none is resolved by this document:

- **No implementation exists.** Both batches are `READY_FOR_GOVERNED_IMPLEMENTATION`
  only once §3 verifies; no module, test or effect is present because of this
  brief.
- **Batch B model stage is unqualified by default.** The route, privacy and cost
  gates are installation-specific and must be qualified before any call;
  absent qualification, the stage stays disabled.
- **Standing-grant absence.** With no provisioned root-owned grant in the fixed
  protected directory, an unattended first Intake dispatch is `HOLD`; that
  blocks only that dispatch, not Batch A or B.
- **Overlapping writers.** Integration into `src/cao/team.py` /
  `src/cao/product_daemon.py` waits for those writers to settle.
- **Independent acceptance.** A human-reviewed decision to integrate, merge,
  deploy or enable remains separate and is not performed by the batch.
- **Measurement.** Every §11 criterion in [`agentic-operations.md`](agentic-operations.md)
  is a definition, not a measured value; no measurement is claimed here.

## 12. Navigation and provenance

- Target architecture and decision record:
  [`agentic-operations.md`](agentic-operations.md)
- Current shipped intake contract: [`intake.md`](intake.md)
- Governed intake roadmap (B3–B8): [`intake-roadmap.md`](intake-roadmap.md)
- Team execution contract: [`team-execution.md`](team-execution.md)
- Automatic recovery: [`automatic-recovery.md`](automatic-recovery.md)
- Feedback watch and improvement: [`evidence-to-investigation.md`](evidence-to-investigation.md)
- Engineering gates: [`engineering-automations.md`](engineering-automations.md)
- Repository overview: [`../README.md`](../README.md)

| Item | Source | Standing |
|---|---|---|
| Batch A/B target | operator-accepted planning decision, 2026-10-17 | `TARGET_DESIGN` |
| Existing evidence producers and contracts | `src/cao/*.py`, `docs/*.md`, `tests/test_*.py` | implemented as documented in [`agentic-operations.md`](agentic-operations.md) §3 |
| Preflight checklist, invariants, cases, gates | this brief | plan; `READY_FOR_GOVERNED_IMPLEMENTATION` once §3 verifies |
