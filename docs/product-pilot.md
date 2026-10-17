# CAO product pilot runbook

This is the accountable operator's runbook for the **real** product pilot. It
exercises the source bootstrap, the task lifecycle and the CAO daemon end to end
against a live managed runtime, inside one disposable directory, and records
evidence for every claim it makes.

The pilot is disposable and self-contained: a fresh XDG config/state root under
`$PILOT`, a scratch git repository, and exactly one runtime workspace that CAO
creates and later closes. It never downloads over a managed runtime, never
stops the runtime's own server, and never touches an unrelated workspace,
session or process.

It observes and drives the runtime through the runtime's own command line (the
`workspace`, `pane` and `agent` verbs shown below), which is a separate surface
from CAO's runtime-neutral plugin API; see
[`architecture.md`](architecture.md).

Read the whole runbook before running any command. Steps 1-4 are preparation;
steps 5-11 are the pilot; "Evidence" says what to keep.

## Safety rules (non-negotiable)

- Work only inside `$PILOT`. Anything you create there is removed at the end.
- `./setup.sh` is used in **adopt-only** mode: it uses an already-installed
  compatible runtime and never replaces or stops it.
- Never run `./setup.sh --download` while a runtime server is live.
- The provider's credentials are the operator's and are never read, stored or
  echoed by CAO; only the CLI, provider id and model are recorded.
- Close **only** the surfaces this pilot created. Leave every other workspace,
  session and the runtime server untouched.
- Verify the actual agent process, repository diff, affected paths and final
  commit before accepting a WRITE pilot. A model status of `idle`/`done`, a
  prompt acknowledgement, or a CAO claim release alone is insufficient proof
  that the requested write occurred. A WRITE completion now requires the same
  branch and an in-scope, clean committed diff relative to the task's captured
  starting HEAD. A missing commit, uncommitted work, unexpected file, or
  unverifiable Git state retains the WRITE claim for correction or governed stop.
- Fail closed: if a step's expected output is not seen, stop and record the
  actual output rather than continuing.

## 0. Preconditions

- A source checkout of the accepted revision at `$CLONE`.
- `python3` 3.10+ and `git` on `PATH`.
- A managed runtime already installed and compatible with the pinned release
  (the version and digests are declared in `setup.sh`, the single source of the
  pinned coordinates).
- Provider credentials for the CLI you will configure, owned by you.

```sh
export CLONE="$PWD"                  # the checkout you are piloting
```

## 1. Isolate everything under a disposable root

```sh
export PILOT=/tmp/cao-pilot-$(date +%Y%m%d%H%M%S)
mkdir -p "$PILOT/config" "$PILOT/state" "$PILOT/work"
export XDG_CONFIG_HOME="$PILOT/config"
export XDG_STATE_HOME="$PILOT/state"
```

Expected: no output. Confirm the isolation:

```sh
echo "$XDG_CONFIG_HOME" "$XDG_STATE_HOME"
```

Every durable CAO record the pilot writes lands under `$PILOT`, including the
Work Registry ledger (`$XDG_STATE_HOME/cao-work-registry/registry.sqlite3`). The
pilot therefore never writes the shared governed ledger.

## 2. Bootstrap: adopt the managed runtime in place

```sh
cd "$CLONE"
./setup.sh --adopt-existing
```

Expected (structure, values abbreviated):

```json
{
  "schema": "cao.product.install.v0",
  "status": "OK",
  "platform": "<system>-<arch>",
  "runtime": {
    "name": "<runtime>",
    "version": "<pinned-version>",
    "policy": "adopt",
    "state": "ADOPTED",
    "path": "<absolute path to the existing binary>",
    "detail": "adopted the existing compatible binary in place"
  }
}
```

- `state` is `ADOPTED` (an existing compatible binary) or `ALREADY_INSTALLED`
  (a managed copy CAO installed earlier). Both are `status: OK`.
