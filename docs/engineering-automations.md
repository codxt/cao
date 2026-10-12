# Engineering evaluations and guarded automation

CAO can poll a governed engineering improvement on its existing daemon cadence,
compare one source-reported evaluation with an exact GitHub PR, ask for
operator approval when warranted, and request GitHub-native auto-merge once
all independent guards pass. No new scheduler, queue, authority ledger,
model route, secret store or release executor is introduced.

This extends the earlier Evidence-to-Investigation work. The independent
evaluation calculator is owned by the runtime repository. CAO consumes only its
sanitized report, and does not implement evaluator search or copy private tenant
knowledge into its repository.

## Workflow

1. The owning system evaluates a fixed suite and exports a
   cao.engineering.evaluation.v1 comparison report. Its source SHA and suite
   digest bind the reported improvement. Missing provider cost, incomplete
   coverage, regression or an unmeasured gain refuses that verdict at the
   evaluator. A source-reported improvement is not accepted operating proof.
2. An opt-in CAO engineering watch polls the exact report and exact candidate
   GitHub PR. A failed evaluation produces NEEDS_IMPROVEMENT and may be passed
   to a separate source-attested CAO feedback watch for automatic READ-only
   diagnosis. A WRITE/review team still requires an operator-invoked
   cao improve authorize request under the shared Work Registry.
3. For a successful candidate, CAO verifies a same-repository non-draft PR,
   exact head SHA, changed file scope, all declared evaluation and CI checks
   completed successfully on that SHA from GitHub Actions, GitHub branch
   protection with strict up-to-date status checks, and a current independent
   APPROVED review on that exact head.
4. An operator with a GitHub identity distinct from the automation bot and
   reviewer explicitly authorizes the single exact PR/head/report by posting
   the exact comment described below. The bot must not hold the operator
   identity. A changing commit or comparison invalidates the grant.
5. If auto_merge=true, CAO requests GitHub-native auto-merge using
   --auto --squash --match-head-commit, once. GitHub remains authoritative
   for merge. Before sending the request, a private CHECKPOINT is persisted as
   SUBMITTING, so interrupted calls cannot be replayed blindly. A private
   kernel file lock serializes effectful daemon and manual invocations for
   that watch; an operator pause or expired grant cannot erase an in-flight
   checkpoint. The next tick checks GitHub for actual merged/armed state,
   and never issues an unchanged second merge request.
6. Merge is only a Git source state. A deployment platform, release, tenant
   health and post-deployment evaluation remain separate. A new regression
   starts a new governed incident rather than silently changing original
   evidence.

The engineering watch is a real opt-in recurring automation because CAO's
existing daemon calls the one-shot checker at its configured cadence. It is
not a general-purpose scheduler or a replacement for the existing descriptive
cao automation spec.v0 (whose run remains unavailable without concrete source
adapters). No engineering watch is enabled by installing CAO.

## Private manifest

Write a private JSON file with exact values. All paths must be absolute and
normalized, and the state directory must already exist under operator control.

~~~json
{
  "schema": "cao.engineering.watch.v1",
  "watch_id": "synthetic-optimization",
  "repo": "example/platform",
  "pr": 123,
  "base_branch": "main",
  "suite_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "evaluation_file": "/private/example/comparison.json",
  "min_gain": 0.05,
  "required_checks": ["validate (3.12)", "eval-suite"],
  "operators": ["separate-owner-account"],
  "reviewers": ["separate-reviewer-account"],
  "allowed_paths": ["src/search.py", "tests"],
  "expires_at": "2027-01-01T00:00:00Z",
  "min_interval_seconds": 300,
  "state_file": "/private/example/state.json",
  "auto_merge": false
}
~~~

Start with auto_merge=false and a dry run, then qualify the GitHub bot,
branch rules, evaluation provenance, and operator account separation. The
manifest is scoped to one PR and one authorized class of changes. The allowed
paths reject protected workflows, CI policy, tenant state, dependency manifests,
infrastructure, and deployment files. The state file binds a fingerprint of
the manifest. Changing the scope cannot silently reuse a prior grant/state:
create a new governed watch or explicitly reconcile the old one.

Read-only planning:

~~~sh
cao improve engineering --file /private/example/watch.json
~~~

The returned evaluation_sha256 is the SHA256 of canonical JSON for the exact
comparison. A legitimate operator grants this exact immutable evaluation and
PR HEAD by adding a GitHub PR comment with no extra text:

