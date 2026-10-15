# Intake roadmap (B3–B7): governed request intake

> **Document status.** Accepted **owner plan** for the governed intake middle
> (B3–B7). **B3 is implemented in source** (`src/cao/intake_triage.py`,
> `tests/test_intake_triage.py`), **merged to the default branch and
> independently accepted**. **B4 is implemented in source**
> (`src/cao/intake_compile.py`, `tests/test_intake_compile.py`) and is reported
> as `SOURCE_COMPLETE`: reviewed, merged and independently accepted are
> **separate, later states** and are not claimed here. **B5 is implemented in
> source** (`src/cao/intake_resolution.py`, `tests/test_intake_resolution.py`)
> and is likewise reported as `SOURCE_COMPLETE`; its independent acceptance is a
> separate, later state and is not claimed here. **B6 is implemented in source**
> (`src/cao/intake_handoff.py`, `tests/test_intake_handoff.py`) and is likewise
> reported as `SOURCE_COMPLETE`; it is `PLAN_ONLY` in this build because the
> authenticated operator identity gate is absent, and its independent acceptance
> is a separate, later state and is not claimed here. **B7 is implemented in
> source** (`src/cao/intake_queue.py`, `tests/test_intake_queue.py`) and is
> likewise reported as `SOURCE_COMPLETE`; it is **default-disabled**, it adds no
> operator-authority adapter, and its first dispatch is `HOLD` in this build,
> while an already-approved immutable READ team can be re-verified and advanced.
> B4, B5, B6 and B7 are **not** promoted past the state the repository actually
> records.

> **Owner.** The CAO repository owner. Shared-contract changes and anything
> beyond the stated scope require an accountable integration decision, exactly as
> `AGENTS.md` requires.

> **Doctrine basis.** The owner's generic cross-project work-execution contract
> (owner-referenced **v0.4**): objective, accepted Plan, work package,
> Capability, qualified Performer, Authority, Evidence. That contract is owned
> outside this repository. The version cited here is the owner's accepted
> planning reference and must be re-verified against its canonical home before
> any further phase is implemented. It establishes **no universal task database
> and no new authority concept**, and neither does this roadmap.

> **Product source.** A vendor's official intake product page, reviewed
> 2026-10-14. It is a promotional product page: its statements are **vendor
> claims, not independently measured**, and its mechanisms are legal-work
> specific. They are inspiration and a comparison point, **not** a specification
> CAO copies wholesale.

## How to read this document

Three layers must stay explicit and never be blurred:

| Layer | Meaning | Where it lives |
|---|---|---|
| **Source evidence** | What an external source or in-tree artifact actually says/does | vendor page; committed B1/B2/B3 code in `src/cao/intake.py`, `src/cao/intake_bridge.py`, `src/cao/intake_triage.py`, `docs/intake.md` |
| **Accepted plan** | What the owner has decided CAO *should* build | this document |
| **Live implementation** | What actually runs and is verified | B3 deterministic triage; B4 draft compiler; B5 read-only status projection and mock gate; B6 gated, `PLAN_ONLY` READ handoff |

A statement in this document is a plan unless it cites source evidence or a live
test. Reviewers must not read a planned capability as shipped.

**B1/B2 standing.** The B1/B2 security source is committed on the default branch
and its final independent security acceptance is recorded separately. Nothing in
this document promotes a commit to a merge, a merge to a deploy, or a deploy to
live acceptance.

## 1. The gap B3–B6 close

`docs/intake.md` describes what ships today: one bounded, strictly-shaped JSON
submission admitted **once** into the operator's private XDG state, deliberately
**not** a work item and **not** a Work Registry claim. Nothing in intake starts,
merges, dispatches or schedules anything.

That leaves a governed middle:

- **B3** — read an admitted submission *on demand* together with permitted
  context, and produce an advisory, provenance-linked triage result.
  **Implemented.**
- **B4** — match a proposed work item to an *accepted* Plan/objective/work
  package, Capability, qualified performer and current authority, and compile an
  immutable reviewable team manifest for **existing** Team Execution.
  **Implemented (`SOURCE_COMPLETE`).**
- **B5** — derive a truthful requester status and accountable next mover from
  existing evidence, and (only under explicit send authority) reply on the
  originating channel. **Implemented (`SOURCE_COMPLETE`); sends nothing.**

Each phase consumes the previous one. None adds a queue, scheduler, dispatcher,
second Registry, universal task database or second authority model.

## 2. Constraints carried forward (non-negotiable)

These are inherited from `AGENTS.md` and the accepted B1/B2 contract and apply
to every phase:

- **Standard library only.** No runtime dependency. The `-S` test run proves the
  core imports without third-party packages.
- **No private values.** No private paths, usernames, tenant names, secrets or
  internal operational values in code, docs or examples.
