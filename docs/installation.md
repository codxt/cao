# Installing CAO from source

CAO runs from a clone with nothing but the standard library. There is no wheel
to build and no package manager required.

## Requirements

- Linux on `x86_64` or `aarch64` for the managed runtime dependency. The CAO
  control plane itself is pure Python.
- Python 3.10 or newer (`python3`).
- A POSIX `sh`.

The CAO core imports with `python3 -S` (no third-party packages on the path).

## Steps

```sh
git clone https://github.com/codxt/cao
cd cao
./setup.sh
./cao doctor
```

`setup.sh` is idempotent: running it again changes nothing.

- `./setup.sh` — adopt a compatible existing runtime binary if one is on
  `PATH`, otherwise install a CAO-managed copy.
- `./cao doctor` — print exact per-subsystem readiness evidence as JSON.
  Anything that cannot be positively established stays `UNKNOWN`/`UNAVAILABLE`
  and the verdict is `NOT_READY` with a nonzero exit.

## What `setup.sh` does

1. Writes an idempotent product configuration into the XDG config directory
   (`$XDG_CONFIG_HOME/cao/config.json`, default `~/.config/cao/config.json`).
   An existing provider selection is never overwritten.
2. Resolves the host platform and installs the pinned runtime release through
   `src/cao/bootstrap.py`:
   - the release is fetched **only** from its official GitHub release URL over
     HTTPS, with a timeout and a size cap;
   - the SHA-256 is verified **before** anything is put in place, so a wrong or
     truncated download can never replace a working binary;
   - the install is atomic (`os.replace`) into an isolated CAO-managed
     directory (`$XDG_STATE_HOME/cao/managed/`, default
     `~/.local/state/cao/managed/`), separate from any unrelated copy; and
   - there is no `curl`-to-shell path and no unverified binary is ever executed.

### Options

| Flag | Effect |
| --- | --- |
| *(none)* | Adopt a compatible existing binary, otherwise install a managed copy. |
| `--adopt-existing` | Require an existing compatible binary; never install. An incompatible one is reported and left untouched. |
| `--download` | Always install a CAO-managed copy, ignoring any external binary. |
| `--no-download` | Verify only; fetch and install nothing. |
| `--force` | Rewrite the managed fields in the configuration. |

Setting `CAO_PYTHON` selects the interpreter used for the bootstrap step.

## Provider and model selection

Installation does not itself authenticate anything. To be considered `READY`,
`cao doctor` requires:

- the managed runtime dependency to be installed or adopted;
- the runtime's **live server** to answer a bounded, read-only `status server`
  probe (a missing or unhealthy server is `NOT_READY`);
- a provider CLI that is present on `PATH`;
- an explicit model recorded in the configuration; and
- credentials that can be positively verified **without reading a secret**.

The CAO daemon is **not** part of `READY`: a manual `cao run` drives the runtime
directly. The daemon is required only for unattended supervision, and its real
status is reported separately (`sections.daemon` and `readiness.unattended`),
never fabricated.

CAO reads only non-secret existence signals (for example, which provider ids a
CLI reports as configured). It never reads, stores or prints a credential
value. If a provider cannot be verified this way, the verdict stays
`NOT_READY` — that is the honest answer, not an error.

## Paths

| Purpose | Environment variable | Default |
| --- | --- | --- |
| Configuration | `XDG_CONFIG_HOME` | `~/.config` |
| Product state and managed binaries | `XDG_STATE_HOME` | `~/.local/state` |
| Work Registry ledger | `CAO_REGISTRY_DB` (highest priority), then product config `registry_db`, then `XDG_STATE_HOME` | `~/.local/state/cao-work-registry/registry.sqlite3` |

All three roots are honoured at call time, so a sandbox can isolate CAO
completely by pointing them at a temporary directory. A selected
`CAO_REGISTRY_DB` must be the **single authoritative WRITE claim ledger** for
all dispatchers that could act on the same repositories. `cao doctor` reports
the same effective registry path as task execution and never creates a ledger
while checking readiness. Setting the path alone does not establish policy or
claim-protocol compatibility with an external dispatcher. When sharing a
host with an existing dispatcher, configure its authoritative ledger in the product
config (for example, `"registry_db": "/absolute/path/to/shared/registry.sqlite3"`)
so manual CLI, doctor and daemon agree even without an inherited environment
variable. The path must be absolute and normalized; no ledger is copied or
merged, and `CAO_REGISTRY_DB` remains an explicit override.

## The read-only core CLI

The accepted control-plane CLI is unchanged and still available:

```sh
PYTHONPATH=src python3 -m cao --help
PYTHONPATH=src python3 -m cao plugins list --json
```

`./cao` is the product-facing surface for bootstrap, readiness and task
lifecycle. It implements `run`, `stop`, `logs`, `status` and `daemon`
(`start`/`stop`/`status`) with governed task ownership. The separate bare
`./cao start` shortcut is not implemented and returns explicit `UNAVAILABLE`.
`cao doctor` reports `NOT_READY` when an explicit OpenCode agent-argument
override would select a different model from `provider.model`.