~~~text
CAO-MERGE-AUTHORIZE <EXACT_40HEX_PR_HEAD> <EXACT_64HEX_EVALUATION_SHA256>
~~~

The operator must be in the watch's allowlist, must differ from the GitHub
identity executing CAO, and must differ from the independent reviewer. An
APPROVED review is required separately; comments or model-produced proposals
are not code-review evidence. To revoke the grant before merging, post
CAO-MERGE-REVOKE followed by the same exact head and evaluation digest;
the newest qualifying operator grant/revocation controls **new admission**.
After GitHub auto-merge has been armed, the operator must disable it in
GitHub itself; the local revocation comment alone cannot cancel an
already-authorized GitHub-side request.

After the watch has returned READY_TO_ARM, an operator with sufficient
pre-existing permissions may test an effectful pass:

~~~sh
cao improve engineering --file /private/example/watch.json --execute
~~~

To poll automatically, set in the existing private CAO config's daemon section:

~~~json
{
  "daemon": {
    "engineering_watch_file": "/private/example/watch.json"
  }
}
~~~

Merge this section into the existing private config. Never replace the whole
configuration with the fragment above. Preserve the shared Registry setting,
provider credentials, and existing feedback watch. No source code install
enables the watcher automatically.

### Ordered PR merge queue

For several PRs, place 1-8 separate watch manifests in one private directory,
named in intended priority order (for example 001-feature.json,
002-fix.json). Each PR keeps its own operator grant, comparison fingerprint,
required checks, distinct state checkpoint and exact-head GitHub authority.
All manifests are validated before the first external request; an invalid
later file refuses the whole scan. Only the first not-yet-merged PR is
considered for promotion. Later PRs cannot overtake an approval, CI, or
auto-merge request still in flight.

~~~sh
cao improve engineering --directory /private/queue
cao improve engineering --directory /private/queue --execute
~~~

Use one daemon configuration key in place of engineering_watch_file:

~~~json
{"daemon": {"engineering_queue_dir": "/private/queue"}}
~~~

Both engineering_watch_file and engineering_queue_dir together are refused.
Each tick processes a maximum of one PR side effect. When the previous PR
has merged, the next PR must independently match its original exact head and
all gates; stale branches hold for requalification. The queue never adds
workers, publishes source without a grant, or performs deployments.

GitHub automation must run under a narrow bot account. On a host where the
GitHub CLI is authenticated as the same account that would authorize the
comment, this automation refuses to arm. A valid user message, source text,
local manifest, model answer, Work Registry claim, or successful CI never
substitutes for the external GitHub operator grant.

## Behavior and unknowns

- NEEDS_IMPROVEMENT: no green measured result; investigate with the existing
  evidence-to-investigation workflow, do not promote a candidate.
- CI_NOT_GREEN_FOR_HEAD, INDEPENDENT_REVIEW_REQUIRED, SCOPE_MISMATCH,
  BRANCH_RULES_NOT_VERIFIED: source gates pending/refused.
- AWAITING_OPERATOR_APPROVAL: all engineering gates passed, but no separate
  operator authorization binds this exact head/evaluation digest.
- READY_TO_ARM: all checks and explicit grant verified in read-only mode.
- AUTO_MERGE_REQUESTED: only an upstream request was submitted.
- AUTO_MERGE_ALREADY_ARMED / MERGED_SOURCE_ONLY: reconciled against GitHub.
- AWAITING_GITHUB_RECONCILIATION: previous request state is uncertain;
  no automatic replay. Inspect the authoritative GitHub PR.
- COOLDOWN, EXPIRED: cadence/expiry guards.

A negative result never promotes a change. Source or provider errors keep
UNKNOWN and report a bounded code rather than leaking credentials, URLs,
evaluation results or CI logs. This mechanism does not auto-create PRs, merge
unreviewed work, edit tenant knowledge, activate production monitors,
deploy, change credentials, grant permissions, or override a deployment
platform's release gates.

## Validation and qualification

~~~sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
  python3 -B -S -m unittest tests.test_engineering_loop -v
~~~

Hermetic tests check exact-head identity, CI and branch protection, separate
operator/reviewer, approval content, scope, TTL, polling cadence, crash
idempotency, remote merge reconciliation and the daemon integration. They
never call live GitHub merge or a production evaluator.

Prior to live use, qualify a real evaluation metric adapter/source, exact metrics
and CI, safe bot permissions and protected branch, operator and reviewer
identities, a disposable PR approval/CI canary, and recovery after a missed
merge response. Enable production automation only under that evidence and
separately authorized scope.
