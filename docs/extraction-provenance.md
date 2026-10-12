# Extraction provenance

CAO's F0 foundation was distilled from an existing, governed control-plane
architecture. This document records what was extracted, what was deliberately
left behind, and the pinned source used for provenance.

## Pinned source

- Source: a private, governed autonomous-execution control-plane protocol and
  the supervising-agent skill that owns it. The accountable upstream source
  owns its own attribution; this public record carries only the attested facts
  below.
- Pinned source SHA: `0d16e9b5ac732d6ea353851cba0ed5bfe0b2b580`.
- Extraction mode: read-only. The source repository was **not** edited.

The pinned SHA identifies the reviewed state of the source material. This
extraction is a point-in-time distillation against that SHA; it is not a claim
of ongoing reconciliation with later source revisions.

## What was extracted

Only the reusable, runtime-neutral process contract:

1. **Separation of concerns.** The source keeps authority and acceptance,
   writer ownership and coordination, execution persistence, liveness and
   recovery, and external supervision distinct. F0 preserves that separation in
   its module boundaries: runtime execution, Work Registry ownership,
   reconciliation, and policy/authority do not leak into one another.

2. **Fail-closed evidence.** The source treats unreadable state as
   `INVALID`/`UNAVAILABLE` with continuation suppressed, never rounded up to a
   valid state. F0 encodes this in `evidence.py` (`UNKNOWN` stays unknown;
   `require_supported()` raises) and in `state.py` (corrupt or unreadable state
   raises rather than defaulting).

3. **Exact-action authorization.** The source keeps authority checks explicit
   and bounded. F0 encodes this as a deny-by-default policy engine that matches
   the exact requested action.

4. **One coherent capture.** The source requires a single captured snapshot as
   the evidence frame for a reconciliation pass. F0 exposes `snapshot.capture`
   as exactly one capture, carrying its own fingerprint and coverage.

## What was deliberately left behind

- **No runtime integration.** The source's persistent execution substrate is
  runtime-specific. F0 defines a runtime-neutral plugin contract and ships no
  runtime. Herdr integration is planned later as a separate plugin and is not
  imported or implemented here.
- **No lane modules.** Work Registry ownership, reconciliation, lifecycle,
  launcher, relay, portfolio, progress, and Git observers belong to later
  lanes. F0 neither implements nor imports them.
- **No runtime projections.** Live host processes, claims, jobs, deployments,
  quotas, and run state are operational evidence owned elsewhere. F0 persists
  only the generic mechanics of state I/O.
- **No private or operational values.** No private paths, usernames, tenant
  names, real-run content, or internal operational values were copied. All
  examples and tests are synthetic.

## Product positioning

CAO is positioned as an open-source AgentOps control plane for agentic
runtimes: runtime neutral at the core, with runtimes integrating through
plugins. This positioning and the F0 scope come from the product baseline for
CAO, not from the source protocol.

## Inspiration vs. code

Agentbox informed the user-experience direction only. No Agentbox code was
copied, translated, or vendored.
