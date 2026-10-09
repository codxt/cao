# Automations

CAO can describe an automation, read its sources without losing or inventing
anything, and write one draft to a local file. It deliberately does **not**
schedule anything, and it deliberately sends nothing anywhere.

Four modules carry the whole feature. They are standard library only and import
nothing outside `cao.evidence` and `cao.state`.

| Module | Owns |
|---|---|
| `src/cao/sources.py` | per-source reads, qualified coverage, replay-safe bookmarks |
| `src/cao/delivery.py` | outward delivery receipts and idempotency |
| `src/cao/spec.py` | the versioned declarative manifest loader |
| `src/cao/brief.py` | the draft-only brief pilot |

## What "no false health" means here

Every one of these is enforced by a test, not by convention.

- A source that is missing, unreadable or partial reports its own qualified
  outcome. It is never an empty result, never a healthy result, and it never
  advances its own bookmark. One failing source leaves every other source's
  data intact.
- A brief whose sources were not all read completely says so **in its own
  body**, and a source that was read and is genuinely empty reads differently
  from a source that could not be read.
- Contributing sources are re-read immediately before the brief is rendered. A
  failed recheck drops the stale items rather than presenting them as current.
- A delivery is `CONFIRMED` only from a positive acknowledgement that carries a
  receipt id. A transport that returns plausible data without one is
  `UNCERTAIN`, and an ambiguous delivery is reconciled against the destination
  by key before it can be sent again.
- Owner preferences are read-only to an agent **by construction**: there is no
  write, merge, propose-in-place or default path in `cao.spec`, so absent or
  corrupt preferences are refused rather than replaced.

## The schedule is descriptive only

`schedule` in a manifest records what the owner's external scheduler is
expected to do. CAO never fires it. There is no timer, cron entry, queue,
daemon or background worker in this feature. If you want a brief on a cadence,
something else has to run it.

## Owner preferences are not the agent's to change

The owner edits preferences in the governed system. An agent reads them. An
agent can *propose* a change to a human; it cannot rewrite them, and this
codebase contains no code path that would let it try.

Source text and run-state notes are wrapped as untrusted data. They are quoted
in a brief as content. They are never parsed as directives, and no authority
decision reads them.

## Where output goes

A local file, and nothing else. `cao.delivery.LocalFileSink` writes one
Markdown file per distinct brief under a directory the caller supplies. There
is no HTTP, socket, SMTP, webhook, credential or environment path anywhere in
this feature.

The delivery key is the **rendered artifact itself, minus its timestamp**.
That is deliberate: an earlier version kept a hand-maintained list of the
fields that reach the output, and a field missing from that list meant a
changed draft silently reused the previous file while the run reported
success. Deriving the key from the output makes that whole class of bug
impossible. `generated_at` is excluded because a repeat run that observes
exactly the same things is the same logical delivery and must reconcile to
one file rather than writing a new one every tick.

## Bounded by design, not incidentally

Every collection has a stated ceiling, and exceeding one raises rather than
truncating:

| Bound | Value | What happens past it |
|---|---|---|
| `MAX_SCAN_DEPTH` (manifest) | 64 | `SpecInvalid`, never a `RecursionError` |
| `max_delivered_per_source` | 512 | `SourcesCapacityError`, nothing mutated |
| `max_receipts` (delivery) | 512 | `DeliveryCapacityError`, no eviction |
| `max_lines` (brief) | 5000 | `BriefCapacityError`, items are not dropped |
| `MAX_MISSES` (brief) | 2 consecutive misses | the item is retired and stated |

The delivery ledger never evicts, because a receipt may recur and forgetting it
would be a silent loss. That is a deliberate ceiling with a real consequence:
after 512 *distinct* brief contents the ledger refuses new keys and a run fails
loudly. An operator running a recurring brief should plan to archive or rotate
the delivery state before that, not discover it as a production failure.

## Persisted state and what it is not

Three state files, each read through its own loader and each failing closed on
corrupt input:

| File | Schema | Holds |
|---|---|---|
| bookmarks | `cao.automation.sources.v0` | per-source cursor and delivered item ids |
| delivery ledger | `cao.automation.delivery.v0` | one receipt per delivery key |
| brief history | `cao.automation.brief.history.v0` | fingerprints of items already reported |

The brief history is **not** a second delivery ledger. It holds item
fingerprints used only to suppress unchanged items; nothing is delivered from
it, and bookmarks remain the single record of what has been consumed.

Bookmarks advance only after a delivery is `CONFIRMED`. That is what makes an
interrupted run safe: an interrupted run never confirmed, so it never advanced,
so the same items are selected again — no missed items, no duplicates.

### Items the fresh re-read does not observe

A brief re-reads every contributing source immediately before rendering, and
an item the re-read does not observe is never reported as current. What happens
next is the interesting part:

- The item **owes a retry**. It is not consumed, and its timestamp caps the
  cursor so it stays selectable on the next run — including the runs where the
  source does not mention it at all. A flaky page or a pagination hiccup
  therefore *delays* an item; it never deletes one.
- After `MAX_MISSES` (2) consecutive runs without it, the item is **retired**:
  consumed, and named in the brief's own body under "Selected for this brief
  but no longer observed". A source that stops reporting an item has, from
  CAO's point of view, resolved it — and the reader is told that is what
  happened rather than left to infer it.

Delivered ids are recorded *even while the cursor is held back*. That is the
whole reason the cap is a separate value rather than a choice between "record
it" and "move the cursor": without the separation, everything above a pending
item would be delivered again on the next run.

## CLI

```sh
cao automation validate --config examples/automation_manifest.json \
                        --preferences examples/automation_preferences.json
cao automation status --bookmarks PATH --delivery PATH --history PATH
cao automation run --config PATH --dry-run
```

`validate` loads the manifest and, optionally, the owner preferences. `status`
reports each state file through its own loader; an absent state file is
`UNAVAILABLE` and exits nonzero, because "no run has happened" and "here is
what the last run observed" are different statements.

`run` reads and validates the manifest first, because a caller who passed a
broken manifest deserves to be told *that* rather than told about a missing
adapter. When the manifest is valid, it reports `UNAVAILABLE` and exits 3:
composing a brief needs a `SourceReader` for each declared source, and CAO
ships none. The command names that missing capability instead of pretending to
have run something.

## Synthetic example

`examples/automation_manifest.json` and `examples/automation_preferences.json`
are obviously synthetic (`example.invalid`, `synthetic-*`). They exist so the
loader and the CLI can be exercised without touching anyone's real source.