- **Fail closed.** Required evidence that cannot be read stays `UNKNOWN`;
  corrupt or unreadable state raises and is never rounded up to a valid state.
- **No silent fallback.** An unavailable capability yields explicit
  `UNAVAILABLE` evidence and a nonzero exit, never a substitute.
- **Admission is not work.** Intake records intent only. No B3–B5 phase may
  start, merge, dispatch, schedule or claim on the strength of an intake
  submission alone.
- **No new canonical home and no second ledger.** B3–B5 reuse the private
  intake state, the existing shared Work Registry, the existing team checkpoints
  and the existing daemon. They create no parallel store of authority or truth.
- **Provenance is separate.** An advisory triage result is persisted distinctly
  from the original submission and distinctly from any canonical accepted work.
- **No outbound message without explicit send authorization.** Every outward
  effect is gated, dry-run first, and receipted.
- **Unimplemented commands report structured `UNAVAILABLE` and exit nonzero**,
  never fabricated success.

## 3. Vocabulary and the five dispositions

A triage result is **advisory**. It is not authority, not a work item, not an
approval and not canonical truth.

Exactly **five** dispositions are permitted — no sixth, no free-text variant:

| Disposition | Meaning |
|---|---|
| `ASK_CONTEXT` | More context is required before any grounded position is possible |
| `ANSWER_CANDIDATE` | A grounded answer may exist, but it is **not** self-authorized |
| `PROPOSE_WORK` | The request appears to map to work that must go through B4 |
| `HUMAN_REVIEW` | A human decision is required; no automated disposition is safe |
| `DENY_OR_REPLAN` | The request cannot proceed as stated and must be denied or replanned |

Every disposition carries provenance: which inputs were read, which sources
were current at the evidence cutoff, what was uncertain, and which of the five
labels was chosen and why. `ANSWER_CANDIDATE` is explicitly **grounded but not
self-authorized**: it is a candidate for review, never an answer CAO sends on
its own.

## 4. Phase B3 — on-demand context and intelligent triage

**Status: implemented, `MERGED` and independently accepted.**

**Goal.** Given one admitted submission, assemble a bounded context packet on
demand and produce an advisory triage result with a provenance record.

**Inputs (read-only, permitted, as implemented):**

- the operator's **private B1 submissions**, read through intake's own strict
  reader (`intake.read_submission`) — never the permissive state reader;
- the submission's declared **source references** (`evidence_refs`), with an
  explicit evidence cutoff;
- bounded **default-branch Git evidence** for the supplied repository: the
  resolved default branch, the source commit id and its commit time;
- an existing, **read-only** snapshot of the CAO team summaries and of active
  Registry claims, opened through the Registry's read-only (`mode=ro`)
  connection. No claim, no mutation, no schema creation.

**Deterministic rule table (first match wins).** Rules depend only on the
bounded evidence bundle; none consults anything outside it.

| Order | Condition | Disposition |
|---|---|---|
| 1 | the submission states no request text | `ASK_CONTEXT` |
| 2 | the request text carries a risk marker (directive text, shell payload, reserved action) | `HUMAN_REVIEW` |
| 3 | the request is already implemented at an attributable commit | `DENY_OR_REPLAN` |
| 4 | the submission duplicates an admitted submission with identical request content | `DENY_OR_REPLAN` |
| 5 | no permitted source reference and no current default-branch Git evidence grounds it | `ASK_CONTEXT` |
| 6 | grounded and question-shaped | `ANSWER_CANDIDATE` |
| 7 | grounded and work-shaped | `PROPOSE_WORK` |
| 8 | grounded but ambiguous | `HUMAN_REVIEW` |

A short risk marker forces `HUMAN_REVIEW` rather than a confident wrong
disposition. An empty body is checked first so it can never fall through to a
work or answer disposition.

**Work B3 performs:**

- collects the **missing context** required for a grounded position;
- detects **duplicate** submissions (identical request content under a different
  item id) and requests that are **already implemented** (an operator-supplied
  commit verified in the supplied repository);
- classifies the request and derives advisory **priority, dependencies and
  capability hints** — each `UNKNOWN` unless attributable evidence exists;
- records the **evidence cutoff**, the cited provenance and the remaining
  **uncertainties**, and reports a source read as **stale** when its commit
  predates the requested cutoff rather than treating it as current.

**Output.** One persisted, **provenance-linked private advisory packet**
(`cao.intake.triage.v1`), distinct from the submission and from canonical
accepted work. Its id is a one-way digest of the evidence bundle, so an identical
replay is idempotent; different evidence yields a distinct packet, never a silent
merge. The packet **never carries the submission body**: it holds only ids,
fingerprints, the disposition, the signals, the cited sources and the cutoff.

