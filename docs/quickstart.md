# Quickstart

```sh
git clone https://github.com/codxt/cao
cd cao
./setup.sh
./cao doctor
```

That is the whole source setup. `./setup.sh` writes the CAO product
configuration and installs or adopts the pinned runtime binary; `./cao doctor`
reports whether the install is actually usable.

This quickstart uses the effectful product CLI, which drives the managed runtime
through that runtime's own command line. The read-only foundation commands
(`cao plugins list`, `cao work list`, …) are unchanged and separate; see
[`operators.md`](operators.md).

## Reading the verdict

`./cao doctor` prints JSON and exits `0` only when every readiness gate passes:

```json
{
  "schema": "cao.product.doctor.v0",
  "status": "NOT_READY",
  "gates": {
    "managed_runtime": false,
    "runtime_server": false,
    "provider_found": false,
    "model_configured": false,
    "credentials_verified": false
  },
  "reasons": [
    "managed runtime is MISSING",
    "runtime server is UNAVAILABLE",
    "provider is NOT_CONFIGURED"
  ]
}
```

`NOT_READY` is honest, not broken: it means a gate is genuinely not satisfied
yet. A fresh clone with no provider configured is `NOT_READY`. Verification
never reads a credential value.

`doctor` also probes the managed runtime's **live server** with a bounded,
read-only `status server` call. If the server is not reachable the verdict is
`NOT_READY`, even when every other gate passes. The CAO daemon is **not**
required for a manual task (`cao run` drives the runtime directly) and is
reported separately under `readiness.unattended`, which is true only when a
daemon is also running.

## Checking the service and running a task

After `./setup.sh` and an explicit provider configuration (see
[`installation.md`](installation.md)), the product commands work:

```sh
./cao run --repo /path/to/project "Review this repository"
./cao status
./cao logs
./cao stop
```

A review task claims `READ` authority in the Work Registry; a code-writing task
must declare its scope explicitly (`--mode write --write <path>...`) and is
refused without one. A scoped write claims `WRITE` only after the CLI reads the
runtime's live occupancy and the registry gate accepts it; an unreadable or
conflicting occupancy is refused, never assumed safe. A prompt is submitted
exactly once: re-running the same task id resumes from the durable checkpoint
and never duplicates the prompt, and an ambiguous acknowledgement holds the
task `UNKNOWN` rather than re-sending it. Completion hands back with the
observed repository HEAD.

`./cao status` merges the durable service record with the latest task record;
on a fresh clone it reports `NOT_STARTED` — CAO never invents a running
service. Without a product configuration these commands report explicit
`UNAVAILABLE` and a nonzero exit; they never fabricate progress, handback or
a process.

## The orchestration daemon

```sh
./cao daemon start
./cao daemon status
./cao daemon stop
```

A CAO-owned, user-level loop: durable desired/observed state, one idempotent
reconciliation tick per interval, a deduplicated manager wake, and bounded
retries that hold `UNKNOWN` instead of fabricating completion.

## Next steps

- [`installation.md`](installation.md) — flags, paths and the security model.
- [`operators.md`](operators.md) — the read-only control-plane CLI.
- [`product-pilot.md`](product-pilot.md) — the real disposable-workspace pilot.
