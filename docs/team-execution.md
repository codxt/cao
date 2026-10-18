# Team Execution

CAO teams run bounded dependency graphs of specialist agents using the existing
CAO task runner, Herdr sessions, one Work Registry and the optional CAO daemon.
No new scheduler, authority ledger, peer chat service or dependency is needed.

## Manifest

A team JSON document contains a unique team_id, a roles object and a tasks
array. Each role has an instruction and may select an exact OpenCode model.
Tasks declare an absolute Git checkout path, role, prompt, mode, optional
dependencies and a precisely scoped WRITE change set. A WRITE task may name a
separate reviewer role with up to three correction rounds.

Every model selection — a role's inline model, a reviewer or an
`agent_profile` — is governed by the installation's own operator-local route
policy. CAO is route-agnostic: it ships no global allowed route set, endorses
no provider, and states no product-wide default naming an operator's selection.
An operator may make its own policy machine-enforced per installation with the
optional CAO product-config `team_model_allowlist` (bounded unique normalized
`provider/model` ids, no `#effort`); its absence preserves existing behavior.
CAO never substitutes a different model when a route is unavailable or out of
quota, and it holds the task `UNAVAILABLE` for an operator decision instead.
When `team_model_allowlist` is configured, every NEW dispatch is additionally
refused with zero Registry claim rows and no workspace or agent effect before
the first effect; an already-claimed worker keeps its exact identity and is
never re-gated. Independently of any allowlist, every NEW OpenCode dispatch
must pass the `mini`-harness qualification below: absence of the allowlist
never permits a bare TUI or an implicit model.

A listed `opencode/` or `opencode-go/` model id binds the dispatch route
identity, not just the model string. A model id alone never proves OpenCode
routing or a paid provider, so a resolved route that lists one must also name
the actual OpenCode agent kind and provider CLI; a resolved route with a
foreign, spoofed or missing agent kind or provider CLI is refused
(`TEAM_ROUTE_IDENTITY_MISMATCH`) instead of dispatching on another runtime,
even when the provider model and the verified argv `--model` look allowed.

An OpenCode route must launch CAO's non-interactive `mini` harness naming
exactly one explicit, safe `--model`, with the optional `--standalone` switch
CAO itself emits. No `--agent` (or any other option) is separately qualified in
this build, so it is refused explicitly rather than admitted through an invented
allowlist. A bare OpenCode launch — a role model, an inherited provider or an
`agent_profile` whose arguments omit `mini` or the model, or carry an unknown
option — would open the interactive TUI at its own default model, so it is
refused (`TEAM_ROUTE_TUI_UNQUALIFIED`, or `TEAM_MODEL_UNRESOLVED` for a missing,
divergent or ambiguous model) before any Registry claim or pane effect. This
qualification holds whether or not `team_model_allowlist` is configured, for
both team and direct product tasks. The explicit model is always the selection
the harness passes, and no unqualified or silent default is ever substituted.
Native Codex routes and their existing READ/WRITE handling are unchanged.

A listed `codex/` model id binds a Codex agent-profile route the same way: a
native `--model` token resolves to the canonical allowlist identity
`<provider>/<model>` (for example a native `--model example-model` under
`agent_kind`/`provider.cli` `codex` is `codex/example-model`), the resolved agent
kind and provider CLI must both be `codex`, and exactly one unambiguous
`--model` is required. A repeated, flag-trapped, divergent or unlisted
selection is unresolved and refused. Native Codex WRITE is permitted only for the exact `codex/gpt-6.1-sol` and
`codex/gpt-6-luna` identities when the task explicitly declares WRITE, the
separate operator-local profile supplies precisely `--model <id> -c
model_reasoning_effort=<medium|high> --sandbox workspace-write -c
approval_policy=on-request` and CAO acquires its normal scoped Work Registry
WRITE claim. All other Codex WRITE requests or weak/unsafe launch arguments
remain refused (`TEAM_ROUTE_READ_ONLY` or `TEAM_ROUTE_WRITE_UNQUALIFIED`);
READ profiles do not grant WRITE. An allowlist entry is not itself a grant.
Publication, deployment, credentials, destructive actions and canonical
acceptance remain separately governed. Medium/High variants share a base
identity; unqualified model substitutions never occur.