**Storage.** Packets live under `$XDG_STATE_HOME/cao/intake-triage/`, one file
per packet, `0700` directories and `0600` files, written through the same
validated, atomic, fsynced, descriptor-relative store the admission path uses.
This is a private advisory store, **not** canonical Company State, **not** a
second Registry and **not** a queue.

**Privacy and model qualification.** A qualified AI may perform triage **only
when the selected provider's privacy terms permit the data**. This build holds
no provider credential and performs **no provider egress**, so B3 always yields
the deterministic context packet and records `ai.used = false`,
`ai.egress = "none"`. It does not send the material to an unqualified provider
and does not fabricate an AI result. There is **no automated provider-privacy
classification**: the operator must verify the provider's terms, exactly as
`docs/team-execution.md` already requires for teams. A future AI path stays
behind that gate and keeps the deterministic path as its fallback.

**Explicitly not in B3:** no dispatcher, no scheduler, no second Registry, no
outgoing message, no work claim, no canonical write, no provider egress.

**CLI.** `cao intake triage` is the minimal command surface, justified by the
existing `cao intake` group and the private XDG state:

```sh
cao intake triage --id <submission-id>
cao intake triage --id <submission-id> --repo /path/to/repo --cutoff 2026-10-01
cao intake triage --item-id <item-id> --source-ref <ref-or-commit>
cao intake triage --id <submission-id> --repo /path/to/repo --implemented <commit>
```

Exit codes are `0` success, `2` usage error and `3` explicit
`UNAVAILABLE`/refusal. Any command without a complete owning implementation
reports structured `UNAVAILABLE` and a nonzero exit.

**Tests (synthetic only).** `tests/test_intake_triage.py` is the runnable check
and covers the acceptance list below; `tests/test_boundaries.py` additionally
guards that triage imports no execution/authority concern, reaches the Registry
through read-only calls only, opens no egress surface and uses no shell:

- **unsafe input injection** — an admitted body full of shell metacharacters and
  directive-shaped text changes no behaviour and is treated as opaque data;
- **stale sources** — a source older than the evidence cutoff is reported stale,
  never silently used as current;
- **unauthorized reads** — a context source outside the operator's permission
  fails closed, never returned;
- **ambiguity** — an ambiguous request yields `HUMAN_REVIEW` or `ASK_CONTEXT`,
  never a confident wrong disposition;
- **duplicate** and **already implemented** — detected and reported, not
  re-proposed as new work;
- **replay** — the same submission triaged twice is idempotent; different
  evidence is a distinct packet; a packet id reused for different content is a
  conflict, never a silent merge;
- **privacy** — with an unqualified provider, B3 yields the deterministic
  context packet and sends nothing.

**Acceptance gate.** B3 is accepted only on independent review of the above
tests, the provenance record and the absence of any effect path.

## 5. Phase B4 — governed routing and batch compiler

**Status: implemented, `SOURCE_COMPLETE`; independent acceptance pending.**

**Goal.** Take a B3 `PROPOSE_WORK` (and only that) and route it to an
accountable home, then compile an immutable reviewable manifest for **existing**
Team Execution. B4 **never starts work on intake alone.**

**Implemented surface.** `src/cao/intake_compile.py` and
`cao intake compile --packet-id <packet-id> --route-file <path> [--dry-run]`.
Given one persisted `cao.intake.triage.v1` packet whose disposition is
`PROPOSE_WORK` and a strictly bounded, operator-supplied route evidence file, B4
compiles one immutable, reviewable `cao.team.v1` draft. The draft is always
`proposed`, carries `authority = "none"` and `dispatch = false`, and names an
explicit, distinct owner and reviewer. B4 compiles and records; it starts,
merges, dispatches, schedules, claims, sends and deploys nothing, writes no
Registry row and writes no canonical Company State. The owner must separately
review the draft and run `cao team init --approved`, then authorize dispatch.

**The route file is evidence, not approval.** It supplies only the accepted
Plan/objective/work package, the required Capability, the domain (repository and
path scope), the performer qualification and the current authority. Each
load-bearing claim cites a source document anchored to an exact Git identity
(repository, ref, commit id, committer time, content digest). B4 verifies every
citation with read-only Git evidence — a readable checkout, a ref resolving to
the cited commit id and time, and the document at that commit matching the cited
digest — and the plan acceptance time must equal the immutable plan commit time.
A free-text `accepted: true` or any unverifiable assertion is refused, never
treated as authority.

**Matching.** B4 matches a proposal against:

- the **accepted standing Plan / objective / work package** — `plan.source`;
- the required **Capability** — `capability.source`, with a performer kind;
- the **domain** (repository and path scope) — every task repository must be the
  declared scope checkout and every WRITE path must fall inside the declared
  path scope;
- the **performer qualification** and **current authority** —
  `capability.source` and `authority.source`;
- the **approval digest** — `plan.approval_digest`;
- the **specific owner or reviewer** — a distinct `owner` and `reviewer`, the
  reviewer declared as a manifest role and named by a WRITE task.

