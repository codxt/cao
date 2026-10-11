# Operator-approved CAO teams

CAO's public team-admission control is portable. It does not presume any
operator's models, subscriptions, provider accounts, host-specific paths or approval
service. The existing CAO daemon, Herdr runtime interface and shared Work
Registry remain unchanged.

## Local policy and trust boundary

For backward compatibility an absent team-approval policy uses
`{"mode":"legacy"}`. Operators who require human decisions configure this
private section in the CAO product configuration:

```json
{
  "team_approval": {
    "mode": "signed",
    "signer": "operator",
    "allowed_signers_file": "/secure/path/team-operators.allowed_signers"
  }
}
```

The signer is a local operator-chosen principal, not a model name or an
automatically authenticated ChatGPT session. The allowlist uses the OpenSSH
allowed-signers format, e.g. `operator ssh-ed25519 AAAA... comment`.
The file must be a regular non-symlink and must not be group/other writable.
Protect the file and parent directories from worker writes using OS-level
ownership and permissions. Most importantly, **keep the operator's private
signing key off the agent host and outside all agent runtimes**. An agent
running with write access to CAO's local configuration or allowed-signers
file could undermine local policy. Unix account identity alone is not
proof of an operator decision when an agent shares that account.

CAO needs OpenSSH `ssh-keygen -Y verify` only in signed mode; no new Python
package or authority service is added. Verification runs without a shell,
and no signing key or provider credential is read by CAO.

## Approval workflow

The proposer prepares an exact team manifest with an initiative name,
roles/model/profile selection, tasks, review and writer limits:

```sh
cao team validate --file /private/initiative.json
cao team plan --file /private/initiative.json
cao team proposal --file /private/initiative.json > /private/team-proposal.json
```

Review the *complete team manifest*, the resolved local model/profile
configuration, review contract, dependency graph, read/write paths, concurrency
and expected costs. The proposal is a canonical JSON line containing the
team ID, initiative, complete manifest SHA-256, routing SHA-256 and optional
successor explanation. Models/profile arguments are bound to the approval
through both digests.

Transfer that exact proposal to the operator's trusted computer. The operator
decides whether to approve it. If approved, the operator signs using an
independently held SSH signing key:

```sh
ssh-keygen -Y sign -n cao-team -f /private/operator_signing_key team-proposal.json
```

Copy only the resulting `team-proposal.json.sig` to the CAO host. Then:

```sh
cao team init --file /private/initiative.json --signature /private/team-proposal.json.sig
cao team start --id approved-team-id
```

The initializer verifies the signature against the operator's public-key
allowlist, refuses missing or stale signatures, and persists only the
public signature, operator identifier and exact digests in the existing
private team checkpoint. Init remains paused and does not spawn an agent.
Start verifies stored approval. The daemon rechecks approved routing before
starting or reconciling tasks. Changed model/provider arguments or revoked
operator trust hold the team instead of silently substituting a backend.

An unsigned, revoked or unverifiable decision reports
`UNAVAILABLE` with code `TEAM_APPROVAL_REQUIRED` on initialization
and newly requested starts. Use `cao team plan` and `proposal` to request
operator review; do not retry by inventing a replacement team.

A CLI-provided `approved_by` string, model completion, GitHub access,
a valid Registry claim, and the ability to edit a config file are **not**
operator authorization. Signed team approval grants only this team/role/task
manifest. Credentials, publication, branch merges, purchases, Factory releases
and production mutations still have their own authorization boundaries.

## No proliferation / explicit successor

A signed team must have a stable `initiative` ID. An initiative has one
current immutable team. Calls to `cao team init` using the same ID and exact
manifest are idempotent and do not create a second execution group.

For a distinct team ID with the same initiative, the new manifest must name
its exact predecessor using `supersedes` and give a substantive
`change_reason` (20–1000 characters), both covered by a *new operator
signature*. The predecessor must be paused, terminal (COMPLETE/BLOCKED),
have no RUNNING slots, and have no active Work Registry claims. Source review
must independently confirm that a blocked or UNKNOWN task has no lingering
runtime effects before any successor is started. Prior team records,
receipts, worktrees and Registry evidence are retained.

Old teams created before this policy have no initiative or signed admission.
Their existing running work may reconcile; pending tasks in those legacy
records cannot newly dispatch when the signed policy is enabled. Existing
legacy teams cannot be restarted from PAUSED without a governed migration.
Legacy unlabelled histories require explicit operator mapping before treating
a newly named initiative as a successor; CAO does not infer relations from
similar team names or task prompts.

Signed mode is operator opt-in in the public OSS product, so existing CAO
users do not break on upgrade. Production installations seeking mandatory
human decisions must enable signed mode and provision their trusted signer.
Without a configured public-key allowlist and an external approval signature,
new team admission fails closed. No approval is created automatically by
a daemon, watcher, agent or assistant.
