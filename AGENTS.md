# AGENTS.md — working in CAO

This is the F0 shared foundation of CAO. Keep changes inside the frozen scope
below. If a task needs something outside it, that work belongs to a later lane
or to the accountable owner, not here.

## What CAO is

CAO is an open-source AgentOps control plane for agentic runtimes. The core is
runtime neutral; runtimes integrate through plugins. Herdr is planned as the
first first-class runtime plugin and **must not** be imported or implemented in
F0.

## Non-negotiable rules

- **Standard library only.** No runtime dependencies. The `-S` test run proves
  the core imports without third-party packages.
- **No private values.** No private paths, usernames, tenant names, real-run
  content, or internal operational values in code, docs, or examples.
  `test_boundaries.py` enforces a denylist.
- **Fail closed.** Required evidence that cannot be read stays `UNKNOWN`.
  Corrupt or unreadable state raises; it is never rounded up to a valid state.
- **No silent fallback.** An unavailable capability yields explicit
  `UNAVAILABLE` evidence. There is no default or substitute runtime.
- **Explicit plugin selection.** Plugins are resolved by id; an unknown id
  raises `PluginNotFound`.
- **Separate concerns.** Runtime execution, Work Registry ownership,
  reconciliation, and policy/authority do not leak into each other. Policy is
  checked against the exact requested action. `test_boundaries.py` enforces the
  import directions.

## Owned in F0

Files under `src/cao/`: `__init__.py`, `__main__.py`, `cli.py`, `contracts.py`,
`runtime.py`, `policy.py`, `evidence.py`, `state.py`; plus `tests/`, `docs/`,
`examples/`, `pyproject.toml`, `README.md`, `AGENTS.md`, and `.gitignore`.

## Not owned in F0 (later lanes)

Do not create lane modules. In particular, none of these may appear:

`registry.py`, `git.py`, `lifecycle.py`, `launcher.py`, `coordinator.py`,
`portfolio.py`, `relay.py`, `progress.py`, `observers/github.py`,
`plugins/herdr/**`.

No `LICENSE` file yet.

## Validation (required before handback)

```sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
  python3 -B -m unittest discover -s tests -p 'test_*.py' -v

PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
  python3 -B -S -m unittest discover -s tests -p 'test_*.py' -v

PYTHONPATH=src python3 -B -S -m cao --help
PYTHONPATH=src python3 -B -S -m cao plugins list --json
git diff --check
```

Remove generated caches (`__pycache__`, `.pyc`) before handback.

## Extending the core

- New generic identities or payload types belong in `contracts.py`.
- New qualified observations belong in `evidence.py`.
- New runtime operations are added to `RuntimePlugin` **and** `RuntimeClient`
  together, with an unavailable path and a test.
- The CLI `plugins` group is the only implemented command surface. Scaffolded
  lane commands must keep reporting structured `UNAVAILABLE`/nonzero until the
  owning lane implements them.