**Performer law.** Human, AI, software and hybrid performers are all supported
**through existing law**. A role is **configuration of the work, not
authority**: a role name, a status string or the mere presence of a capability
never grants authority. This mirrors the doctrine's performer-as-configuration
rule and CAO's existing separation of concerns. CAO's existing Team Execution
adapter launches AI specialists only, so a non-AI performer kind is reported
`UNAVAILABLE` rather than compiled into a falsely launchable manifest.

**Fail closed.** Where Plan acceptance, capability qualification, delegated
authority or source access cannot be objectively established, B4 returns an
explicit `NEEDS_HUMAN_REVIEW` (unverifiable plan, capability or authority
evidence; an unreadable shared Work Registry) or `UNAVAILABLE` (a
non-`PROPOSE_WORK` packet; active Registry work overlapping the proposal; an
unsupported performer kind) instead of fabricating a qualified manifest.

**Output.** Immutable, reviewable `cao.team.v1` manifests consumed by the
**existing** CAO Team Execution path. B4 compiles and records; it starts
nothing. Drafts live under `$XDG_STATE_HOME/cao/intake-routes/` (`0700`/`0600`,
written through the same validated, atomic, fsynced, descriptor-relative store
as admission); their id is a one-way digest of the immutable inputs, so an
identical replay is idempotent, different evidence is a distinct draft and a
reused id for different content is a conflict. `--dry-run` creates no state.

**Derived, not duplicated.** Queue position, ownership, escalation and the
separation of **approval from release** are **derived** from existing evidence
(the shared Registry, team checkpoints, plan records). B4 introduces no new
ledger and no new authority.

**Tests (synthetic only).** `tests/test_intake_compile.py` is the runnable
check and covers the acceptance list below; `tests/test_boundaries.py`
additionally guards that B4 imports no execution/authority concern, reaches the
Registry through read-only calls only, opens no egress surface, uses no shell
and never reaches a team execution entry point, a product task, a Registry write
or canonical state:

- **non-`PROPOSE_WORK` refusal** — any other disposition is refused;
- **stale / revoked / unverifiable plan** — a moved ref, a removed document or a
  mismatched digest yields `NEEDS_HUMAN_REVIEW`, never a compiled manifest;
- **no qualified capability** — an unverifiable qualification is refused and an
  unsupported performer kind is `UNAVAILABLE`;
- **missing review or authority** — a manifest without a distinct reviewer, or
  unverifiable authority evidence, is refused;
- **domain / path conflict** — a task outside the scope repository, or a WRITE
  path outside the declared scope, is refused;
- **active Registry overlap** — overlapping registered work is refused;
- **replay / duplicate / conflict** — identical replay is idempotent and a
  reused id for different content is a conflict;
- **prompt injection inert** — hostile route text executes nothing and changes
  no file;
- **wrong tenant / path / symlink** — a symlinked route file and path traversal
  fail closed;
- **zero agent/team/claim effects** — no team, task or Registry state appears;
- **denied unapproved provider** — the draft authorizes no provider.

**Acceptance gate.** B4 is accepted only after B3 acceptance, on independent
review proving that routing is evidence-derived, manifest compilation is
immutable, and no path starts or claims work.

**Prerequisite for a real run.** No accepted Plan/authority record is shipped
in this repository, so a real route file must cite the owner's own committed
plan, qualification and grant evidence; absent that evidence B4 fails closed
with `NEEDS_HUMAN_REVIEW` rather than fabricating a qualified manifest.

## 6. Phase B5 — request to resolution

**Status: implemented in source (`SOURCE_COMPLETE`); independent acceptance
pending. Reads only; sends nothing.**

**Goal.** Give the requester a truthful status and name the accountable **next
mover**, derived from **existing** evidence — never fabricated.

**Implemented surface.** `src/cao/intake_resolution.py` and
`cao intake status --id <submission-id> [--guest-view]`, with the outbound gate
`cao intake reply --id <id> --kind <followup|answer|event> --auth-file <path>
[--dry-run|--mock]`. The projection reuses the existing strict private readers
for the B1 submission, the B3 advisory packet and the B4 draft, reads the
existing CAO team checkpoint and the shared Work Registry read-only, and
persists **nothing at all** (no queue, ledger, index or canonical state), so it
is idempotent by construction.

**Derived status.** Requester status and next mover are computed from existing
Intake, triage, team and Registry evidence, with external PR/review state
reported `UNKNOWN` because it is not locally readable (no client, no network).
The vocabulary truthfully shows **clarification requested**, **work proposed**,
**in progress**, **waiting for review**, **verified answer**, **completion** and
**later outcomes**, without false closure. `VERIFIED_ANSWER`, `COMPLETION` and
`LATER_OUTCOME` are emitted only from an **authenticated** acceptance source;
this build ships none, so it never asserts closure and instead reports the
highest supported status and names the reviewer/operator. A candidate answer, a
`PROPOSED` draft, a `COMPLETE` team summary and any user-supplied status are
never acceptance.

