# Evidence-to-Investigation

CAO's Evidence-to-Investigation capability turns **source-attested structured
observations** into bounded, explainable investigation candidates. It keeps the
original observation references and resource-to-repo mapping, evaluates
post-change goals and guardrails, and proposes new detection checks after
repeated source-attested improvements.

It **does not** automatically collect production logs, schedule runs, query
cloud providers, create agents, obtain Work Registry claims, modify rules,
merge, publish, deploy, or accept client-operating proof. Source collection
belongs to the accountable observer and its permissions. This component is
read-only and deterministic, and the existing CAO daemon/Team Execution and
Registry remain responsible for execution and ownership. No new ledger or
runtime service is introduced.

## Public command surface

All commands emit JSON. Invalid input yields status UNAVAILABLE and a nonzero
exit; the CLI never silently substitutes an empty source.

    cao improve assess --file examples/improvement_case.json
    cao improve context --file examples/improvement_case.json --resource api --depth 1
    cao improve verify --file examples/improvement_case.json --after examples/improvement_after.json
    cao improve propose --file examples/improvement_case.json --cause /path/to/cause.json
    cao improve learn --history /path/to/accepted-run-history.json
    cao improve check-proof --file examples/improvement_case.json --ref urn:factory:observation:sample --artifact /path/to/evidence-file

Source installs can use ./cao improve directly. Commands do not require a
configured model or live managed runtime. This is an executable analysis
interface, not a trigger for an implementation worker.

Assess separates four states:

- CORROBORATED: supported adverse observations about explicitly connected
  resources from two or more declared independent source categories.
- CANDIDATE: an adverse observation with only one represented source category.
- UNLINKED_EVIDENCE: a claimed common issue spanning resources
  whose relationship cannot be established in the supplied context.
- SOURCE_UNCERTAIN / NO_ADVERSE_SIGNAL: incomplete evidence or no
  supported adverse observation. Missing data is never reported as healthy.

The report keeps positive, normal and unknown observations separately with
their evidence references and fingerprints. A CORROBORATED signal is **not**
a proven root cause. A numeric signal threshold is checked deterministically;
an adverse or normal flag that contradicts the threshold is refused. Alerts,
tickets and other non-numeric observations may supply their qualified finding,
but CAO cannot independently verify their contents merely by parsing JSON.

## Input contract and source fidelity

A case of schema cao.improvement.case.v1 has one case_id, subject, UTC
detected_at, declared context, bounded signals, owner-defined objectives
and a mandatory data_class: synthetic or source_attested. Synthetic input
always yields SYNTHETIC_ONLY rather than a positive operating outcome;
source_attested is still an assertion requiring independent artifact checks.
A numeric threshold signal includes its exact metric identity, and an
objective binds its metric to an observed issue and resource. Each baseline
must identify the same SHA as that resource's declared source version.
The graph contains resource nodes and typed edges. Optional node owner_repo,
source_path and release_sha map the affected object back to its source owner
and version. Missing links remain missing; CAO does not infer ownership.

Every signal has a stable id, resource id, issue id, a finding, severity,
and proof with these fields:

    {
      "source": "factory",
      "ref": "urn:factory:observation:sample",
      "fingerprint": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
      "observed_at": "2026-01-01T09:00:00Z",
      "verdict": "SUPPORTED"
    }

The ref must identify a resolvable artifact for the accountable operator;
its fingerprint should be computed from that immutable artifact. CAO
requires URN/git proof namespaces to agree with declared source type and
refuses unsafe HTTPS references with userinfo, query or fragment.

The optional check-proof command hashes the selected local artifact,
reading at most 16 MiB from a regular file without following its final
path-component symlink. MATCH means the bytes match a submitted SHA-256
**and** the submitted source verdict is SUPPORTED; an UNKNOWN source stays
UNQUALIFIED even when the digest matches. Neither MATCH nor SUPPORTED
authenticates the original producer. The command reports source and verdict
without printing raw artifact data; the owning provider remains accountable
for provenance and access.

Input files are bounded to 512 KiB; duplicate JSON keys, non-finite values,
missing fields, invented graph endpoints, unsafe source paths, invalid hashes,
contradictory numeric classifications and oversized arrays are refused.
Do not include secrets, unredacted logs, personal identifiers, private prompts,
raw transcripts, credential-bearing URLs or customer data in references or
hypotheses. Use access-governed evidence references.

## P0: Full-run measurement and evidence-preserving handoff

Verify takes a separate cao.improvement.after.v1 file describing the
proposed change, exact Git commit, review, CI, deployed revision and
source-attested measurements. CAO:

1. Requires review, CI and deployed revision to reference the **same SHA**.
2. Refuses reported positive stage outcomes without supported source evidence.
3. Requires the after document to name the exact immutable case
   fingerprint; baselines must predate the proposal and bind to the original
   source release. After measurements must follow deployment and bind to
   the changed revision with distinct fingerprints.