A defensive, model-specific READ-only gate additionally covers the exact
Step-5 Preview Free base model id (`opencode/step-5-preview-free`), reached
through its canonical identity, the bare `--model` token, a non-canonical
spelling, a foreign provider prefix or a bounded `#effort` variant: it is
refused for a WRITE task (`TEAM_ROUTE_READ_ONLY`) before any Registry claim or
agent effect, so an alias never restores write access. It is a legacy defensive
gate for that one base id, not a product-wide route policy; a distinct id such
as `opencode-go/step-5-preview` is a different identity, is never treated as an
alias of it, and its availability is unknown per installation. Every other
route keeps both modes.

An allowlisted model id whose provider is known not to offer
zero-data-retention is refused at every NEW dispatch
(`TEAM_ROUTE_PRIVACY_UNVERIFIED`), because this build cannot technically prove
that a checkout or prompt is clean public OSS or sanitized. See the
non-zero-data-retention note in docs/automatic-recovery.md; never route private,
client, tenant or secret material to such a route.

Example (replace checkout paths and illustrative model identifiers):

    {
      "schema": "cao.team.v1",
      "team_id": "sample-project",
      "max_writers": 2,
      "max_parallel": 4,
      "roles": {
        "planner": {
          "instruction": "Inspect requirements and propose an approach.",
          "model": "opencode/selected-planning-model"
        },
        "developer": {
          "instruction": "Make scoped changes and commit them locally.",
          "model": "opencode/selected-coding-model"
        },
        "reviewer": {
          "instruction": "Independently review the exact Git HEAD.",
          "model": "opencode/selected-review-model"
        }
      },
      "tasks": [
        {
          "id": "requirements",
          "role": "planner",
          "repo": "/absolute/path/to/read-checkout",
          "mode": "read",
          "prompt": "Inspect requirements and identify assumptions."
        },
        {
          "id": "implementation",
          "role": "developer",
          "repo": "/absolute/path/to/write-checkout",
          "mode": "write",
          "write_scope": ["src/module.py", "tests/test_module.py"],
          "depends_on": ["requirements"],
          "prompt": "Implement and test the accepted change.",
          "reviewer": "reviewer",
          "max_corrections": 1
        }
      ]
    }

Team identities and graphs are immutable once initialized. Start a separate
governed team for a changed scope. Choose models deliberately, verify live
provider availability, pricing and privacy. No manifest field certifies that
a provider is suitable for private/client/tenant/secret data.

## Operator commands

Use the source product launcher (./cao) or the installed cao command. The
separate read-only operator CLI (python -m cao) does not start agents.

    cao doctor
    cao team validate --file /path/to/team.json
    cao team init --file /path/to/team.json
    cao team status --id sample-project
    cao team start --id sample-project
    cao team tick --id sample-project
    cao team status --id sample-project
    cao team list
    cao team pause --id sample-project

When a READ specialist's final result receipt is wrong, do not accept a
near-miss marker or repeatedly resume the unchanged response. Inspect the
original task output and Registry claim, pause the team, then stop only the
original CAO-owned READ task via `cao stop --id <execution-id>`. Once CAO
confirms pane closure and Registry abandon-release, an operator can request
`cao team retry-read --id sample-project --task requirements` (replace with
the affected READ task). This admits **one** new execution identity and
challenge, preserving the original task record. It does not start an agent;
review role/provider privacy and restart the team separately. WRITE work and
other UNKNOWN failure classes are ineligible. The old result is never
silently accepted, and an invalid second receipt remains blocked.

Init stores the manifest and defaults PAUSED; it launches nothing. Start sets
the durable desired state RUNNING, without itself starting a worker. Tick
dispatches ready tasks and observes running ones. For continuing unattended
ticks, use the existing CAO daemon (cao daemon start); no other manager/timer
is needed. Team-owned product tasks are reconciled **only** by the team loop.
The daemon resolves ownership from validated durable team checkpoints: an
execution ID must be referenced by one team and its run ID must agree.
The older single-task recovery loop and progress relay skip those verified
team-owned tasks, so they cannot bypass the team result receipt or release
a claim prematurely. Missing, conflicting or corrupted team ownership is
held UNKNOWN with an explicit reason; a matching name alone is not authority
and is never silently routed to the generic recovery loop. Earlier work,
review and correction execution records remain team-owned after a phase
transition. Terminal historical stages are recorded without false UNKNOWN
holds; unexpectedly non-terminal historical stages and stages not yet launched
hold for inspection. Ordinary product tasks retain their original recovery
path. Doctor READY alone
is not proof of a qualified private provider or autonomous Factory operator.

