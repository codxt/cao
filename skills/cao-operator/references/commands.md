# CAO command surfaces

CAO ships two surfaces from one tree. They differ in effect, so pick the surface
by intent: inspect with the read-only CLI, act with the product CLI only under
operator authority.

## Read-only foundation: `python -m cao`

Pure Python, no runtime dependency, structured JSON on stdout, and it never
mutates state. Run it from a checkout with `PYTHONPATH=src python3 -m cao`.

| Command | Reports |
|---|---|
| `plugins list` | registered runtime plugins, by explicit id (empty is honest) |
| `work list --ledger <path>` | proposed work items from the registry ledger |
| `worker list --ledger <path>` | active claims in the registry ledger |
| `run list --ledger <path>` | runs in the registry ledger |
| `coordinator status --ledger <path>` | intended-vs-observed reconciliation |
| `relay status --state <path>` | relay deduplication state |
| `automation validate --config <path>` | declarative automation manifest |
| `automation status --bookmarks <path>` | automation state |
| `automation run` | always `UNAVAILABLE` (no concrete source reader) |

Exit codes are `0` success, `2` usage error, `3` explicit `UNAVAILABLE` or
`UNKNOWN`. An unreadable ledger is reported `UNAVAILABLE`, never as an empty but
successful result.

## Effectful product CLI: `./cao`

The source launcher `./cao` runs `python3 -S -m cao.product_cli` from the
checkout. The installed `cao` console script is the same command. It drives one
installed, pinned managed runtime and owns task, team, intake, improve and
daemon state.

| Command | Effect |
|---|---|
| `./cao doctor` | one-shot per-subsystem readiness; fails closed with `NOT_READY` |
| `./cao status` | product/service status |
| `./cao run …` | submit one governed task (can start an agent) |
| `./cao logs` | bounded captured task output |
| `./cao stop --id <id>` | stop one owned task and release its claim |
| `./cao daemon start\|stop\|status` | the separate, opt-in supervision loop |
| `./cao team …` | `validate`, `plan`, `init [--approved]`, `start`, `tick`, `status`, `list`, `pause`, `resume`, `retry-read` |
| `./cao intake …` | `submit`, `list`, `show`, `pull`, `transports`, `triage`, `compile`, `status`, `reply`, `handoff`, `authority`, `queue …` |
| `./cao improve …` | `assess`, `context`, `verify`, `propose`, `learn`, `check-proof`, `watch`, `authorize`, `engineering` |

`./setup.sh` writes an idempotent product configuration and installs or adopts
the pinned runtime through a stdlib-only installer that verifies a hash before
anything is put in place. Adoption never replaces or stops an existing binary.

## Cautions

- `doctor` and `daemon` are different things. `doctor` reports readiness;
  `daemon start` is the recurring supervision loop. `daemon` is optional for
  manual tasks.
- Commands owned by an unimplemented lane report structured `UNAVAILABLE` and a
  nonzero exit. Never treat a nonzero exit as a transient error to retry: read
  the reported code and hold.
- Intake `triage`, `compile`, `status` and `reply --dry-run/--mock` are
  advisory, draft-only, projection-only or simulated. They grant no authority
  and start nothing.
- `python -m cao` is read-only; `./cao` is effectful. Do not use the read-only
  surface expecting it to start work, and do not use the product surface to
  bypass a gate the read-only surface reports as unavailable.