4. Computes detection-to-proposal, detection-to-review and
   detection-to-deployment elapsed time.
5. Requires a goal tied to each adverse issue, a previously unmet baseline,
   a change in the affected owner's repository, and evidence that the original
   numeric adverse condition no longer holds. Any goal regression, guardrail
   regression or guardrail violation blocks a positive outcome.
6. Reports SOURCE_REPORTED_IMPROVEMENT, TARGET_NOT_MET, REGRESSION,
   NO_MEASURED_IMPROVEMENT, SYNTHETIC_ONLY or UNQUALIFIED with original refs
   and reasons. Positive source reports are never promoted to authenticated
   production fact.

A source-attested result is **not independently verified production health**.
The accepted_as_canonical_operating_proof field is always false. The
accountable operator must verify actual external evidence, reviewed diff,
security, authorization, tenant scope and deployment before file-back.

## P1: Context and bounded improvement proposals

Context is an *on-demand, read-only projection* of explicitly recorded
resource and repository relationships. It stores no second mutable resource
graph or topology authority. Actual deployed topology and Git remain
accountable. Traversal is exactly one graph edge per requested hop, follows
only the center resource's declared scope, and never expands from an unscoped
center. The source-declared scope label is not verified tenant authority.

Propose requires a separate cao.improvement.cause.v1 record containing
selected issue, cause **hypothesis**, owner repository, exact path scope and
supported signal IDs. It returns only PROPOSAL_ONLY. Suggested write paths
must already be explicitly declared by affected resources for that owner.

    {
      "schema": "cao.improvement.cause.v1",
      "case_id": "synthetic-incident-one",
      "issue": "deployment-failure",
      "hypothesis": "Inspect bounded retry behavior before changing it.",
      "repo": "example/platform",
      "write_scope": ["src/api.py"],
      "evidence_ids": ["factory-observation", "ci-observation"]
    }

The accountable CAO supervisor may convert an accepted proposal into a
new Team Execution manifest **only after** independently checking evidence,
root cause, proposed scope and repo ownership. Suggested paths are
source-declared and unverified exact files, never directory-prefix grants.
A conservative denylist refuses hidden, secret and governance-sensitive
paths from generic proposals; the operator must evaluate any exceptions
separately. The existing Registry preflight/WRITE claim remains mandatory.
Publication and deployment have separate authorization gates.
Models may suggest hypotheses, never grant scope.

This feature adds neither live cloud collectors nor vector search. To use
real operating signals, a source owner supplies a sanitized JSON adapter
from already accepted read-only telemetry and deployment evidence. An absent
adapter is UNKNOWN, never a fabricated healthy observation. New provider
connectors require separate privacy, access and cost qualification.

## P2: Learning from repeated evidence

Learn consumes bounded cao.improvement.history.v1 entries. Each contains
the exact case and outcome plus a source-attested operator acceptance receipt
bound to both immutable fingerprints. It suggests a new detection check
**only** when multiple separate cases for the **same issue, subject,
owner repository and declared scope** have distinct detection times and
release SHAs, disjoint evidence artifacts, cross-source corroboration,
and an issue-specific measured improvement.

Acceptance must occur after the supporting review, CI, deployment and
follow-up measurements. Its proof cannot reuse any case/after evidence from
the complete history or another acceptance. The output is PROPOSALS_ONLY:
no rule, prompt, runtime, policy or deployment is modified. Source-attested
operator acceptance is not cryptographic owner identity. The owner must
evaluate false positives, source coverage, collection cost and holdout
behavior before accepting a proposed check; recurrence alone proves no cause.

## First evaluation: interrupted deployment recovery

Use synthetic disposable-tenant fixtures and immutable proof references.
Measure error/acceptance rate and release recovery time before and after the
smallest correction, with isolation, access scope and rollback as guardrails.
Corroborate an issue using Factory plus independent CI or runtime signals.
Refuse improvement if the review/CI/deploy revisions differ, recovery is
incomplete, a guardrail regresses, or source evidence is unavailable.

Compare the full detection-to-verified-outcome duration, diagnostic accuracy,
handoff loss, model cost and false-positive rate to the manual baseline.
The case closes on independently accepted operating evidence; a positive CAO
analysis alone is not deployment acceptance.

## Deliberate limits

- No raw log scraping, uncontrolled sampling, cross-tenant joins or replayed
  model-generated evidence.
- No agent self-modification or changing authority from a model's output.
- No automatic WRITE team, Registry claim, PR or deployment.
- Python standard library only; works without site packages.
- Sample case/after evidence is synthetic. Its illustrative fingerprints
  are not production proof.
