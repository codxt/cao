# CAO architecture (F0)

CAO is an AgentOps control plane for agentic runtimes. It observes and drives
runtimes through a stable, runtime-neutral contract while keeping authority,
state, and execution as separate concerns.

## Layering

```text
        +------------------------------------------------------+
        |                    cao CLI                            |
        |   plugins list        (implemented in F0)             |
        |   work|worker|run|coordinator|relay   (UNAVAILABLE)   |
        +---------------------------+--------------------------+
                                    |
             +----------------------+----------------------+
             |                      |                      |
             v                      v                      v
     +--------------+       +--------------+       +--------------+
     |   policy.py  |       |  runtime.py  |       |   state.py   |
     | authority on |       | plugin iface |       | loud state   |
     | exact action |       | + selection  |       | I/O          |
     +-------+------+       +-------+------+       +-------+------+
             |                      |                      |
             +----------+-----------+----------+-----------+
                        v                      v
                 +--------------+      +--------------+
                 | evidence.py  |      | contracts.py |
                 | qualified    |      | generic ids  |
                 | observations |      | and payloads |
                 +--------------+      +--------------+
```

Arrows point from a concern to the modules it depends on. The directions are
deliberate and enforced by `tests/test_boundaries.py`.

## Concerns, kept separate

1. **Runtime execution** (`runtime.py`). Talks to a runtime through a plugin.
   Knows nothing about ownership, authority, or persistence.
2. **Work Registry ownership** (later lane). Who may write a scope. F0 does not
   implement or import it.
3. **Reconciliation** (later lane). Whether a run is making progress. F0 does
   not implement or import it.
4. **Policy and authority** (`policy.py`). Whether the exact requested action
   is authorized. Independent of runtime execution.

Because these are separate, a runtime cannot grant itself authority, and an
authority check cannot reach into a runtime's internals. The CLI composes them
rather than any module reaching across the boundary.

## Generic identities

CAO owns the *roles* of identities, not their contents:

| Identity | Meaning |
| --- | --- |
| runtime | A runtime integration, selected explicitly by plugin id |
| workspace | A named area a runtime exposes |
| execution | One launched unit of work inside a workspace |
| session | A promptable conversation inside an execution |

## Capabilities

The runtime contract exposes seven generic capabilities:

| Capability | Operation |
| --- | --- |
| `capability.discovery` | Describe the runtime and what it offers |
| `snapshot.capture` | Capture exactly one snapshot of a workspace |
| `target.inspect` | Inspect one target |
| `output.observe` | Observe output and events |
| `execution.start` | Start an execution |
| `session.prompt` | Prompt a session |
| `session.close` | Close a session (optional) |

A plugin declares the capabilities it offers. When a caller asks for one the
plugin does not offer, it receives explicit `UNAVAILABLE` evidence and no
value — never a fallback runtime and never a fabricated result.

## Evidence

Every observation is an `Evidence` value carrying:

- **source** — where it came from (plugin, runtime, policy, state, CLI, internal);
- **verdict** — `SUPPORTED`, `UNAVAILABLE`, `UNKNOWN`, `REFUSED`, or `INVALID`;
- **capture time** — RFC 3339 UTC;
- **fingerprint** — a stable `sha256:` content fingerprint;
- **coverage** — the fraction captured, when meaningful;
- **reason** — why the verdict was reached.

Only `SUPPORTED` is positive. Callers that must not proceed on unreadable facts
call `Evidence.require_supported()`, which raises. This is how "required
unreadable evidence stays `UNKNOWN` and fails closed" is enforced in code.

## Policy

Policy answers one question: is this precise action authorized? A decision is
made by matching the requested action field by field against grants:
kind, plugin id, workspace id, and optional execution/session narrowing. A
grant to start one runtime in one workspace does not authorize closing it,
prompting a different workspace, or acting on another plugin.

The engine is deny-by-default. With no matching grant the verdict is `REFUSED`.
If the policy source cannot be read or understood, every decision is `UNKNOWN`
and denied.

## State

`state.py` owns only the mechanics of JSON object state:

- an absent file returns `None` (first-run bootstrap);
- invalid JSON raises `StateCorruptError`;
- valid JSON that is not an object raises `StateSchemaError`;
- an unreadable file raises `StateUnreadableError`;
- writes are atomic (temporary file, fsync, replace) and verified by reading
  back.

State *vocabulary* belongs to the lane that owns a state file. F0 defines no
run-state machine.

## CLI surface

The `plugins` group is implemented. The `work`, `worker`, `run`, `coordinator`,
and `relay` groups are scaffolded so their command shape is stable; every
command under them prints a structured `UNAVAILABLE` result and exits nonzero.

```json
{
  "schema": "cao.cli.unavailable.v0",
  "status": "UNAVAILABLE",
  "group": "work",
  "command": "list",
  "error": {
    "code": "LANE_NOT_IMPLEMENTED",
    "verdict": "UNAVAILABLE",
    "message": "'work list' is scaffolded but not implemented in F0"
  }
}
```