**Channel adapters.** B1/B2 source provides no authenticated delivery identity
and no outbound credential, so an unqualified real send is a structured
`UNAVAILABLE`. Only a deterministic `--dry-run` (no egress) or an explicit
`--mock` run of the **built-in** deterministic simulated adapter (`sent: false`,
`simulation: true`) is served, after a separate operator authorizing event bound
to the **verified** originating recipient/context. The mock pathway accepts
**no** caller-supplied adapter object or callable, so a caller cannot inject a
`send` that reaches the network while the call still reports `sent: false`. The
send id is a deterministic idempotency key; an ambiguous acknowledgement is
`UNAVAILABLE`, never rounded up. B5 builds **no** custom marketplace MCP server,
**no** service, **no** external status API and **no** scheduler; marketplace
policy and any channel credential stay the operator's, external to CAO, exactly
as `docs/intake.md` states.

**Guest view.** The optional guest status view is a **redacted projection** for
a caller a host has **independently authenticated**; it is not served from a
process environment or CLI input. B5 ships no authenticated host connector, so
the CLI `--guest-view` fails closed `UNAVAILABLE` even when
`CAO_HOST_PRINCIPAL` / `CAO_HOST_PRINCIPAL_SUBMISSIONS` are set: those values are
untrusted input, never proof. The pure library `guest_projection` stays
available for a host that *has* authenticated the caller; a submission outside
the proven scope is refused; it writes no index, keeps no second canonical state
and cannot be spoofed by a submission body. Live guest access is not offered.

**Tests to write (synthetic only):** guest and cross-tenant leak; a forged host
environment that still yields a fail-closed `--guest-view`; wrong recipient;
retries / ambiguous send; a caller-injected adapter that never executes; absent
approval; missing evidence; misreported sent/closed; invalid/stale status;
prompt-injection inertness; no new task/work claim; read-only Registry and
private `0700`/`0600`; no egress. These are the runnable check in
`tests/test_intake_resolution.py`, with the read-only and zero-effect boundaries
in `tests/test_boundaries.py`.

**Acceptance gate.** B5 is accepted only after B4 acceptance, on independent
review proving truthful status derivation, gated and idempotent sends, and
projection-only guest access.

## 7. Phase B6 — governed execution handoff

**Status: implemented in source (`SOURCE_COMPLETE`); `PLAN_ONLY` in this build. Independent acceptance pending.**

**Goal.** Close the missing Intake-to-execution bridge: turn an admitted,
read-scoped request into **one** governed, reviewable READ handoff — and nothing
more — from evidence that already exists.

**Implemented surface.** `src/cao/intake_handoff.py` and `cao intake handoff
--id <submission-id> [--repo PATH] [--source-ref REF] [--dry-run] [--inspect]
[--authorize]`. B6 derives an independent **source-attested eligibility result**
from the original admission and its B3 advisory and classifies each reserved
consequential effect (`merge`, `deploy`, `release`, `publish`, `credential`,
`permission`, `destructive`) in opaque request text as `REQUESTED`, `AMBIGUOUS`,
`MENTIONED`, `NEGATED` or `ABSENT`. It handles the case B3 cannot: a *mention* of
a reserved word is not a request, while an explicit effect, or an effect that
cannot be proven non-actionable, is a `HOLD` with a precise reason. A request that
mixes a consequential effect with a read scope (`mixed_effect_and_read_request`)
is **never** downgraded to a READ plan. The immutable B3 packet is bound, never
mutated, and no model decides anything.

**The plan and its gates.** A plan binds the submission id, the B3 packet
fingerprint and disposition, the source ref and HEAD, the role/model/privacy
qualification, the immutable team/task ids and the plan digest, and it records
the operator-granted Registry owner. It is executed **only** through an in-process
authenticated operator-authority adapter; a CLI flag, environment value or JSON
file is caller-controlled and is never proof. This build ships no such adapter,
so every dispatchable plan is `PLAN_ONLY` and names the smallest required
integration. When every gate proves, B6 hands one immutable `cao.team.v1` READ
manifest to the existing CAO team owner API (`team.validate_spec`,
`team.initialize`, `team.set_desired`); it never ticks, and the existing R3/R4
daemon reconciles the team. A hold, a plan and a refusal create no team, no agent
and no Registry claim.

**Preserve, do not duplicate.** An already-started independent READ audit is
linked and preserved, not duplicated. The link is validated from authoritative
evidence (team id resolves, the immutable manifest is entirely READ and cites the
submission), and its status is read from the checkpoint and its claims from the
ledger read-only — never from a caller-supplied `completed` field. Two competing
audits are refused, never collapsed.

