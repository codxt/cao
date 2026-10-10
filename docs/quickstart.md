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

## Reading the verdict

`./cao doctor` prints JSON and exits `0` only when every readiness gate passes:

```json
{
  "schema": "cao.product.doctor.v0",
  "status": "NOT_READY",
  "gates": {
    "managed_runtime": false,
    "provider_found": false,
    "model_configured": false,
    "credentials_verified": false
  },
  "reasons": ["managed runtime is MISSING", "provider is NOT_CONFIGURED"]
}
```

`NOT_READY` is honest, not broken: it means a gate is genuinely not satisfied
yet. A fresh clone with no provider configured is `NOT_READY`. Verification
never reads a credential value.

## Checking the service

```sh
./cao status
```

On a fresh clone this reports `NOT_STARTED` — CAO never invents a running
service. The task-running lifecycle is owned by later lanes; in this build:

```sh
./cao run --repo /path/to/project "Review this repository"
# { "status": "UNAVAILABLE", "error": { "code": "NOT_IMPLEMENTED" } }
```

`run`, `start`, `stop` and `logs` return an explicit `UNAVAILABLE` and a
nonzero exit. They never fabricate progress, handback or a process.

## Next steps

- [`installation.md`](installation.md) — flags, paths and the security model.
- [`operators.md`](operators.md) — the read-only control-plane CLI.
