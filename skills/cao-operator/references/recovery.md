# CAO receipt verification and bounded recovery

Recovery never invents evidence. Every step below re-reads durable evidence and
holds rather than guesses.

## What completion requires

A prompt being accepted is not completion. A bare `IDLE` is not completion: the
managed runtime distinguishes an IDLE pane (ready for input) from a DONE pane
(a settled completion observed in an unseen tab). Only a **post-prompt settled**
capture that carries this execution's exact, task-bound 16-character nonce on a
standalone `CAO_TEAM_RESULT:` line verifies completion. For a review, the exact
single verdict and the exact reviewed HEAD must also verify.

The receipt proves a post-prompt response was observed. It is **not** a security
signature, approval, guarantee of correctness or proof that tests passed.
Repository, Registry, scope, commit, review, human-approval and release gates
remain independently required.

A DONE without a receipt is terminal but unverified and holds `UNKNOWN` rather
than unblocking dependents. An idle observed immediately after prompt
acknowledgement, before any work, is a transient early idle and stays RUNNING
with its claim held.

## Bounded recovery paths

- **`cao team resume --id <team> --task <task>`** re-checks an already-inspected
  `UNKNOWN` task. It resets only the stored `UNKNOWN` observation to RUNNING and
  preserves the **same** execution ID and prompt; it does not force a fresh model
  call. Inspect the exact pane, prompt state and Registry claim first.
- **`cao team retry-read --id <team> --task <task>`** admits **one** new READ
  execution identity after an invalid terminal receipt. It is admitted only when
  the original attempt's own durable evidence proves it is a READ execution, it
  is `stopped`, its CAO-owned pane was closed and recorded, its claim was
  released as `abandon`, and the shared Registry holds exactly that released READ
  claim. The old record, receipt and nonce stay durable and untouched, and the
  new attempt gets its own copy-only nonce. A WRITE attempt is never retryable,
  and a second retry is refused.
- **Bounded automatic READ recovery.** The team tick may apply exactly one
  automatic transition: one bounded resume of the **same** original `UNKNOWN`
  READ execution, and only when every gate is proven from durable evidence
  (recoverable READ transport failure, intact identity/prompt/pane, an available
  runtime still reporting the original worker, the exact open READ claim, and an
  unchanged pinned HEAD and branch). At most one resume is admitted per original
  execution identity.

## What always holds

Everything else holds or blocks with a reported reason: a lost terminal receipt,
a Registry conflict or unreadable ledger, a Git HEAD or branch drift, a platform
or tool-layer denial, an interactive provider/route gate, and a pending
exact-head PR CI gate. An `UNKNOWN` WRITE — including a `SOURCE_COMPLETE`-but
unverified WRITE at an already committed head — is never automatically replayed,
retried, stopped, released, merged or published, and its claimed worktree is
never mutated.

A stale Registry claim is never released by age, by a lease or by guessing its
owner. An unknown or merely old claim keeps its ownership until an operator
reconciles it. CAO never force-unlocks the Registry and never closes a pane it
cannot prove is its own.

## Operator checklist when a task is UNKNOWN

1. Inspect the exact task record, its bounded terminal observations, its prompt
   state and its Registry claim. Do not accept a near-miss marker.
2. If a worker is still running and a stop is needed, stop only the identified
   CAO-owned task with `cao stop --id <execution-id>` and confirm pane closure
   and claim release.
3. Choose the correct path: `cao team resume` for a re-check of the same
   identity, or `cao team retry-read` for one fresh READ identity after a
   verified stop. A WRITE stays held for explicit investigation.
4. Escalate rather than improvis. No recovery path grants publication,
   deployment or release authority.