State is stored privately under the XDG CAO state root in teams/team_id.json
(mode 0600). A shared kernel lock serializes CAO team dispatches. A crash
releases that lock but preserves exact task execution IDs and prompts, so
resumption does not blindly resubmit an already issued prompt.

## Verified terminal results

A prompt being accepted does not mean the work is complete, and neither does a
bare IDLE. The managed runtime distinguishes an IDLE pane -- ready for input
after the focused tab has been seen -- from a DONE pane, which is the same
underlying settled completion observed while the work finished in an unseen
background tab; focusing a finished tab turns DONE into IDLE. Team Execution's
product adapter therefore waits on both and keeps the original task and claim
RUNNING until the specialist's actual output contains this execution's exact,
task-bound receipt in a durable **post-prompt settled** capture. A team IDLE is
only a candidate: it is admitted solely when that receipt -- and, for a review,
the exact single verdict and reviewed HEAD -- verifies. An idle without a
verified result, including the transient idle a runtime may report immediately
after prompt acknowledgement before any work, remains RUNNING with its claim
held: never falsely complete and never UNKNOWN. A DONE without a receipt is
terminal but unverified and holds UNKNOWN rather than unblocking dependents. An
ordinary product task carries no receipt to verify and is never completed by a
bare idle. The prompt contains a 16-character nonce on a separate line but not
the completed receipt; the specialist copies the nonce (no calculation) after
CAO_TEAM_RESULT: on a standalone line only after successful work. Legacy
checkpointed prompts that explicitly required reversal continue to be checked
against their original contract.

A new task's first prompt is submitted with the managed runtime's native
bounded wait: `agent prompt --wait` with explicit `--until working`, `--until
blocked` and `--until done`, bounded by `task.prompt_timeout_ms`. A bare
`agent_prompted` acknowledgement only proves that the terminal accepted a
write; it does not prove the runtime consumed the prompt. CAO records the
prompt as accepted only after the driver reports observed activity. A stall,
timeout, or acknowledgement without observed activity leaves the durable
`prompt_issued` checkpoint and the original claim in place, records the prompt
as ambiguous and the task `UNKNOWN`, and never re-sends the prompt, injects an
automatic Enter, or creates a second Registry claim.

The receipt proves that a post-prompt response was observed; it is not a
security signature, approval, guarantee of correctness, or proof that tests
passed. Repository, Registry, scope, commit, review, human approval, and
release gates remain independently required. A provider credit denial,
transport failure or model output without the receipt must be investigated
rather than retried under a new task identity.

### One READ completion protocol

All CAO READ executions complete through one ordered protocol. This applies to
an ordinary product READ and to a team work or review READ alike:

1. CAO observes a **post-prompt settled** state -- `done`, or an `idle` that
   carries this execution's verified result (never a pre-response IDLE).
2. CAO saves the bounded terminal observation durably.
3. CAO verifies the exact task receipt (`CAO_TEAM_RESULT:`) and, for a review,
   the exact single verdict and reviewed HEAD, against that durable terminal
   evidence. A team READ passes this verification as a completion gate, so an
   unverified output, nonce or review verdict is decided **before** any closing.
4. CAO persists the verified terminal settlement durably, bound to this task's
   exact claim, repository, run and owned pane. This happens after verification
   and **before the first pane-close attempt**, so a crash during closure can
   neither lose the settled receipt nor strand the still-open claim: a later run
   reconciles only its own recorded settlement, re-proves the result and the
   original claim, and releases exactly once.
5. CAO closes only its own exact pane -- its own workspace and pane identity
   must match the durable checkpoint, and a foreign occupant is refused -- and
   then verifies the pane is actually gone.
6. Only when 2-5 hold does CAO release the Registry claim and mark the task
   complete.