- If no compatible binary exists the outcome is `MISSING` and status
  `NOT_READY`. Do **not** fall back to a download. Stop and record the output.

Record the managed executable path for later observations:

```sh
export RUNTIME=$(python3 -c "import json;print(json.load(open('$XDG_STATE_HOME/cao/managed.json'))['path'])")
echo "$RUNTIME"
```

## 3. Configure the provider explicitly

The bootstrap created `$XDG_CONFIG_HOME/cao/config.json`. Add the provider block
(never a secret value):

```sh
python3 - "$XDG_CONFIG_HOME/cao/config.json" <<'PY'
import json, sys
path = sys.argv[1]
doc = json.load(open(path))
doc["provider"] = {
    "cli": "opencode",        # an agent CLI CAO recognises on PATH
    "id": "<provider-id>",    # the id CAO verifies against the CLI's auth list
    "model": "<model>",       # the explicit model the agent is started with
}
doc.setdefault("task", {})["agent_kind"] = "opencode"
json.dump(doc, open(path, "w"), indent=2, sort_keys=True)
PY
```

- For the `opencode` provider, CAO passes the chosen `provider.model` to
  `opencode mini --standalone --model <provider.model>` automatically. Without
  this, Herdr may launch the interactive OpenCode TUI at an unrelated default
  model, and a prompt acknowledgement would not prove a running task.
  `task.agent_args` is an advanced explicit override; if you set it, ensure
  it selects the intended model and suitable non-interactive harness.
- `cli` must be on `PATH`; `id` must name an authenticated account as printed
  by the CLI (for example `OpenCode-Go`), not the CLI executable's own name.
- CAO never reads or stores the credential itself. If authentication cannot be
  positively verified, `doctor` stays `NOT_READY`.

### Credential verification needs the provider's own environment

`doctor` verifies credentials by running the provider CLI's own non-secret
authenticated-list probe (`opencode auth list`) as a subprocess. That CLI reads
its configuration and authentication from the same `XDG_CONFIG_HOME` and
`XDG_STATE_HOME` this pilot isolates, and an isolated `XDG_STATE_HOME` in
particular makes it start a per-root managed service and exit `127` (observed on
the qualification host). Under full isolation the probe therefore fails and
`doctor` truthfully reports `credentials: UNVERIFIED`, `status: NOT_READY`,
exit `3` — a fail-closed answer, not a defect.

To reach a verified `READY` gate, run `doctor` with the provider CLI's own
environment visible, for example on the operator's own roots:

```sh
env -u XDG_STATE_HOME -u XDG_CONFIG_HOME ./cao doctor
```

The pilot's task steps (6 onward) are unaffected either way: the managed
runtime launches the agent from its own server process, which carries the
operator's environment, so the agent authenticates while CAO's records stay
isolated. The credential gate is a readiness report for the operator, not a
precondition the runtime enforces.

## 4. Readiness gate

```sh
./cao doctor
```

Expected: exit `0` and `"status": "READY"`, with every gate true:

```json
{
  "schema": "cao.product.doctor.v0",
  "status": "READY",
  "gates": {
    "managed_runtime": true,
    "runtime_server": true,
    "provider_found": true,
    "model_configured": true,
    "credentials_verified": true
  },
  "readiness": {"manual_task": true, "unattended": false},
  "reasons": []
}
```

`runtime_server` is the managed runtime's own live `status server` probe. If the
server is down the gate is false and `doctor` is `NOT_READY`, even when the
other gates pass. The daemon is optional for a manual task: `readiness.manual_task`
does not require it, while `readiness.unattended` is true only with a running
daemon.

If any gate is false the exit code is `3` and `status` is `NOT_READY`. Stop; the
reason names the gate that did not pass. Under the isolated roots of step 1 the
credentials gate fails closed by design (see step 3); verify readiness once with
the provider's own environment visible, then continue with the isolated roots.
pilot is not ready. Record the `sections` and `reasons` verbatim.

