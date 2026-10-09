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

The delivery key is derived from the brief's *content* — every line, every
quoted note, every signal, and the full per-source coverage including item
counts and failure reasons. So a repeated or interrupted run reconciles to the
same artifact instead of writing a second one, and a genuinely changed draft
gets its own file rather than silently reusing the previous one.

`generated_at` is deliberately **not** part of the key. A second run that
observes exactly the same things is the same logical delivery, so it
reconciles; the timestamp on disk is the one from the run that actually wrote
it.

## Bounded by design, not incidentally

Every collection has a stated ceiling, and exceeding one raises rather than
truncating:

| Bound | Value | What happens past it |
|---|---|---|
| `MAX_SCAN_DEPTH` (manifest) | 64 | `SpecInvalid`, never a `RecursionError` |
| `max_delivered_per_source` | 512 | `SourcesCapacityError`, nothing mutated |
| `max_receipts` (delivery) | 512 | `DeliveryCapacityError`, no eviction |
| `max_lines` (brief) | 5000 | `BriefCapacityError`, items are not dropped |

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

`run` **always reports `UNAVAILABLE` and exits 3** in this build. Composing a
brief needs a `SourceReader` for each declared source, and CAO ships none: no
runtime and no source adapter is a built-in default. The command names that
missing capability instead of pretending to have run something.

## Synthetic example

`examples/automation_manifest.json` and `examples/automation_preferences.json`
are obviously synthetic (`example.invalid`, `synthetic-*`). They exist so the
loader and the CLI can be exercised without touching anyone's real source.