A failure at any step holds the claim and the pane; an unverified DONE (or a
closed recovery that cannot be re-verified) holds UNKNOWN, while an unverified
IDLE is only an early idle and stays RUNNING. Nothing is released
on a done receipt alone. CAO never scans or signals an arbitrary process, never
force-unlocks the Registry and never closes a pane it cannot prove is its own.
Pane absence is the only closure CAO claims: an OS process exit it cannot prove
through a stable recorded identity is reported as unverified, not asserted.
Historical records written before this protocol may still hold a terminal pane.
The opt-in, default-off daemon hygiene pass below is gated and default
plan-only: an unexpected reappearance of a pane after its recorded verified
close is held `protected_unknown`, while a corroborated historical record (the
pre-close-first build released its claim without closing, so no close is
recorded) is `eligible` only when the exact RELEASED Work Registry row, the
task's own exact workspace/pane and its stopped uniquely named worker all
corroborate it. Exact pane existence is probed first: a genuinely absent pane is
skipped before any team/lane, checkout or Registry test, so an unrelated
unprovable predicate never inflates `protected_unknown`, while an unreadable
runtime or pane still holds and an unreadable ledger is reported unavailable
without ever granting a close. Each terminal pane gets one final
classification per tick. Immediately before a close the pass re-reads the task
record, reuse conflicts, team acceptance, checkout state, the full Registry row
and the pane's own worker, so a task or worker that changed after the first
check is never knowingly closed. It closes that exact pane only when an operator
enables both switches locally; source never changes the defaults. Close a
specific historical pane manually with the runtime's own close primitive until
an installation's own plan run proves a reviewed eligible cohort.

The accepted disposable empty-pane and native stopped named-worker canaries, run
against a synthetic legacy release ordering in an isolated Registry, prove this
closure mechanism -- a single close, the durable receipt and idempotence across a
restart -- and a positive accepted-team regression proves the team acceptance
gate in a fake driver only. That is technical qualification, not operating
authority: it does not by itself authorize enabling the effect. An operator must
still independently verify a live eligible pre-close-first cohort and its exact
released Registry rows, workspace and pane in that installation before clearing
the dry run, and a plan that reports no eligible candidate remains plan-only even
after a successful canary. An IDLE worker and an unaccepted team are never
eligible; `protected_active` and `protected_unknown` are held and reported
separately. Preemptive activation with no eligible cohort is not accepted
behavior; it would require a separate, explicit policy revision.

Team task agent names use a readable prefix and digest of their full ID.
This prevents longer team/task names from colliding after terminal name-length
truncation. The original task record remains the idempotency authority.

## Handoffs, review and correction

A task starts only after every declared dependency completes. Its prompt
receives accepted predecessor Git HEADs and bounded outputs explicitly
labeled untrusted. A scoped WRITE verifies its actual committed Git changes,
exact Registry release and worker shutdown through the existing task runner.

A reviewer is a separate READ agent against the latest accepted source HEAD.
Its route is governed by the installation's own operator-local policy (the
optional `team_model_allowlist`), and no other model or automatic fallback
model is dispatched in its place. It must report findings and exactly one of
these
lines:

    CAO_REVIEW_VERDICT: APPROVED
    CAO_REVIEW_VERDICT: CHANGES_REQUESTED

It must also report the exact reviewed Git SHA in one line:

    CAO_REVIEWED_HEAD: 0123456789012345678901234567890123456789

CAO compares the SHA with the work result and current checkout HEAD. A change
request triggers a new, separately identified, bounded correction task in
the same authorized WRITE scope and a fresh review. Missing/ambiguous markers,
stale source, provider failure and correction-budget exhaustion hold UNKNOWN
or BLOCKED, never implicit approval.

