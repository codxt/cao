# CAO operator guide

This guide describes the commands that work today. Every command is
**read-only**: no command starts, stops, prompts, applies, syncs or writes
anything. There is no write subcommand.

## Output and exit codes

Every command prints structured JSON on stdout with a stable `schema` field.
Human-readable output exists only for `plugins list` when `--json` is omitted;
it is derived from the same data.

| Exit code | Meaning |
| --- | --- |
| `0` | Success; the command produced real evidence |
| `2` | Usage error (unknown command, missing required argument) |
| `3` | Explicit `UNAVAILABLE`/`UNKNOWN`; required state could not be read. Never emitted with an optimistic or empty payload. |

When a command cannot read the state it needs, it prints an
`cao.cli.unavailable.v0` payload and exits `3`:

```json
{
  "schema": "cao.cli.unavailable.v0",
  "status": "UNAVAILABLE",
  "group": "work",
  "command": "list",
  "error": {
    "code": "REGISTRY_UNAVAILABLE",
    "verdict": "UNAVAILABLE",
    "message": "RegistryError: no registry database at ..."
  }
}
```

Defined `error.code` values: `REGISTRY_UNAVAILABLE` (the registry ledger could
not be opened), `RELAY_STATE_NOT_FOUND` (the named relay state file does not
exist), `RELAY_STATE_UNREADABLE` (the state file could not be read or is not a
JSON object), `RELAY_STATE_INVALID` (the state is not a valid relay ledger).

## Runtime plugins

```sh
cao plugins list --json
```

Reports the runtime plugins installed under the `cao.runtime_plugins`
entry-point group. A plugin is only listed when it is actually installed: CAO
ships **no built-in default runtime**. An environment with no plugin installed
reports an empty, honest list and exits `0`.

```json
{
  "schema": "cao.cli.plugins.v0",
  "status": "OK",
  "plugins": [
    {
      "id": "herdr",
      "available": true,
      "descriptor": {
        "id": "herdr",
        "name": "Herdr",
        "version": "protocol-22",
        "capabilities": [
          "capability.discovery",
          "snapshot.capture",
          "target.inspect",
          "session.prompt",
          "execution.start"
        ]
      },
      "reason": null
    }
  ]
}
```

A plugin that is installed but cannot be loaded stays listed with
`"available": false` and a `reason`; it is never silently dropped.

### Runtime capability support

Support is declared by each plugin, not by the core. The Herdr plugin currently
advertises the capabilities below; the rows marked **unavailable** are
explicitly not offered and are reported as `UNAVAILABLE` evidence rather than
being attempted or substituted.

| Capability | Herdr |
| --- | --- |
| `capability.discovery` | supported |
| `snapshot.capture` | supported |
| `target.inspect` | supported |
| `session.prompt` | supported |
| `execution.start` | supported for an **EXISTING** verified target only; `PROVISION` is **unavailable** (no ownership record or cleanup policy, so no pane or workspace is ever created) |
| `output.observe` | **unavailable** (no event boundaries to map) |
| `session.close` | **unavailable** |

An unavailable capability is reported as explicit `UNAVAILABLE` evidence; there
is no fallback runtime and no fabricated result.

