# Portfolio admission planning (advisory)

`cao.portfolio_admission.plan_admission(proposals, snapshot, max_writers=2)`
returns deterministic ADMIT / DEFER / REJECT decisions in caller order.
It counts all active WRITE tasks across canonical repository identities against
one capacity ceiling; READ tasks consume no WRITE slots. Existing active task
IDs are rejected even when proposed as READ. Malformed, missing, unverified or
contradictory occupancy evidence raises `AdmissionError`, never an invented
zero-active snapshot. Repeated inputs produce identical plans.

The caller must supply `OccupancySnapshot(verified=True, tasks=...,
active_write_count=...)` from an actually verified reading. Both count and
identities must agree. A planning verdict is explicitly `ADVISORY`: it neither
reserves capacity nor authorizes execution. Immediately before any effectful
dispatch, use a **fresh** single-authority Work Registry preflight and atomic
claim, rechecking liveness, repository/path overlap, scope and permissions.
A stale snapshot or positive plan cannot override the Registry. A provider or
external-system failure cannot be interpreted as completed work or a free slot.

No scheduler, queue, persistent ledger, runtime plugin or provider fallback is
introduced by this module. External systems with partial effects require live
state reconciliation and an explicitly authorized recovery action before retry.