A long review answer can exceed the task's bounded capture. product_task keeps
the **terminal suffix** of a runtime read and states truthfully, in the durable
task record, that it kept the tail, exactly how many characters it dropped, and
how many it captured. Such a bounded capture may decide a review **only** when
all of these hold: the record itself says it kept the tail; the window is
exactly sized and equals the stored text; the reviewed source HEAD still
matches; and the exact verdict plus this task's own completion nonce are both
present in what was kept. A head-only window, an unsized window, a capture of
unknown provenance, and a missing or forged nonce all hold UNKNOWN with
`REVIEW_OUTPUT_BOUND_UNVERIFIED`, because the capture itself is not a provable
tail. A provably bounded tail that still carries this task's nonce but yields
no unambiguous verdict, names a different reviewed HEAD, or repeats the verdict
holds UNKNOWN with `REVIEW_VERDICT_UNVERIFIED`. A verdict whose reviewed HEAD no
longer matches the checkout holds BLOCKED with `REVIEW_HEAD_DRIFT`. The bounds
are CAO's own recorded provenance, never a value supplied by the answer being
judged.

The findings and verdict sit at the **end** of a clipped capture, so correction
feedback and every later handoff take the newest 5,000 characters of the kept
text, never the oldest part of the retained window.

Both directions of a scrolling pane are now retained. `product_task` keeps each
bounded read as its own provenance-tagged observation -- its kept/limit/
captured/dropped sizes, the capture timestamp and the prompt/stage provenance
current when it was read -- alongside the primary capture. A shorter later read
that newly carries the receipt is therefore evaluated instead of discarded, and
an earlier receipt an already-proved read supplied is never lost when a longer
later read arrives. A receipt is accepted only from a **post-prompt terminal**
observation whose text is exactly the window it claims and carries this task's
exact nonce; a pre-prompt or provenance-unknown window, a prompt echo, a changed
nonce and another task's receipt are all refused. See
docs/automatic-recovery.md for the reviewer and recovery policy this contract
implements.

Reviewer verdicts are workflow advisory, not deployment/publication/credential
authority and not independent proof of correctness. Final acceptance still
requires an accountable operator to inspect tests, CI, the reviewed diff,
security, operating state and authorization.

## Parallelism, privacy and authority

max_parallel bounds active agents for one team (default 4, hard maximum 8).
max_writers bounds this team's **own** simultaneous WRITE slots (default and
hard maximum 2), counting a durable WRITE intent from the moment it is
dispatched, before its first Registry claim exists. READ tasks and reviews
consume no writer slots. Foreign WRITE claims from other CAO teams or non-team
claimers do **not** consume this team's writer cap; they are still honoured as
checkout conflicts, and an EXCLUSIVE claim refuses team dispatch outright.
The global CAO team lock serializes admissions; live Registry preflight and
atomic claim still decide actual repository/path/branch permission. Separate
Git worktrees are required for truly independent concurrent writers. Do not
write to a checkout while its reviewer is inspecting that HEAD.

### Verified intermediate corrections

When a reviewer explicitly requests changes on a scoped WRITE whose manifest
allows correction rounds, the first committed version will generally remain
ahead of the remote branch until the entire team finishes. The existing Registry
will correctly flag that commit as historical work and refuse an unexplained
second WRITE. CAO handles this narrow case using the existing Registry's
fingerprint-bound historical-disposition contract. Before recording it, CAO
requires all of: the previous **same-team, same-task** WRITE was completed and
released at the exact current HEAD; its worker pane was closed; its Registry
claim and scope agree; the repository is clean; no live conflicting agent is
seen; and the complete committed diff lies inside the authorized scope. Only
then may the new correction attempt the ordinary Registry preflight and claim.

The disposition is explicitly attributed as an automated CAO-team decision,
not a human approval. The operator-authorized immutable team manifest must
declare the reviewer and a nonzero correction budget to permit this continuation.
The original team's base HEAD must match the Git merge base, and all intervening
correction claims must form an uninterrupted released-HEAD lineage. CAO rejects
a foreign intermediate commit even if it happens to touch an authorized file.
An unknown, dirty or out-of-scope finding is never cleared. The normal Registry
preflight and atomic WRITE claim remain mandatory. No disposition grants a
new scope, credential, push, merge or deployment right.

Set max_corrections to zero (or omit it) when automatic historical
reconciliation is not desired; then CAO never starts a correction task.
If GitHub has caught up to the intermediate HEAD, no historical waiver is
necessary. Missing or contradictory evidence holds UNKNOWN for operator
inspection. New work on another task/worktree is not silently labeled
historical. The disposition is attributable and fingerprint-bound in the
existing shared Registry; there is no second ledger.

