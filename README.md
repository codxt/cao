# CAO

CAO (Chief Agent Officer) is an open-source **AgentOps control plane for
agentic runtimes**. It gives operators one place to discover runtime
capabilities, capture state, inspect targets, observe output, start work,
prompt sessions, and enforce authority — without coupling the core to any one
runtime.

CAO is **runtime neutral**. A runtime integrates through a plugin. Herdr is
planned as the first first-class runtime plugin, but it is **not referenced or
implemented** in this foundation release. Core identities are generic:
runtime, workspace, execution, and session ids.

## Status: F0 foundation

This repository currently contains the **F0 shared foundation** only:

- runtime-neutral contracts and capability discovery;
- one-shot snapshot capture, target inspection, and output/event observation;
- start, prompt, and optional close;
- explicit plugin selection with no silent fallback;
- evidence that preserves source, verdict, capture time, fingerprint,
  coverage, and reason;
- policy authorization against the exact requested action;
- state I/O that fails loudly on corrupt or unreadable state.

Later lanes add the `work`, `worker`, `run`, `coordinator`, and `relay`
command groups. Their CLI shape is scaffolded now, and every scaffolded command
reports a structured `UNAVAILABLE` result with a nonzero exit status rather
than pretending to succeed.

### Non-goals for F0

- No runtime implementation ships in the core, and no runtime is imported.
- No Work Registry, reconciliation, lifecycle, launcher, relay, portfolio, or
  Git modules (owned by later lanes).
- No runtime dependencies; the core uses only the Python standard library.

## Install

Requires Python 3.10+.

```sh
python3 -m pip install -e .
```

To run straight from a checkout without installing:

```sh
PYTHONPATH=src python3 -m cao --help
```

## CLI

```sh
cao --help
cao plugins list --json
```

`plugins list` reports registered runtime plugins by explicit id. With no
plugins installed the result is an empty, honest list — not a fallback
runtime.

Scaffolded lane groups report structured `UNAVAILABLE`:

```sh
cao work list --json   # exit status 3, structured UNAVAILABLE
cao run list --json
```

## Design in one paragraph

Runtime execution, Work Registry ownership, reconciliation, and
policy/authority are four separate concerns. CAO's core keeps them separate:
`runtime.py` talks to plugins, `policy.py` authorizes the exact requested
action, `state.py` persists state and fails loudly on bad input, and
`evidence.py` qualifies every observation. Required evidence that cannot be
read stays `UNKNOWN` and fails closed.

See [`docs/architecture.md`](docs/architecture.md),
[`docs/runtime-plugin.md`](docs/runtime-plugin.md), and
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
