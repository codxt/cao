# AGENTS.md — working in CAO

CAO F0 is accepted. Later modules are implemented in explicitly owned lanes,
with separate worktrees and the shared writer-ownership gate. The accepted F0
contracts remain the baseline; a worker may change only the paths delegated to
its lane. Shared-contract changes require an accountable integration decision.

## What CAO is

CAO is an open-source AgentOps control plane for agentic runtimes. The core is
runtime neutral; runtimes integrate through plugins. Herdr is the designated first first-class runtime plugin. Its implementation
belongs under `src/cao/plugins/herdr/`; generic core modules never import it.

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

## Ownership and write discipline

Every write lane needs a registered, bounded WRITE claim before edits, and
independent concurrent writers require separate Git worktrees. The runtime
plugin observes execution; the Registry coordinates writer ownership; the
coordinator reconciles intended and observed state; configured policy decides
authority. Neither a live agent nor a valid claim authorizes deployment, release,
publication, or other reserved actions.

The accepted F0 foundation owns `src/cao/{contracts,runtime,evidence,policy,state,cli}.py`
and the package entry points. Those are shared interfaces: extension requires
integration review, not unilateral edits from a lane.

Initial disjoint implementation lanes:

- **Registry / Git:** `src/cao/registry.py`, `src/cao/git.py`, and dedicated
  registry/Git tests.
- **Herdr runtime plugin:** `src/cao/plugins/herdr/**` and dedicated plugin tests.
- **Worker lifecycle / launcher:** `src/cao/lifecycle.py`, `src/cao/launcher.py`,
  and dedicated lifecycle/launcher tests.
- **Coordination / portfolio:** `src/cao/coordinator.py`, `src/cao/portfolio.py`,
  `src/cao/observers/github.py`, and dedicated coordinator/portfolio tests.
- **Relay / progress:** `src/cao/relay.py`, `src/cao/progress.py`, and dedicated
  relay/progress tests.

Unspecified shared files, tests or docs remain with accountable integration.
Cross-lane interface changes require manager review. CLI commands without a
complete owning implementation must report structured `UNAVAILABLE` and a
nonzero exit, never fabricate successful work.

The initial implementation remains private. Do not add a `LICENSE` file, push,
publish, deploy, introduce a runtime dependency or move private operational
values without separate owner authorization.

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
- CLI features become available only with their owning implementation and
  evidence; unimplemented commands keep structured `UNAVAILABLE`/nonzero.