There is no automated provider privacy classification. Operators must verify
the selected model's terms before using it on private or sensitive data.
A free model is not a default destination for confidential material. A
permitted route's quota is a cost boundary, not a permission: exhausting it
holds the task `UNAVAILABLE` for an operator decision, and neither an unlisted
model nor a fallback provider is dispatched to work around it.
Teams cannot publish, deploy, grant permissions, modify credentials, create
production resources, or override repository governance by implication.

## Recovery and limits

To re-check an UNKNOWN task after inspecting its exact pane, prompt state and
Registry claim, use:

    cao team resume --id sample-project --task implementation

This resets only the stored UNKNOWN observation to RUNNING. It preserves
the **same execution ID and prompt** and does not force a fresh model call.

### One bounded READ retry after an invalid receipt

A READ attempt that produced an invalid terminal receipt
(`TEAM_RESULT_UNVERIFIED` or `TEAM_DURABLE_RESULT_UNVERIFIED`) can be retried
once, after an operator has independently stopped the original worker:

    cao team retry-read --id sample-project --task review

The retry is admitted only when the original attempt's own durable evidence
proves all of: it is a READ execution; it is `stopped`; its CAO-owned pane was
closed and recorded; its claim was released `abandon`; and the shared Registry
holds exactly that released READ claim for this team, work key and owner. This
covers a failed **review** attempt, including a review of a correction round:
such a retry still reviews the same accepted source revision, with the same
work handoff and the same review findings, because those are retained. The
failed attempt's record, receipt and nonce stay durable and untouched. The new
attempt receives a new execution identity and its own copy-only nonce, so it
can never adopt the stopped agent or its released claim. A retried review that
still requests changes opens a new, separately identified correction round on
the same accepted source revision; the correction is not a retry, and its
stored round resets the one-time retry budget. A WRITE attempt is never
retryable in any phase, and a second retry is refused.

### Bounded stalled task handling

Every dispatched task records durable start and progress stamps. Progress is a
fingerprint of what the task observably produced — its durable status, its
bounded captured output, its pane handback and its last observation — so a tick
that merely rewrites a record is never mistaken for progress.

A task is reported stalled (`TEAM_TASK_STALLED`, status UNKNOWN) only when all
of these hold: its durable record is not terminal; no observable progress has
been recorded for longer than the bound; and the runtime can no longer report
its worker. A worker the runtime still reports as present keeps its bound
however long model thinking takes, so a long authorized computation is never
reported as a stall. A record with no stamps at all is never reported stalled
on the strength of a missing value.

The bound is `task.team_stall_timeout_ms` (default two hours, configurable
between one minute and seven days, validated fail-closed). It is always
measured from the last durable observation, so evidence discovered during a
tick cannot quietly reset a bound that had already expired.

Reporting a stall never releases a claim, never replaces or re-prompts an
agent and never fabricates an identity. The exact existing execution identity,
prompt and claim are retained for the operator, who resolves the runtime and
then uses `cao team resume` (or the bounded retry above where it applies).

### Bounded automatic READ recovery (R3)

Each tick classifies every UNKNOWN team task once from durable evidence and
records a bounded decision in the existing team checkpoint: a classification, a
decision, a reason, the next required action and provenance (the task ID, the
source HEAD, the Registry claim ID, the nonce-receipt state and a timestamp).
The receipt is idempotent — an unchanged snapshot keeps its original
fingerprint and capture stamp — so a restart does not rewrite or re-apply it. A
running team that still holds an UNKNOWN slot is ticked for this classification;
the daemon reports each decision and applies none of them itself. No second
scheduler, ledger, database or observer is added.

Exactly one transition is automatic: **one bounded resume of the SAME original
UNKNOWN READ execution**. It is admitted only when all of these are proven from
durable evidence: the failure is a recoverable READ transport failure (not a
lost receipt); the original execution identity, prompt and agent/pane
checkpoint are intact; the runtime is available and still reports the original
worker; the shared Registry holds the exact open READ claim (claim ID, owner,
mode, run and work key) that the durable record names; and the pinned Git HEAD
and branch are unchanged. The resume reuses `cao team resume`'s transition and
`product_task`'s own idempotent reconciliation under the same dispatch lock, so
it never starts a replacement agent, replays an issued prompt or creates a new
claim. At most one resume is admitted per original execution identity, so an
unresolved failure is reported rather than looped.

