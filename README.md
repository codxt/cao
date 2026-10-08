# CAO

CAO (Chief Agent Officer) is an open-source **AgentOps control plane for
agentic runtimes**. It gives operators one place to discover runtime
capabilities, capture state, inspect targets, observe output, start work,
prompt sessions, and enforce authority — without coupling the core to any one
runtime.

CAO is **runtime neutral**. A runtime integrates through a plugin selected
explicitly by id. Herdr is the first first-class runtime plugin; it is
registered through an installed entry point and is **not** a built-in default.
Core identities are generic: runtime, workspace, execution, and session ids.

## Status

This repository provides a usable, **read-only** control-plane CLI:

- runtime plugin discovery (`cao plugins list`);
- read-only registry views over proposed work, active claims, and runs
  (`cao work list`, `cao worker list`, `cao run list`);
- explicit intended-vs-observed reconciliation status
  (`cao coordinator status`);
- relay deduplication-state inspection (`cao relay status`).

Every command reports structured JSON, delegates to the module that owns the
state, and fails closed. The CLI never mutates state and never fabricates a
result: when required state cannot be read it prints explicit `UNAVAILABLE`
evidence and exits nonzero.

## Install

Requires Python 3.10+.

```sh
python3 -m pip install -e .
cao plugins list --json
```

Installing the package registers the Herdr runtime plugin under the
`cao.runtime_plugins` entry-point group, so `cao plugins list` reports it with
its real id and advertised capabilities. There are no runtime dependencies
(`dependencies = []`).

To run straight from a checkout without installing:

```sh
PYTHONPATH=src python3 -m cao --help
```

## CLI

```sh
cao plugins list --json
cao work list --ledger /path/to/registry.sqlite3
cao worker list --ledger /path/to/registry.sqlite3
cao run list --ledger /path/to/registry.sqlite3
cao coordinator status --ledger /path/to/registry.sqlite3
cao relay status --state examples/synthetic_ledger.json
```

`plugins list` reports registered runtime plugins by explicit id. With no
plugins installed the result is an empty, honest list — not a fallback
runtime.

The registry commands read a Work Registry SQLite ledger read-only. `--ledger`
defaults to the registry module's own default state path; the CLI never scans
the filesystem for state. `examples/synthetic_ledger.json` is a relay
deduplication state file, not a registry ledger.

Exit codes are `0` (success), `2` (usage error), and `3` (explicit
`UNAVAILABLE`/`UNKNOWN`). See [`docs/operators.md`](docs/operators.md) for the
exact JSON schema of every command and the runtime capabilities that are
supported versus explicitly unavailable.

## Design in one paragraph

Runtime execution, Work Registry ownership, reconciliation, and
policy/authority are four separate concerns. CAO's core keeps them separate:
`runtime.py` talks to plugins, `policy.py` authorizes the exact requested
action, `state.py` persists state and fails loudly on bad input, and
`evidence.py` qualifies every observation. Required evidence that cannot be
read stays `UNKNOWN` and fails closed. The CLI composes these modules rather
than reimplementing any of their logic.

See [`docs/architecture.md`](docs/architecture.md),
[`docs/runtime-plugin.md`](docs/runtime-plugin.md),
[`docs/operators.md`](docs/operators.md), and
[`docs/extraction-provenance.md`](docs/extraction-provenance.md).

## Development and validation

```sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
  python3 -B -m unittest discover -s tests -p 'test_*.py' -v

PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
  python3 -B -S -m unittest discover -s tests -p 'test_*.py' -v

PYTHONPATH=src python3 -B -S -m cao --help
PYTHONPATH=src python3 -B -S -m cao plugins list --json
git diff --check
```

The `-S` run proves the core imports with no third-party packages on the path.

See [`AGENTS.md`](AGENTS.md) for contribution boundaries and
[`examples/`](examples) for a synthetic demo runtime.