**Storage, idempotency, status.** Plans live under
`$XDG_STATE_HOME/cao/intake-handoffs/` (`0700`/`0600`, through the existing
validated private store); the plan id is a one-way digest of the immutable
binding, so replay is idempotent and a reused id for different content is a
conflict. `--dry-run` creates no state, and `--inspect` and `cao intake status`
persist nothing.

**Explicitly not in B6:** no scheduler, no queue, no second ledger, no tick, no
claim, no merge/deploy/permission action, no network call, no credential and no
LLM authority. Real operator authentication, email, Telegram and guest access
remain gated and `UNAVAILABLE`.

**Tests to write (synthetic only):** the classification and the mixed/ambiguous
hold; four redacted-request equivalents; authenticated vs caller-spoofed
operator; immutable B3 fingerprints; stale/drift; competing claims; duplicate and
crash-restart idempotency; a WRITE request never dispatched; no new agent/claim
on a hold; unknown sender/tenant privacy; truthful status. These are the runnable
check in `tests/test_intake_handoff.py`, with the read-only and zero-effect
boundaries in `tests/test_boundaries.py`.

**Acceptance gate.** B6 is accepted only after B5 acceptance, on independent
review proving the eligibility is source-attested, the operator gate is the only
path to a dispatch, and no held, planned or refused handoff has any effect.

## 8. Phase B7 — durable Intake execution queue

**Status: implemented in source (`SOURCE_COMPLETE`); default-disabled. First
dispatch is `HOLD` in this build; an already-approved immutable READ team may be
re-verified and advanced. Independent acceptance pending.**

**Goal.** Give the governed middle a durable, opt-in **queue** without adding a
scheduler, a service, a database, a second ledger, a second Registry or a
credential: remember bounded, prequalified, source-bound READ intents and
reconcile them on the **existing** daemon tick into the **existing** CAO team
owner API.

**Implemented surface.** `src/cao/intake_queue.py`, the configuration key
`daemon.intake_queue` (boolean, default `false`) and the CLI `cao intake queue
enqueue|list|show`. The existing daemon owns reconciliation: when, and only
when, `daemon.intake_queue` is `true`, its own tick calls
`intake_queue.tick`. No new loop, timer, thread, socket or wake path exists.

**Admission is evidence, not authority.** The queue derives the intent from B6:
only a request B6 derives as `ELIGIBLE_READ` with a readable exact source
ref/HEAD is queued; everything else is reported and persists nothing. An intent
records an immutable binding (submission id/fingerprint, B3 packet
id/fingerprint, B6 plan id/digest, source repo/ref/head, the derived immutable
team/task ids) and a deterministic, derived decision/exception receipt. A queue
intent is **never** its own approval.

**Authority.** The only path that creates new work is an **in-process**
authenticated operator-authority adapter (the B6 `operator_authority` contract).
A CLI flag, an environment value or a JSON file is caller-controlled and is
never proof. This build ships no such adapter and the daemon constructs none, so
a first dispatch is held with the exact missing integration. A persisted,
already-approved immutable CAO team bound to an intent may re-verify and advance
PAUSED -> RUNNING only after its approval/spec digest, Intake binding, exact
source HEAD and dependency terminal evidence are all re-validated; the
revalidation is delegated to `team.set_desired`, so a forged manifest or
approval string is never trusted. Any preexisting team (PAUSED, RUNNING or
COMPLETE) is accepted only when its manifest is fully READ, cites the
submission, targets the intent's exact bound repository and carries a persisted
admission that binds that exact manifest; an unapproved, legacy or cross-source
team is held before the owner API is reached, and a COMPLETE label is terminal
evidence only for the intent's exact bound HEAD.

**Gates, isolation and bounds.** A changed Intake binding, a moved HEAD, an
unmet, missing or cyclic dependency, an unreadable Work Registry or an
independent overlapping Registry claim holds **that item only**; every active
Registry claim is compared and the same gates run for every preexisting status,
not only the PAUSED advance. Items are ordered by priority, then age, then id;
the per-tick budget is checked before any side effect, at most
`ADVANCES_PER_TICK` (one) item advances per pass and held items never consume
that budget. Two concurrent passes are serialized by a kernel lock; the loser
reports `LOCKED`. Enqueue's own read/limit/write section is serialized by the
same lock, and a replay that changes the priority or dependencies is refused
deterministically rather than reported as identical. A corrupt record or a
changed content digest fails the whole pass closed. A held, queued or refused
intent creates no team, no agent and no Registry claim.

**Explicitly not in B7.** No scheduler, service, database, second ledger,
second Registry, coordinator, credential, network egress, provider or LLM
authority. No WRITE, new corrective batch, PR/CI/merge/deploy, credential,
permission change or canonical acceptance is ever dispatched by the queue. B7
does **not** claim unattended portfolio management.

