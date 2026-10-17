---
name: cao-operator
description: Operate the CAO AgentOps control plane safely from a public codxt/cao checkout. Inspect readiness and state, keep the effectful product `./cao` CLI separate from the read-only `python -m cao` CLI, run bounded Team Execution, treat Intake as advisory until an operator authorizes it, and honor the Work Registry, model-privacy, receipt, CI, merge and deployment gates. Use when an operator or agent asks to work with CAO, run or inspect `cao` commands, CAO teams, CAO intake, or CAO recovery.
---

# CAO Operator

CAO is an open-source AgentOps control plane for agentic runtimes. It keeps four
concerns separate: runtime execution, Work Registry ownership, reconciliation,
and policy/authority. Nothing in this skill overrides those gates, and no CAO
command grants publication, deployment or release authority.

Use this skill to inspect CAO and to drive it **within** its gates. Never invent
a result: when required evidence cannot be read, CAO reports explicit
`UNAVAILABLE`/`UNKNOWN` and exits nonzero. Treat that as a hold for an operator,
not as success.

## Two command surfaces — never conflate them

- **Read-only foundation: `python -m cao`** (or the installed `cao` for groups
  this build does not own). Read-only, structured JSON, never mutates state:
  `plugins list`, `work list`, `worker list`, `run list`, `coordinator status`,
  `relay status`, `automation validate`, `automation status`. `automation run` is
  always `UNAVAILABLE`. Use this surface to inspect.
- **Effectful product CLI: `./cao` from this checkout** (or installed `cao`).
  Drives one installed, pinned managed runtime and owns task, team, intake,
  improve and daemon state: `doctor`, `run`/`status`/`logs`/`stop`, `daemon
  start|stop|status`, `team …`, `intake …`, `improve …`. These commands can start
  an agent and write state under the private CAO roots. Use them only with
  operator authority.

Both surfaces fail closed. Exit codes are `0` success, `2` usage error, `3`
explicit `UNAVAILABLE`/`NOT_READY`/`UNKNOWN`. `NOT_READY` and `UNAVAILABLE` are
holds, not soft failures to retry blindly.

## Inspect before acting

- Run `./cao doctor` first. It prints exact per-subsystem readiness evidence and
  fails closed: an unreachable runtime server, a missing provider, or
  credentials that cannot be verified without reading a secret yields
  `NOT_READY`.
- **`doctor` and `daemon` are separate.** `doctor` is a one-shot readiness
  report. `./cao daemon start` is the separate, opt-in supervision loop that
  ticks teams and reconciles work. Doctor `READY` is not proof of a qualified
  private provider and is not an autonomous-operator grant.
- Prefer the read-only surface for inspection; do not start work to "find out".

## Team Execution lifecycle

- Order: `team validate` -> `team plan` (read-only) -> `team init` (defaults
  PAUSED and launches nothing) -> `team start` (sets desired RUNNING) -> `team
  tick`/the daemon (dispatch and observe) -> `team status`. Use the existing
  daemon for recurring ticks; no other scheduler is needed.
- Bounds: `max_parallel` default 4 (hard maximum 8); `max_writers` default and
  hard maximum 2. READ tasks and reviews consume no writer slot. Use separate
  Git worktrees for truly independent concurrent writers.
- Team identities and graphs are immutable once initialized. To change scope,
  start a separate governed team. Do not write to a checkout while its reviewer
  is inspecting that HEAD.
- Reviewer verdicts (`CAO_REVIEW_VERDICT: APPROVED|CHANGES_REQUESTED` plus
  `CAO_REVIEWED_HEAD: <sha>`) are workflow advisory and independent review, not
  deployment/publication/credential authority and not proof of correctness.
- Corrections run only inside the team's authorized WRITE scope and correction
  budget; no disposition grants a new scope, credential, push, merge or deploy.

## Intake: advisory versus execution

- Admission commands (`submit`, `list`, `show`, `pull`) record intent only.
  `triage` returns one advisory disposition; `compile` produces a draft
  manifest that carries `authority = none` and `dispatch = false`; `status`
  derives a truthful requester status; `reply` serves only a gated `--dry-run`
  or `--mock` simulation.
