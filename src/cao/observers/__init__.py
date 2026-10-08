"""Read-only observers that qualify external facts as evidence.

Observers never mutate anything and never reach the network at import time.
Transport is always injected by the caller, so importing this package has no
side effects: no socket, no subprocess and no real clock read.
"""

from __future__ import annotations

__all__: list[str] = []
