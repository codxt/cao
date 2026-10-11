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

A prompt being accepted and the model being IDLE does not mean the work
is complete. Team Execution's product adapter keeps the original task and
claim RUNNING until the runtime reports post-prompt DONE and the specialist's
actual output contains a task-specific receipt. The prompt contains a
16-character nonce on a separate line but not the completed receipt; the
specialist copies the nonce (no calculation) after CAO_TEAM_RESULT: on a
standalone line only after successful work. Legacy checkpointed prompts that
explicitly required reversal continue to be checked against their original
contract. An unreadable or missing receipt holds UNKNOWN
instead of unblocking dependents. The single-task core also no longer
accepts a pre-response IDLE state as completion.

The receipt proves that a post-prompt response was observed; it is not a
security signature, approval, guarantee of correctness, or proof that tests
passed. Repository, Registry, scope, commit, review, human approval, and
release gates remain independently required. A provider credit denial,
transport failure or model output without the receipt must be investigated
rather than retried under a new task identity.

Team task agent names use a readable prefix and digest of their full ID.
This prevents longer team/task names from colliding after terminal name-length
truncation. The original task record remains the idempotency authority.

## Handoffs, review and correction

A task starts only after every declared dependency completes. Its prompt
receives accepted predecessor Git HEADs and bounded outputs explicitly
labeled untrusted. A scoped WRITE verifies its actual committed Git changes,
exact Registry release and worker shutdown through the existing task runner.

A reviewer is a separate READ agent against the latest accepted source HEAD.
It must report findings and exactly one of these lines:

    CAO_REVIEW_VERDICT: APPROVED
    CAO_REVIEW_VERDICT: CHANGES_REQUESTED

It must also report the exact reviewed Git SHA in one line:

    CAO_REVIEWED_HEAD: 0123456789012345678901234567890123456789

CAO compares the SHA with the work result and current checkout HEAD. A change
request triggers a new, separately identified, bounded correction task in
the same authorized WRITE scope and a fresh review. Missing/ambiguous markers,
stale source, provider failure and correction-budget exhaustion hold UNKNOWN
or BLOCKED, never implicit approval. Outputs truncated before validation
cannot approve review.

Reviewer verdicts are workflow advisory, not deployment/publication/credential
authority and not independent proof of correctness. Final acceptance still
requires an accountable operator to inspect tests, CI, the reviewed diff,
security, operating state and authorization.

## Parallelism, privacy and authority

max_parallel bounds active agents for one team (default 4, hard maximum 8).
max_writers bounds total active WRITE claims observed in the same authoritative
Work Registry (default and hard maximum 2). READ tasks consume no writer slots.
Existing WRITE claims count; an EXCLUSIVE claim refuses team dispatch.
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
A free model is not a default destination for confidential material.
Teams cannot publish, deploy, grant permissions, modify credentials, create
production resources, or override repository governance by implication.

## Recovery and limits

To re-check an UNKNOWN task after inspecting its exact pane, prompt state and
Registry claim, use:

    cao team resume --id sample-project --task implementation

This resets only the stored UNKNOWN observation to RUNNING. It preserves
the **same execution ID and prompt** and does not force a fresh model call.

Unknown/blocked task states remain held for explicit investigation, with the
original execution identity retained. Pausing a team only stops new dispatch:
it does not terminate already running workers. Use the existing task stop
command on an identified worker and preserve the original code/worktree first.
No automatic replacement agent or hidden scope expansion is performed.

Team v1 uses declared, finite DAGs and artifact-based coordination; it does not
implement unrestricted agent-to-agent conversation or agent-generated new
permissions/scopes. Use the current task record, shared Registry and durable
team checkpoint to recover an interrupted run without replaying work.

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
    "codex_dev": {
      "agent_kind": "codex",
      "read_args": ["--model", "gpt-example", "--sandbox", "read-only"],
      "write_args": ["--model", "gpt-example", "--sandbox", "workspace-write"]
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
unchanged. Profile names, launch arguments and backend choices are operator
configuration, not tenant state or operator identity proofs.

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
identity service or approval files. The immutable team and selected model
configuration are retained so later changes cannot silently alter dispatch.

Reuse a team for one initiative. A separately approved successor identifies
its predecessor with `supersedes` and gives a short `change_reason`; the old
team must be paused and terminal. Existing teams and Registry claims stay
intact. Public installations without this optional chat policy retain the
existing initialization behavior.