**Tests (synthetic only).** `tests/test_intake_queue.py` is the runnable check
and covers forge/replay, tamper and a corrupt private store, a stale source
head, a changed binding, missed/cyclic dependencies, duplicate execution and
crash-restart idempotency, concurrent passes, the absence of any authority
(CLI, environment or JSON) and of any model/privacy qualification, self-blocking
isolation with priority fairness, a bounded budget (including a zero budget that
starts nothing), the auto-advance of an already-approved team and the refusal of
a forged approval, an unapproved, cross-source or drifted-revision team that is
held rather than accepted as progress or terminal evidence, a conflicting claim
beyond any fixed cut, serialized enqueue with a deterministic changed-input
refusal, the daemon opt-in
(default disabled) and zero team/Registry mutation while held;
`tests/test_boundaries.py` additionally guards that the queue reaches the
Registry read-only, opens no egress surface, uses no shell and starts a team
only from its single advance path.

**Acceptance gate.** B7 is accepted only after B6 acceptance, on independent
review proving the intent binds the exact source and evidence, the daemon owns
reconciliation, no queue intent grants authority, and no held, queued or refused
intent has any effect.

## 9. Dependencies and gates

```text
B2 final security approval
  → B3 implementation            [done: SOURCE_COMPLETE]
  → B3 independent acceptance    [done: accepted]
  → B4 implementation            [done: SOURCE_COMPLETE]
  → B4 independent acceptance    [separate recorded gate]
  → B5 implementation            [done: SOURCE_COMPLETE]
  → B5 independent acceptance    [pending]
  → B6 implementation            [done: SOURCE_COMPLETE; PLAN_ONLY]
  → B6 independent acceptance    [pending]
  → B7 implementation            [done: SOURCE_COMPLETE; default-disabled]
  → B7 independent acceptance    [pending]
```

- No phase begins before the **previous gate is accepted**. B3's independent
  acceptance is recorded, so B4 has begun; B5's source is present at the merged
  B4 head and its own independent acceptance is a separate, later state that is
  **not** claimed here. The repository records exactly the states above and
  promotes none of them.
- **Model qualification.** The owner plan names a read/manager model, a writer
  model and a final-read advisory model. Every role is **qualified by provider
  privacy and source scope**, and none is certified suitable for
  private/client/tenant/secret data by the plan alone.
- **Shared Registry and the same daemon.** B3–B5 reuse the one shared Work
  Registry and the existing CAO daemon. No new scheduler, timer or manager.
- **One governed batch/commit per repository**, with the full normal and `-S`
  test suites. Git, CI and release state are verified **separately** — a commit
  is not a merge, a merge is not a deploy, and a deploy is not live acceptance.

## 10. Completion-state vocabulary

Five distinct states, never collapsed:

| State | Means | Does **not** mean |
|---|---|---|
| `SOURCE_COMPLETE` | The source change exists at a head | reviewed, merged or accepted |
| `TEAM_COMPLETE` | The governed team run finished | merged to the default branch |
| `MERGED` | Landed on the target branch | built, deployed or released |
| `DEPLOYED` | Running in the target environment | verified live |
| `LIVE_ACCEPTED` | Independently accepted in operation | permanent or self-maintaining |

Reports must name the exact state reached and must not promote a lower state to
a higher one.

## 11. Pilot and acceptance criteria; smallest first steps

**Pilot posture.** Every phase is piloted **synthetic-only**: invented
submissions, invented repos, invented identities. No real client or private
payload is used in a pilot or a test.

**Smallest first steps (in order):**

1. **B3 read-only context packet** — read one private submission and assemble a
   deterministic context packet with an explicit evidence cutoff. No model.
   **Done.**
2. **B3 deterministic triage** — emit one of the five dispositions from
   deterministic rules, with provenance, no provider. **Done.**
3. **B3 qualified-AI triage** — enable the AI path only behind the provider
   privacy gate; keep the deterministic path as the fallback. **Not started;**
   this build performs no egress.
4. **B4 matcher** — match a proposal to an accepted Plan/work package and refuse
   on stale, revoked, unqualified, missing-review and conflict cases. **Done.**
5. **B4 manifest compiler** — compile one immutable `cao.team.v1` manifest with
   no execution. **Done.**
6. **B5 derived status** — compute requester status and next mover from existing
   evidence only. **Done (`SOURCE_COMPLETE`).**
7. **B5 dry-run adapter** — one mock channel send, gated and receipted, before
   any real adapter. **Done (`SOURCE_COMPLETE`);** real adapters remain
   unavailable, and an unqualified send is `UNAVAILABLE`.
8. **B6 governed handoff** — an independent source-attested eligibility result
   and one gated, idempotent READ handoff plan. **Done (`SOURCE_COMPLETE`);
   `PLAN_ONLY`,** because the authenticated operator identity gate is absent; no
   held, planned or refused handoff has any effect.
