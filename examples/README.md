# Examples

Synthetic only. Nothing here is a real runtime or a real policy.

- `synthetic_runtime.py` — an in-memory demo plugin implementing the frozen
  runtime contract, with a runnable self-check.
- `policy.example.json` — an example policy granting a few exact actions to the
  synthetic plugin.

Run the self-check from the repository root:

```sh
PYTHONPATH=src python3 -B examples/synthetic_runtime.py
```

Load the example policy:

```python
from cao.policy import PolicyEngine

engine = PolicyEngine.load("examples/policy.example.json")
```
