# CAO

CAO (Chief Agent Officer) is an open-source **AgentOps control plane for
agentic runtimes**. It gives operators one place to discover runtime
capabilities, capture state, inspect targets, observe output, start work,
prompt sessions, and enforce authority — without coupling the core to any one
runtime.

CAO's core is **runtime neutral**. A runtime integrates through an in-process
plugin selected explicitly by id; core identities are generic: runtime,
workspace, execution, and session ids. Herdr is the first first-class runtime
plugin; it is registered through an installed entry point and is **not** a
built-in default.

The product layer is a **second, separate surface**. `./setup.sh` and the
effectful `./cao` commands drive one installed, pinned managed runtime through
that runtime's own command-line protocol — currently Herdr-shaped `workspace`,
`tab`, `pane` and `agent` verbs with JSON result envelopes. The product driver
keeps the executable as data and names no runtime in its code, but the protocol
it speaks is the managed runtime's CLI. It is not a second plugin abstraction
and does not replace CAO's runtime-neutral contract.

## Status

CAO ships two surfaces from this tree, and they differ in effect.

The **read-only foundation** is the accepted F0 core CLI. Every command reports
structured JSON, delegates to the module that owns the state, fails closed, and
never mutates state:

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

The **effectful product CLI** is `./cao`, built on the source bootstrap. It
drives the installed managed runtime and owns task, team and daemon state:
`./setup.sh` installs or adopts the pinned runtime, `./cao doctor` reports
readiness, `./cao run`/`status`/`logs`/`stop` drive one governed task, and
`./cao daemon start|stop|status` adds optional supervision. The specialist
`cao team` commands build on the same task ownership. These commands can start
an agent and write state under the CAO roots; they fail closed with an explicit
`UNAVAILABLE` or `NOT_READY` (exit `3`) instead of fabricating progress.

Neither surface invents a result: when required state cannot be read, the
command prints explicit `UNAVAILABLE` evidence and exits nonzero.

`cao intake` is the inbound gate, its on-demand advisory triage, its governed
routing compiler and its request-to-resolution projection. `submit`, `list`,
`show` and `pull` admit and read private submissions; `triage` reads one
admitted submission together with bounded read-only context and returns one
advisory, provenance-linked disposition; `compile` turns only a `PROPOSE_WORK`
packet plus bounded operator-supplied route evidence into one immutable,
reviewable `cao.team.v1` draft for the existing Team Execution path; `status`
derives a truthful requester status and the accountable next mover from existing
evidence, read-only and idempotent, with an optional redacted `--guest-view`.
Admission is not work, triage is not authority, a compiled draft is not approval
and a derived status is not acceptance: the accountable owner must still review
the draft and run `cao team init --approved` separately. `reply` serves only a
gated `--dry-run` or `--mock` origin-channel simulation; a real send is
`UNAVAILABLE`, because no authenticated delivery identity or outbound credential
exists. See
[`docs/intake.md`](docs/intake.md) and the
[B3–B5 governed-intake roadmap](docs/intake-roadmap.md).

`cao request` is the single send-to-CAO entrypoint: one bounded request is
decided `BACKLOG` (recorded for later), `INVESTIGATE` or `IMPLEMENT`, and is
routed to the existing Intake or governed Team Execution path without
dispatching anything. An implementation-intent request becomes a visible
execution candidate, but only a separately qualified exact team manifest and an
existing operator authorization can start one; the request text is never
approval.

## Install

Requires Python 3.10+ and a POSIX `sh`. The read-only core is pure Python and
runs wherever Python 3.10+ runs. The managed runtime the product layer installs
or adopts is supported on Linux `x86_64` and `aarch64`; on an unpinned platform
the readiness report marks the runtime dependency `UNSUPPORTED` rather than
substituting a different runtime.

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

### Portable agent skill

A first-party, vendor-neutral operator skill ships at
[`skills/cao-operator/`](skills/cao-operator/). It teaches a coding agent to
inspect and operate CAO within its gates, keeping the effectful product `./cao`
CLI separate from the read-only `python -m cao` CLI. Installing it only makes
the instructions discoverable; it grants no authorization and no live CAO
access. See [`docs/agent-skill.md`](docs/agent-skill.md) for portable symlink
installation into `~/.agents/skills`, `~/.codex/skills` and
`~/.config/opencode/skills`.

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

These assessment commands require no model or background service. They do
not claim work or start agents. Real provider observations need source-specific
sanitized adapters; input fingerprints assert provenance without authenticating
the source. See [Evidence-to-Investigation](docs/evidence-to-investigation.md).

### Optional continuous feedback (bounded)

An explicitly configured CAO daemon can scan a source-attested JSON inbox,
correlate observations against a declared resource graph, and admit one durable
READ-only investigator per incident. Evidence refs, source SHA, ownership,
precomputed metric questions, and potential impact stay attached to the same
incident. There is no new queue, scheduler, ledger, telemetry scraper, or
unattended WRITE authority.

    cao improve watch --file /path/to/private-watch.json --dry-run
    cao improve watch --file /path/to/private-watch.json

After independent cause/scope review, an operator may explicitly create a
scoped developer-plus-reviewer team for an isolated Git worktree with
`cao improve authorize`. The ordinary Work Registry gate still decides every
WRITE. Publish/deploy remain separately authorized; `verify` evaluates external
review/CI/deployment evidence and before/after guardrails, while `learn`
proposes detection changes without silently applying them. See the detailed
[feedback operating contract](docs/evidence-to-investigation.md).

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
[`docs/automations.md`](docs/automations.md),
[`docs/intake.md`](docs/intake.md),
[`docs/intake-roadmap.md`](docs/intake-roadmap.md), and
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