9. **B7 durable execution queue** — remember prequalified READ intents and
   reconcile them, opt-in, on the existing daemon tick. **Done
   (`SOURCE_COMPLETE`);** default-disabled, first dispatch held (the
   authenticated operator identity gate is still absent), and an
   already-approved immutable READ team is re-verified before it advances.

**Acceptance criteria per phase:** full normal and `-S` test suites pass; the
phase's tests pass; the foundation CLI smoke passes; `git diff --check` is
clean; an independent reviewer confirms the phase's evidence and the absence of
disallowed effects.

## 12. Doctrine admission gate

Before any future cross-project work-admission doctrine change is made, the
implementation must first rehearse a **synthetic standing-Plan** end to end:
proposal → matched accepted Plan/work package → compiled manifest → derived
status. Only after that rehearsal passes may a doctrine change be proposed.
This roadmap makes **no** doctrine change itself.

## 13. Explicit non-goals

B3–B6 do **not** introduce a universal task database, a new authority concept,
a dispatcher, scheduler or queue, a second Registry or a second ledger, a custom
marketplace MCP server, a presumed external status API, automatic execution on
intake, automatic or unapproved outbound messaging, or any fabricated
completion, sent or closed state. **B7** adds the one deliberate, separately
gated exception: an **opt-in, default-disabled** durable queue of prequalified
READ intents, reconciled by the existing daemon and holding every first dispatch
behind an in-process authenticated operator authority. It introduces no new
authority concept, no scheduler, no second Registry and no second ledger.

## 14. Open questions and research debt

- **Request/event intake remains research debt.** The shape of a generic
  request/event intake beyond the current operator submission is unsettled.
- **Marketplace policy is unresolved** (see `docs/intake.md`): whether and how a
  marketplace app may deliver a document to a local adapter is a platform
  question, and CAO implements the receiving side only.
- **Vendor ROI is unmeasured.** The source page's routing, queue and audit
  claims are vendor statements; none is independently measured here.
- **Legal-specific mechanisms may not transfer.** Rule-based routing to approved
  agents/lawyers, team queues and guest status are legal-work patterns; their
  transfer to generic CAO is a hypothesis to test, not an assumption.
- **Channel credentials stay external.** Any credential a connected adapter
  needs is the operator's, external to CAO, and is never passed into intake.
- **B3 provider path is unbuilt.** The AI triage step stays behind the provider
  privacy gate and is not implemented in this build.

## 15. Provenance

| Item | Source | Standing |
|---|---|---|
| Vendor intake capabilities | official intake product page, reviewed 2026-10-14 | vendor claims, not independently measured |
| B1/B2 admission contract | `docs/intake.md`, `src/cao/intake.py`, `src/cao/intake_bridge.py` | committed; final security acceptance recorded separately |
| B3 advisory triage | `src/cao/intake_triage.py`, `tests/test_intake_triage.py`, `docs/intake.md` | implemented, merged and independently accepted |
| B4 routing compiler | `src/cao/intake_compile.py`, `tests/test_intake_compile.py`, `docs/intake.md` | implemented, `SOURCE_COMPLETE`; acceptance pending |
| B5 request-to-resolution projection and mock gate | `src/cao/intake_resolution.py`, `tests/test_intake_resolution.py`, `docs/intake.md` | implemented, `SOURCE_COMPLETE`; acceptance pending |
| B6 governed execution handoff | `src/cao/intake_handoff.py`, `tests/test_intake_handoff.py`, `docs/intake.md` | implemented, `SOURCE_COMPLETE`; `PLAN_ONLY` (no authenticated operator gate); acceptance pending |
| B7 durable Intake execution queue | `src/cao/intake_queue.py`, `tests/test_intake_queue.py`, `docs/intake.md` | implemented, `SOURCE_COMPLETE`; **opt-in, default-disabled**; first dispatch held (no authenticated operator gate); acceptance pending |
| Team Execution manifest `cao.team.v1` | `docs/team-execution.md` | shipped |
| Performer/authority/evidence semantics | owner's cross-project work-execution contract (owner-referenced v0.4) | owner planning reference; re-verify against canonical home |
| B5 request-to-resolution projection and mock gate | `src/cao/intake_resolution.py`, `tests/test_intake_resolution.py`, `docs/intake.md` | implemented, `SOURCE_COMPLETE`; acceptance pending; the owner plan below is its authority, not operating fact |

## 16. Navigation

- Current shipped intake contract: [`docs/intake.md`](intake.md)
- Team execution contract: [`docs/team-execution.md`](team-execution.md)
- Portfolio admission planning: [`docs/portfolio-admission.md`](portfolio-admission.md)
- Repository overview: [`README.md`](../README.md)