## 5. Prepare the one disposable working tree

```sh
export REPO="$PILOT/work/repo"
git init -b main "$REPO"
printf 'pilot\n' > "$REPO/README.md"
git -C "$REPO" add -A
git -C "$REPO" -c user.name=pilot -c user.email=pilot@example.invalid commit -m "pilot seed"
```

`cao run` provisions **exactly one** runtime workspace, labeled with the
CAO-owned format the product already emits: `cao-review-<task_id>` (the
`task.workspace_label_prefix` default `cao-review-` plus the derived task id).
Do not create any other workspace by hand.

## 6. Run one real, harmless agent task

Use a harmless prompt: the agent should reply with a single confirmation word.

```sh
./cao run --repo "$REPO" --id pilot01 \
  "Reply with the single word: confirmed. Do not modify any file."
```

Expected: exit `0` and a running task:

```json
{
  "schema": "cao.product.task.v0",
  "ok": true,
  "status": "running",
  "task_id": "pilot01",
  "repo": "<the disposable repo>",
  "mode": "READ",
  "prompt_state": "accepted",
  "claim": {"claim_id": <n>, "status": "CLAIMED", "mode": "READ"},
  "workspace": {"id": "<ws-id>", "label": "cao-review-pilot01"},
  "pane_id": "<pane-id>",
  "agent": {"name": "caopilot01", "kind": "opencode", "started": true}
}
```

Capture the identifiers from the durable task record written by the run above:

```sh
export TASK=pilot01
export REC="$XDG_STATE_HOME/cao/tasks/$TASK.json"
export WS=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['checkpoint']['workspace_id'])" "$REC")
export PANE=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['checkpoint']['pane_id'])" "$REC")
echo "$WS" "$PANE"
```

## 7. Observe the live agent

Three independent readings, all expected to agree:

```sh
./cao status
./cao logs --id "$TASK" --lines 40
```

- `status` → `"status": "RUNNING"`, `task.observation.status: "OBSERVED"`, the
  workspace label and the agent name/kind above.
- `logs` → exit `0`, `"ok": true`, bounded `output` ending in the agent's
  confirmation. The output is truncated to the configured bound, never dumped
  unbounded.

Confirm the live process carries the exact configured kind and model argv:

```sh
ps -eo pid,ppid,etimes,args | grep -F '<model>' | grep -v grep
```