Everything else holds or blocks with a reported reason. A lost terminal receipt
holds for `cao team retry-read` after an operator stops the original worker. A
Registry conflict, an unreadable ledger, a Git HEAD or branch drift, a platform
or tool-layer denial, an interactive provider/route gate and a pending
exact-head PR CI gate all hold. An UNKNOWN WRITE — including a
`SOURCE_COMPLETE`-but-unverified WRITE at an already committed head — is never
automatically replayed, retried, stopped, released, merged or published, and its
claimed worktree is never mutated. `cao team resume` and `cao team retry-read`
remain the explicit operator paths.

### Crash recovery between the Registry claim and the record save

A product task persists its **acquisition intent** — run, work key,
repository, mode, normalized scope, branch and the exact precrash HEAD — to
its own record *before* the durable Registry claim. If the process then dies
between the claim and the record save, the next run can recognize the OPEN row
as this task's own instead of leaving an operator staring at an invisible claim.

Adoption still requires positive proof that the row is the same attempt: exact
run, work key, owner, mode, repository, registered scope, branch, worktree and
identity, plus a `claimed_at` no older than the task record. For WRITE it also
requires that the checkout still sits exactly on the recorded precrash baseline
and is clean. A post-acquisition commit, a dirty checkout, an unverifiable
checkout, a tampered intent, a mismatched scope or a row that belongs to
something else are refused with `NEEDS_RECONCILIATION` and a precise reason.
The claim is preserved, never released, and never replaced by a second claim.
A WRITE claim that exists without any provable intent keeps the original
refusal rather than being adopted.

Unknown/blocked task states remain held for explicit investigation, with the
original execution identity retained. Pausing a team only stops new dispatch:
it does not terminate already running workers. Use the existing task stop
command on an identified worker and preserve the original code/worktree first.
No automatic replacement agent or hidden scope expansion is performed.

Team v1 uses declared, finite DAGs and artifact-based coordination; it does not
implement unrestricted agent-to-agent conversation or agent-generated new
permissions/scopes. Use the current task record, shared Registry and durable
team checkpoint to recover an interrupted run without replaying work.

A stale Registry claim is never released by age, by a lease or by CAO guessing
its owner: an unknown or merely old claim keeps its ownership until an operator
reconciles it, and team recovery never fabricates a release to get past it.

## Portable agent profiles (opt-in)

CAO Teams supports runtime-neutral, per-role `agent_profile` references.
Operators configure profiles in their own private CAO product configuration
(`~/.config/cao/config.json` or `XDG_CONFIG_HOME/cao/config.json`). A
profile chooses an agent kind supported by the installed runtime plugin and
explicit read/write argument vectors. CAO does not own subscription login,
credential storage, or hardcoded model catalogs.

Example local configuration fragment (merge into the existing config rather
than replacing its registry, provider or managed fields):

```json
{
  "agent_profiles": {
    "claude_review": {
      "agent_kind": "claude",
      "read_args": ["--model", "sonnet", "--effort", "high", "--permission-mode", "plan"]
    },
    "codex_review": {
      "agent_kind": "codex",
      "read_args": ["--model", "gpt-example", "-c",
                    "model_reasoning_effort=medium", "--sandbox", "read-only"]
    },
    "gemini_read": {"agent_kind": "gemini", "read_args": []}
  }
}
```

Reference a profile in an immutable team role:

```json
{"roles": {"reviewer": {"instruction": "Review the exact commit", "agent_profile": "claude_review"}}}
```

The examples illustrate CLI syntax, not qualification of a particular
model, account, runtime integration, permission setting or data class.
Operators must validate exact argument support against their installed
agent versions, provider terms, and permitted data scope before execution.
Supported agent kinds and arguments belong to the installed runtime plugin.
CAO passes argument arrays directly to the existing runtime; it never uses
a shell to expand or interpret them.

