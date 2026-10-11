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
- relay deduplication-state inspection (`cao relay status`);
- declarative automation manifest validation and state inspection
  (`cao automation validate`, `cao automation status`).

`cao automation run` is present and **always reports `UNAVAILABLE` with a
nonzero exit**: composing a brief needs a concrete source reader per declared
source, and CAO ships none. See [`docs/automations.md`](docs/automations.md).

Every command reports structured JSON, delegates to the module that owns the
state, and fails closed. The CLI never mutates state and never fabricates a
result: when required state cannot be read it prints explicit `UNAVAILABLE`
evidence and exits nonzero.

## Install

Requires Python 3.10+.

### From source (no wheel)

```sh
git clone https://github.com/codxt/cao
cd cao
./setup.sh
./cao doctor
./cao run --repo /path/to/project "Review this repository"
./cao status
```

`./setup.sh` writes an idempotent product configuration into the XDG config
directory and installs or adopts the pinned runtime release through a
stdlib-only installer that verifies the SHA-256 before anything is put in
place — no `curl`-to-shell, no unverified binary. `./cao doctor` prints exact
per-subsystem readiness evidence and fails closed: when the runtime's live
server is unreachable, a provider is missing, or its credentials cannot be
verified without reading a secret, the verdict is `NOT_READY`. Once a provider
is configured explicitly, `./cao run` submits one real task through the managed
runtime (a code-writing task must declare its write scope and is gated on live
occupancy), `./cao status` and `./cao logs` report durable state, and
`./cao stop` ends the run; `./cao daemon start` adds the CAO-owned supervision
loop, which is optional for manual tasks. See
[`docs/installation.md`](docs/installation.md),
[`docs/quickstart.md`](docs/quickstart.md) and
[`docs/product-pilot.md`](docs/product-pilot.md).

### As a package

```sh
python3 -m pip install -e .
cao plugins list --json
```

Installing the package registers the Herdr runtime plugin under the
`cao.runtime_plugins` entry-point group, so `cao plugins list` reports it with
its real id and advertised capabilities. There are no runtime dependencies
(`dependencies = []`).

To run the read-only control-plane CLI straight from a checkout without
installing:

```sh
PYTHONPATH=src python3 -m cao --help
```

## Team Execution

The product launcher supports specialist team roles, declared dependencies,
model-per-role routing, parallel governed workers, handoff artifacts and
bounded independent review/correction loops.

    cao team validate --file /path/to/team.json
    cao team init --file /path/to/team.json
    cao team start --id sample-project
    cao team tick --id sample-project
    cao team status --id sample-project

Initialization is PAUSED and launches no agent. Team review is advisory;
publication, deployment and sensitive provider routing keep separate gates.
See docs/team-execution.md for the manifest and recovery contract.

## Evidence-to-Investigation

The read-only CAO improve commands assess source-attested operating signals,
project explicit resource-to-source relationships, draft bounded investigation
proposals, compare release-bound before/after fitness and guardrail evidence,
and suggest detection-check improvements after repeated results:

    cao improve assess --file examples/improvement_case.json
    cao improve verify --file examples/improvement_case.json --after examples/improvement_after.json
    cao improve context --file examples/improvement_case.json --resource api

The check-proof command hashes a selected original evidence artifact and
compares its bytes to the source-attested reference without displaying it.

These commands require no model or background service. They never claim work,
start agents, change detection rules or deploy. Real provider observations
require source-specific adapters; input fingerprints are source assertions,
not independently authenticated proof. See
[Evidence-to-Investigation](docs/evidence-to-investigation.md).

## CLI

```sh
cao plugins list --json
cao work list --ledger /path/to/registry.sqlite3
cao worker list --ledger /path/to/registry.sqlite3
cao run list --ledger /path/to/registry.sqlite3
cao coordinator status --ledger /path/to/registry.sqlite3
cao relay status --state examples/synthetic_ledger.json
cao automation validate --config examples/automation_manifest.json
cao automation status --bookmarks /path/to/bookmarks.json
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
[`docs/operators.md`](docs/operators.md),
[`docs/automations.md`](docs/automations.md), and
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

## License

CAO is licensed under the Apache License, Version 2.0. The full text is in
[`LICENSE`](LICENSE).

That is the standard grant, including the express patent grant in section 3 and
its patent-termination clause. The copyright owner is **not named** in the
`LICENSE` file: the appendix still reads
`Copyright [yyyy] [name of copyright owner]`, because no holder has been
named yet.
