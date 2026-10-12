# Runtime plugin contract

CAO never names a concrete runtime in its core. A runtime integrates by
providing a `RuntimePlugin` and registering it under an explicit id. This
document is the frozen plugin contract for F0.

This is the runtime-neutral, in-process plugin contract (surface 1 in
[`architecture.md`](architecture.md)). It is separate from the product layer's
managed-runtime CLI protocol (`product_driver.py`), which drives one installed
runtime through that runtime's own command line and is not a plugin.

## Selecting a plugin

Plugins are selected explicitly by id through `PluginRegistry`:

```python
from cao.runtime import PluginRegistry, RuntimeClient

registry = PluginRegistry.discover()   # entry-point group: cao.runtime_plugins
client = RuntimeClient(registry.resolve("your-runtime-id"))
```

Resolution never falls back. An unknown id raises `PluginNotFound` with code
`PLUGIN_NOT_FOUND`. `PluginRegistry.discover()` reads the
`cao.runtime_plugins` entry-point group; with no plugins installed it returns
an empty registry, which is the honest result — CAO ships no built-in runtime.

A plugin that fails to load is not silently dropped: `registry.entries()` (used
by `cao plugins list`) reports it as unavailable with a reason.

## The interface

Subclass `RuntimePlugin` and declare identity, version, and capabilities.
Override only the capability methods you actually support.

```python
from cao.contracts import Capability, ExecutionRef, StartRequest
from cao.runtime import RuntimePlugin


class YourRuntime(RuntimePlugin):
    runtime_name = "Your Runtime"
    runtime_version = "1.0"

    @property
    def plugin_id(self) -> str:
        return "your-runtime-id"

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.DISCOVERY, Capability.START})

    def start(self, request: StartRequest) -> ExecutionRef:
        ...
```

| Method | Capability | Required? |
| --- | --- | --- |
| `describe()` | `capability.discovery` | provided by the base class |
| `snapshot(workspace_id)` | `snapshot.capture` | optional |
| `inspect(target)` | `target.inspect` | optional |
| `observe(execution)` | `output.observe` | optional |
| `start(request)` | `execution.start` | optional |
| `prompt(session, text)` | `session.prompt` | optional |
| `close(session)` | `session.close` | optional |

Unimplemented methods inherit a body that raises `UnsupportedCapability`.

## What the client guarantees

`RuntimeClient` is the only surface the rest of CAO uses to reach a runtime. For
every operation it returns a `CapabilityResult` pairing the value with its
`Evidence`.

- If the plugin declares the capability, the client calls it and returns
  `SUPPORTED` evidence whose fingerprint is derived from the returned value.
- If the plugin does **not** declare it, the client returns `UNAVAILABLE`
  evidence, `value is None`, and does **not** call the plugin.
- If a declared capability returns `None`, the client returns `UNKNOWN`
  evidence. It never treats an absent value as success.
- If a reference passed to the client is bound to a different runtime, the
  client returns `INVALID` evidence and does **not** call the plugin.
- If a declared capability returns a value that violates the contract — the
  wrong type, a result bound to another runtime/workspace/execution/session, or
  a snapshot with missing or non-finite metadata — the client returns `INVALID`
  evidence with `value is None`. A malformed result is never positive
  execution evidence.

There is no silent fallback to another plugin, no default runtime, and no
fabricated result.

### Snapshot evidence

A snapshot is captured exactly once. The plugin supplies the capture time,
fingerprint, and coverage on the `Snapshot`; the client carries those into the
evidence unchanged, so an operator can see what was actually seen rather than
what CAO assumed. The client never synthesizes a capture time for a plugin
snapshot: a snapshot with an invalid capture time (empty, not RFC 3339, or
without an explicit timezone), an empty fingerprint, non-finite or
out-of-range coverage, or a wrong runtime/workspace binding is `INVALID` and
non-positive.

```python
result = client.snapshot("workspace-1")
if result.ok:
    snapshot = result.value
    print(result.evidence.coverage, result.evidence.fingerprint)
```

### Optional close

`session.close` is optional. A runtime that does not offer it returns
`UNAVAILABLE` for `close`, and callers must handle that explicitly.

## Authorizing actions

Policy is a separate concern. Before performing an action, compose it with
`PolicyEngine`:

```python
from cao.policy import ActionKind, ActionRequest, PolicyEngine

engine = PolicyEngine.load("policy.json")
decision = engine.authorize(
    ActionRequest(ActionKind.START, "your-runtime-id", "workspace-1")
)
if not decision.allowed:
    ...
```

The check is against the exact requested action: kind, plugin id, workspace id,
and any execution/session narrowing. See `docs/architecture.md`.

## Testing a plugin

`tests/support.py` contains a synthetic `FakeRuntimePlugin` used by the CAO
test suite. `examples/synthetic_runtime.py` is a small standalone demo plugin.
Neither is a shipped runtime.