A profile must declare the selected task mode. Missing profiles, unknown
fields, unsafe argument strings, unsupported modes, or collisions between a
role's inline OpenCode model and a profile fail closed. Reviewer tasks always
resolve `read_args`; a read profile never inherits `write_args`.
Current inline OpenCode model routing and no-profile team behavior remain
unchanged, and an OpenCode profile is subject to the `mini`-harness
requirement above whether or not a `team_model_allowlist` is configured.
Profile names, launch arguments and backend choices are operator configuration,
not tenant state or operator identity proofs.

The existing CAO daemon, Herdr adapter, Work Registry, task identities,
review receipts, and independent repository/release authorization remain
unchanged. The public repository ships no authentication tokens or
hardcoded personal models. An operator may keep local credential policies
for subscriptions, API providers or enterprise access entirely outside CAO.

## Team composition approval

Agree on roles, models, tasks and limits with the operator in chat.
If an installation wants an explicit confirmation, configure:
`"team_approval": {"mode": "chat"}`.

```sh
cao team plan --file team.json
cao team init --file team.json --approved
cao team start --id team-id
```

The flag records the caller's declaration of chat approval. CAO cannot
verify the conversation or authenticate a person; it uses no signing keys,
identity service or approval files. The immutable team and the exact
model/provider selection each task would dispatch are retained so later
changes cannot silently alter dispatch.

`cao team plan` is read-only. Alongside the declared task graph it shows the
resolved work/review route each task would dispatch — the agent kind, the
provider CLI/id and the effective model — and, in chat mode, the exact
`spec_sha256` and `routes_sha256` that a following `cao team init --approved`
would save, so an operator can confirm the digests before approving. Each route
also reports `model_allowed`: the verdict the configured `team_model_allowlist` and the
unconditional OpenCode harness qualification would apply at dispatch (a
non-OpenCode route with no allowlist is truthfully `true`, while an unqualified
OpenCode route reads `false` even without an allowlist), including the OpenCode
route-identity binding so a foreign or spoofed agent kind or provider CLI reads
as denied on the plan exactly as it refuses at dispatch. Roles
are shown only by their safe declared selection (an inline model or an
`agent_profile`); raw role instructions, raw argument vectors, prompts,
environment and configured document text are never emitted. The provider
CLI/id and model fields shown are operator-declared identifier values, so an
operator must keep them non-secret; each is emitted only when it has a bounded
identifier shape and the provider entry and any argv `--model` agree, and a
divergent, repeated, flag-like or otherwise unqualified selection is reported
unresolved instead of echoed. An unknown profile or unresolvable route fails
closed with a nonzero exit, and a missing or unavailable product configuration
is refused rather than guessed. Legacy approval mode reports truthful null
digests and claims no chat-confirmation authority.

That binding is the resolved selection an agent actually receives — the role
model or agent profile of every task and phase — and not the surrounding
configuration document, so a registry path, runtime name or profile no role
uses cannot invalidate a team. It is re-verified before any eligible
first effect, including a crash-recovered RUNNING intent with no acquired claim.
Capacity-blocked slots are not falsely labeled as approval failures. A
configuration change stops the team from starting more work — the
held slot reports `TEAM_ADMISSION_CHANGED` — while tasks that are already
running keep their exact execution identity and are still reconciled, released
and reported. Restoring the approved effective route releases the held slot on
the next tick. No command in this section is publication, deployment or release
authority.

A team recorded before chat approval was required can be re-approved on its
identical immutable manifest with the same `cao team init --file team.json
--approved`. Only a team that never dispatched anything — no execution
identity, no Registry claim — qualifies, so no successor team is created for
an unused manifest; a team that already ran keeps its existing recovery paths.

Reuse a team for one initiative. A separately approved successor identifies
its predecessor with `supersedes` and gives a short `change_reason`; the old
team must be paused and terminal. Existing teams and Registry claims stay
intact. Public installations without this optional chat policy retain the
existing initialization behavior.

A paused predecessor that **never dispatched anything** — no execution identity
in any slot, no Registry claim of any outcome, and unreadable Registry evidence
still fails closed — may instead be replaced by an approved successor, because
it started no work and holds no authority. That successor is a genuinely
changed immutable manifest admitted under its own ID, run, admission and
execution identities; nothing of the predecessor's identity or authority is
reused, and re-admitting the identical manifest is still re-approved in place
rather than replaced. Any predecessor that dispatched work or holds a claim
keeps the full terminal and Registry checks above.