- **Admission is not work, triage is not authority, a compiled draft is not
  approval, and a derived status is not acceptance.** A real `reply` send is
  `UNAVAILABLE`, because this build holds no authenticated delivery identity.
- Only an independently reviewed draft followed by a separate operator
  `cao team init --approved` and an explicit dispatch authority starts anything.
  The gated handoff/queue/pipeline paths require an **in-process authenticated
  operator authority**; a CLI flag, an environment value or a JSON file is
  caller-controlled and is never proof. WRITE and every consequential effect
  stay held.

## Registry hard refusals

- The shared Work Registry is the only writer-ownership gate. Live Registry
  preflight and the atomic claim decide the actual repository/path/branch
  permission; an EXCLUSIVE claim refuses team dispatch outright.
- Never force-unlock, release or fabricate a Registry release. A stale, unknown
  or foreign claim keeps its ownership until an operator reconciles it; team
  recovery never invents a release to get past it.

## Model privacy and routing

- CAO is route-agnostic: it ships no global allowed-route set and endorses no
  provider. An installation may enforce its own policy with the optional
  operator-local `team_model_allowlist`; only the absence of the field is the
  opt-out.
- Never substitute a different model when a route is unavailable or out of
  quota: hold the task `UNAVAILABLE` for an operator decision.
- An allowlisted route whose provider is known not to offer zero-data-retention
  is refused at every NEW dispatch (`TEAM_ROUTE_PRIVACY_UNVERIFIED`). Never
  route private, client, tenant or secret material to a free or unverified
  route.
- Codex routes are READ-only (`TEAM_ROUTE_READ_ONLY`) and a foreign, spoofed or
  missing route identity is refused (`TEAM_ROUTE_IDENTITY_MISMATCH`).

## Receipts and recovery

- A prompt being accepted is not completion, and a bare `IDLE` is not
  completion. Completion requires a durable **post-prompt settled** capture
  carrying this execution's exact task-bound 16-character nonce on a
  `CAO_TEAM_RESULT:` line. Never accept a near-miss marker, never invent a
  receipt, and never repeat an issued prompt under a new identity.
- A receipt proves a post-prompt response was observed. It is not a security
  signature, approval, guarantee of correctness or proof that tests passed.
- `cao team resume` re-checks an inspected `UNKNOWN` task using the **same**
  execution identity and prompt. `cao team retry-read` admits **one** fresh READ
  identity after an operator has verifiably stopped the original worker. WRITE
  work is never automatically replayed, retried, stopped, released, merged or
  published. See `references/recovery.md`.

## CI, merge and deployment acceptance boundaries

- Final acceptance requires an accountable operator to inspect tests, CI, the
  reviewed diff, security, operating state and authorization. A green local
  check, a review verdict or a Registry claim is not that acceptance.
- Merge requires a distinct qualified operator grant bound to the exact head
  plus source review and green CI on that exact head. Deployment, release and
  publication require separate, distinct accountable authorization.
- Report `SOURCE_COMPLETE`, `TEAM_COMPLETE`, `MERGED`, `DEPLOYED` and
  `LIVE_ACCEPTED` separately, each with its actual source SHA and evidence.

## No direct runtime or agent bypass

- Never bypass the managed runtime (Herdr) or Codex from outside CAO's governed
  path, and never enable
  `--dangerously-bypass-approvals-and-sandbox`,
  `--dangerously-bypass-hook-trust`, `danger-full-access`, or an unbounded
  `--auto` grant. No permission bypass can override a host tool-layer refusal: a
  refused call stays refused and is reported `UNAVAILABLE`.
- Do not drive a runtime or agent directly to route around a Registry claim, a
  model-privacy refusal or a receipt check; that bypasses the gate rather than
  passing it.

## References

- `references/commands.md` — the two command surfaces, exit codes and cautions.
- `references/recovery.md` — receipt verification and the bounded recovery paths.