Confirm the runtime sees the one CAO-owned agent (never another workspace's):

```sh
"$RUNTIME" workspace list
"$RUNTIME" pane list --workspace "$WS"
"$RUNTIME" agent get caopilot01
```

Expected: exactly one workspace labeled `cao-review-<task_id>`; its pane reports
the agent with state `working`/`idle`; `agent get` names the same agent. Do not
inspect or touch workspaces you did not create.

## 8. Final handback evidence

Let the agent finish by resuming the same task with a bounded wait:

```sh
./cao run --repo "$REPO" --id "$TASK" --wait-ms 300000 \
  "Reply with the single word: confirmed. Do not modify any file."
```

Because this is a resume, it reuses the workspace/agent/prompt and only waits.
On a `done` agent the task completes and releases its claim with the observed
repository HEAD. An ordinary product task has no task-bound receipt to verify,
so a bare `idle` -- including the idle a focused tab reports -- is never treated
as completion for it:

```json
{
  "status": "complete",
  "claim": {
    "outcome": "complete",
    "released": true,
    "released_head": "<40-hex commit id>"
  }
}
```

Cross-check the handback against the repository itself:

```sh
git -C "$REPO" rev-parse HEAD
```

The two commit ids must match byte for byte. Record both. If the HEAD cannot be
read, the task stays `running` with `TASK_HEAD_UNREADABLE` and the claim is left
open — never reported complete.

## 9. Restart proof (daemon mid-run)

Start the CAO daemon while work exists, then restart it, and prove the restart
neither re-prompts nor re-starts the agent.

```sh
./cao daemon start
./cao daemon status
```

Expected: `"status": "RUNNING"`, a positive `pid`, and `tasks` naming the pilot
task. Now restart it:

```sh
./cao daemon stop
./cao daemon start
./cao daemon status
```

Prove no duplicate effect from durable, fake-free evidence:

1. The task record — prompt/start were issued exactly once:

   ```sh
   python3 - "$XDG_STATE_HOME/cao/tasks/$TASK.json" <<'PY'
   import json, sys
   rec = json.load(open(sys.argv[1]))
   cp = rec["checkpoint"]
   print("agent_started:", cp["agent_started"])
   print("start_attempted:", cp["start_attempted"])
   print("prompt_issued:", cp["prompt_issued"])
   print("prompt_ack:", cp["prompt_ack"])
   PY
   ```

   Expected: `agent_started: True`, `prompt_issued: True`, `prompt_ack:
   accepted`, and no field showing a second attempt.

2. `./cao daemon status` — supervision of the task with no error surge:

   ```sh
   ./cao daemon status | python3 -c "import json,sys;d=json.load(sys.stdin);print(json.dumps({'ticks':d['ticks'],'errors':d['errors'],'tasks':d['tasks'],'held':d['held']}, indent=2))"
   ```

   Expected: `tasks` names the pilot task; `held` is empty; `errors` does not
   grow across the restart.

3. The runtime's own agent list — one agent, not two:

   ```sh
   "$RUNTIME" pane list --workspace "$WS"
   "$RUNTIME" agent get caopilot01
   ```

Expected: exactly one CAO-owned agent, in the same pane, with the same name and
kind before and after the restart. Capture the three readings before and after
and diff them.

## 10. Fail-closed verification

Each case must fail closed (exit `3`) and fabricate no work. Restore config
between cases.

- **Provider missing.** Point the pilot at a config with no provider and run:

  ```sh
  XDG_CONFIG_HOME="$PILOT/empty-config" ./cao run --repo "$REPO" "review"
  XDG_CONFIG_HOME="$PILOT/empty-config" ./cao doctor
  ```

  Expected: `run` → exit `3`, `"code": "PRODUCT_TASK_UNAVAILABLE"`; `doctor` →
  exit `3`, `"status": "NOT_READY"`.

- **Incompatible runtime version.** With a non-pinned runtime earlier on `PATH`,
  `./setup.sh --adopt-existing` must refuse to adopt rather than use it:

  ```sh
  ./setup.sh --adopt-existing   # against a non-compatible binary on PATH
  ```

  Expected: `"state": "INCOMPATIBLE"`, `status: NOT_READY`. It never replaces or
  stops the existing binary.

- **Duplicate task id.** Reuse an id with different content:

  ```sh
  ./cao run --repo "$REPO" --id "$TASK" "a different prompt"
  ```

  Expected: exit `3`, `"code": "PRODUCT_TASK_USAGE"`, naming the mismatched
  field. No second task is started.

- **Write authority.** A write task without a declared scope is a usage error,
  and a scoped write is gated on live occupancy:

  ```sh
  ./cao run --repo "$REPO" --mode write "edit the code"
  ./cao run --repo "$REPO" --write README.md "edit the code"
  ```

  Expected: the first exits `3` with `PRODUCT_TASK_USAGE` and starts nothing.
  The second records `mode: WRITE` with the declared scope, reads the runtime's
  live occupancy, and claims `WRITE` (`"status": "running"`, `claim.mode:
  "WRITE"`, exit `0`) when the occupancy is healthy and no writer conflicts.
  Two WRITE tasks sharing a path are refused by the ledger with a `CONFLICT`;
  an unreadable occupancy is refused as `NEEDS_RECONCILIATION`, never assumed
  safe.

## 11. Close only CAO's surfaces

If the task is still in flight, `cao stop` releases the claim and closes only
the CAO-created pane:

```sh
./cao stop --id "$TASK"
./cao daemon stop
```

Expected: `"outcome": "abandon"` and `"pane_closed": "<pane-id>"` equal to the
pane from step 6; `daemon stop` reports `"status": "STOPPED"`.

A task that reached `complete` under this build already closed its own pane
before releasing its claim (READ and WRITE alike), so `cao stop` is an
idempotent no-op there and no pane is left behind. Only a record written by an
older build can still hold a terminal pane. For such a historical record, close
the CAO-created surface directly with the runtime's own close primitive,
addressed by the captured ids and only after confirming the workspace carries
the CAO-owned label `cao-review-<task_id>`:

```sh
"$RUNTIME" pane close "$PANE"
# if the runtime leaves the now-empty CAO-owned workspace open, close it too:
"$RUNTIME" workspace list
```

Confirm no CAO-owned workspace remains:

```sh
"$RUNTIME" workspace list | python3 -c "import json,sys;print([w['workspace_id'] for w in json.load(sys.stdin)['result']['workspaces'] if (w.get('label') or '').startswith('cao-review-')])"
```

Expected: `[]`.

Remove the disposable root:

```sh
rm -rf "$PILOT"
```

Do not touch any workspace, pane, session or process this pilot did not create,
and do not stop the runtime's own server.

## 12. Historical pane hygiene (default-off; gated historical cleanup)

A fresh CAO only **observes and plans**: it never closes a historical pane
unless an operator explicitly enables the effect. The existing daemon performs
one bounded hygiene pass per tick and reports it on the existing status surface
under `hygiene` (counts only, never raw process output):

```sh
./cao daemon status | python3 -c "import json,sys;print(json.load(sys.stdin)['hygiene'])"
```

Expected with no configuration: `"mode": "plan"`. The classification counts are
`eligible`, `closed`, `protected_active`, `protected_unknown`, `unowned`,
`missing_pane`, `close_failed` and `process_leak`. Exactly one final
classification is recorded per terminal pane per tick: an applied success is
`closed` (not both `eligible` and `closed`), a race or close failure reports its
final hold/failure, and a candidate skipped by the per-tick bound stays
`eligible`.

Pane existence is probed read-only first. After a valid terminal pane identity
and a readable runtime, a genuinely absent pane is skipped **before** any
team/lane, checkout or Work Registry/owner-acceptance test. An absent pane is
neither `eligible` nor a held `protected_unknown` count: an unrelated predicate
that cannot be proved is not evidence that a leftover exists. An unreadable pane
or runtime is never treated as absence and still holds, and an unreadable ledger
is still reported `UNAVAILABLE` and never grants a close.

**Default off.** `hygiene_enabled` defaults to `false` and `hygiene_dry_run`
defaults to `true`, and both must stay that way in source. Committing code never
activates the effect; only an installation's own local config does. Two terminal
pane states are distinguished:

* **Unexpected reappearance.** The task durably released its claim **and**
  recorded a verified close of this exact pane, yet the pane is observed
  present. A recorded verified absence cannot be reconciled to a present pane
  without a runtime continuity attestation that does not exist, so it is
  `protected_unknown` and held -- the accepted PR #31 protection, preserved.
* **Corroborated historical record.** A task written by the pre-close-first
  build (before commit `6d2f271`) released its claim **without** closing the
  pane, so its record carries a released disposition and **no** recorded close.
  Such a pane is `eligible` only when every one of these holds: the exact
  RELEASED Work Registry row (claim id, run, work key, CAO owner, mode, release
  outcome and time, and any declared worktree) corroborates the record's
  released disposition; the pane is this task's own exact `cao-review-<task_id>`
  workspace and pane; its uniquely named worker is observed stopped; no live
  claim owns the task or checkout; the checkout is clean and readable; and no
  other task reuses the pane. A forged released boolean the ledger does not
  match, a mismatched/unknown row, an unaccepted team phase, a reused pane, a
  foreign or active worker, and an unreadable runtime or ledger all hold.

**Activation criteria.** The opt-in switch is not operating authority by itself.
Enable it only on an installation whose own plan run shows a bounded, reviewed
`eligible` cohort that is a genuine pre-close-first leftover, with the operator
separately confirming each row's released Registry claim and its
`cao-review-<task_id>` workspace. A passing test suite proves the gate, not the
operating cohort. To activate, the operator clears the dry run **locally** in
the installation's own config (`hygiene_enabled: true`, `hygiene_dry_run:
false`); source defaults never change.

**Operator canary.**

```sh
# Plan only (no effect): review the eligible cohort line by line before activation.
./cao daemon status | python3 -c "import json,sys;print(json.load(sys.stdin)['hygiene'])"
```

When enabled, an applied pass closes only the revalidated eligible cohort: for
each candidate it re-reads the mutable task record, current pane/workspace reuse
conflicts, the team's durable acceptance, a fresh checkout cleanliness probe, the
full original released Registry row, and the pane's own named worker immediately
before the close. A task that became non-terminal, a worker that became active
or foreign, a newly opened claim (including another owner's), a newly dirty or
unreadable checkout, and a changed row all hold. It then closes only this task's
exact owned pane, confirms absence through the runtime (`pane_get`), and records
the verified close as `pane_closed` on the shared task record. The small instant
between the final check and the runtime's own close call is an irreducible
runtime race: CAO never knowingly closes an active or foreign worker, and it
never claims a closure that `pane_get` did not confirm.

**Audit receipts.** The daemon record's `hygiene` block -- its counts plus a
bounded item list of `task_id`/`classification`/`reason` -- is the receipt,
surfaced by `cao daemon status`. A closed pane additionally carries
`pane_closed` on its own task record. No raw process output or tenant data is
ever included.

**Rollback / fail-closed.** Clear the local opt-in (or set `hygiene_dry_run:
true`) and the pass returns to plan-only on the next tick; nothing is closed. An
unreadable runtime, an unreadable ledger, a dirty or unreadable checkout, a
reused pane, an active or foreign worker, a live claim, an unknown team phase,
or a `pane_get` that still finds the pane after an acknowledged close all fail
closed to a held classification -- never a fabricated `closed`. `process_leak`
is only ever reported with a real recorded process identity; CAO records no
pane-child process identity, so it is never fabricated.

One crash window is irreducible: the close can be verified while persisting
`pane_closed` on the task record fails. That tick reports `close_failed`, never
`closed`; the record remains without a verified close and the next pass sees the
pane absent, skips it as having no leftover, and claims nothing further. CAO
therefore never fabricates a closure it could not attribute to a persisted task
record.

If no eligible cohort survives review, leave the switches at their defaults and
close the specific historical pane manually (section 11) after confirming the
CAO-owned `cao-review-<task_id>` workspace label.


## Evidence

Record, for each claim, the command and its exact JSON output in the pilot's
evidence file:

| Evidence | Source | Expected |
| --- | --- | --- |
| Adoption in place | `setup.sh --adopt-existing` | `state: ADOPTED`, `status: OK` |
| Readiness | `cao doctor` | `READY`, exit `0` |
| Single CAO workspace | `run` + runtime `workspace list` | one `cao-review-<task_id>` |
| Exactly-once prompt/start | task record `checkpoint` + runtime `agent get` | one agent, `prompt_issued: true` |
| Live observation | `cao status`, `cao logs`, `ps` | `OBSERVED`, bounded output, kind/model argv |
| Handback | `cao status` + `git rev-parse HEAD` | `complete`, equal `released_head` |
| Daemon restart | `cao daemon status` before/after | no new prompt, no new start, `held` empty |
| Fail-closed cases | step 10 commands | exit `3`, no faked work |
| Clean close | `cao stop`, `daemon stop` | claim released, only CAO pane closed |