`execution.start` reads one snapshot to positively verify a caller-supplied
existing target before it starts anything, and `session.prompt` submits one
prompt. Both have a deliberate hazard worth stating: once the write has been
issued, a later failure (including the plugin's own returned-identity check)
still raises even though the prompt or start may already have landed. The
plugin has no retry, idempotency or deduplication of its own, so a caller that
blindly retries after such an error can submit the same prompt or start twice.
Treat a post-send error as **unknown**, not as "nothing happened".

## Work registry commands

These commands read the Work Registry ledger. The ledger is a SQLite database
owned by `cao.registry`; the CLI opens it **read-only** and never creates or
migrates it. If the named ledger does not exist, the command exits `3` with
`REGISTRY_UNAVAILABLE`.

`--ledger PATH` is optional. When omitted, the CLI uses the registry module's
own default path (`XDG_STATE_HOME` when set, otherwise the user's default state
root, under `cao-work-registry/registry.sqlite3`). The path is never discovered
by scanning the filesystem.

### `cao work list`

Lists proposed work items recorded in the ledger.

```sh
cao work list --ledger /path/to/registry.sqlite3
cao work list --ledger /path/to/registry.sqlite3 --run <run-id>
```

```json
{
  "schema": "cao.cli.work.v0",
  "status": "OK",
  "ledger": "/path/to/registry.sqlite3",
  "run_id": null,
  "count": 2,
  "works": [
    {
      "run_id": "run-a",
      "work_key": "work-1",
      "issue": "",
      "repo": "/path/to/repo",
      "paths": ["src/alpha"],
      "components": [],
      "objective": "synthetic work one",
      "created_at": "2026-01-01T00:00:00Z"
    }
  ]
}
```

An empty ledger is a real answer: `count` is `0` and the exit code is `0`.

### `cao worker list`

Lists active claims, joined to the scope of the work they hold. A "worker" here
is the owner of an open claim.

```sh
cao worker list --ledger /path/to/registry.sqlite3
cao worker list --ledger /path/to/registry.sqlite3 --run <run-id>
```

```json
{
  "schema": "cao.cli.worker.v0",
  "status": "OK",
  "ledger": "/path/to/registry.sqlite3",
  "run_id": null,
  "count": 1,
  "workers": [
    {
      "claim_id": 1,
      "run_id": "run-a",
      "work_key": "work-1",
      "issue": "",
      "owner": "writer-a",
      "mode": "READ",
      "branch": "",
      "worktree": "",
      "claimed_at": "2026-01-01T00:00:00Z",
      "repo": "/path/to/repo",
      "paths": ["src/alpha"],
      "components": [],
      "objective": "synthetic work one"
    }
  ]
}
```

### `cao run list`

Lists runs, derived directly from the work items returned by
`registry.list_works`. It does not maintain a second store; it is a grouping of
the ledger's own work.

```sh
cao run list --ledger /path/to/registry.sqlite3
```

```json
{
  "schema": "cao.cli.run.v0",
  "status": "OK",
  "ledger": "/path/to/registry.sqlite3",
  "derived_from": "registry.list_works",
  "count": 1,
  "runs": [
    {"run_id": "run-a", "work_count": 2, "work_keys": ["work-1", "work-2"]}
  ]
}
```

### `cao coordinator status`

Reconciles each active registry claim (the *intended* state) against *observed*
state. This lane ships no runtime observation source, so the observed state is
genuinely unavailable: every subject is reported `UNKNOWN` with `UNAVAILABLE`
evidence and the command exits `3`. That is deliberate fail-closed behaviour,
not a silent success.

```sh
cao coordinator status --ledger /path/to/registry.sqlite3
```

```json
{
  "schema": "cao.cli.coordinator.v0",
  "status": "UNKNOWN",
  "ledger": "/path/to/registry.sqlite3",
  "run_id": null,
  "counts": {"AGREEMENT": 0, "DISAGREEMENT": 0, "UNKNOWN": 1},
  "subjects": [
    {
      "subject": "run-a/work-1#@writer-a",
      "verdict": "UNKNOWN",
      "intended_state": "READ",
      "observed_state": null,
      "evidence": {
        "source": "cli",
        "verdict": "UNAVAILABLE",
        "reason": "probe could not observe subject: no runtime observation source is configured"
      }
    }
  ]
}
```

An empty ledger has nothing to reconcile: `counts` are all `0`, the status is
`OK`, and the exit code is `0`.

## Relay command

### `cao relay status`

Reports a relay deduplication state file (schema `cao.relay.v0`), read through
`cao.state.read_state` and `cao.relay.load_ledger`. The path is required; an
absent, unreadable or malformed file exits `3` as `UNAVAILABLE`.
`examples/synthetic_ledger.json` is such a relay state file, not a registry
ledger; it is valid only for `relay status --state`, and passing it to a
`--ledger` command correctly reports `REGISTRY_UNAVAILABLE`.

```sh
cao relay status --state examples/synthetic_ledger.json
```

```json
{
  "schema": "cao.cli.relay.v0",
  "status": "OK",
  "state": "examples/synthetic_ledger.json",
  "max_identities": 4096,
  "delivered_count": 2,
  "delivered": ["synthetic-event-0001", "synthetic-event-0002"],
  "writers": {
    "synthetic-writer-a": "sha256:0000...",
    "synthetic-writer-b": "sha256:1111..."
  }
}
```

## What is not implemented

There is no command that starts, stops, prompts, applies, syncs or otherwise
mutates state. Runtime capabilities beyond each plugin's declared set are
explicitly unavailable. No plugin ships as a built-in default until one is
installed.
