# Model coverage tracker

The model coverage tracker answers one question: **what is the planned
recipe/platform coverage for each model, and which targets actually have
merged recipes or independent benchmark evidence?**

GitHub Issues own the plan. The repository catalog
(`tools/catalog.py::build_catalog`) owns implementation status, recipe
maturity, and committed benchmark evidence. There is no second hand-maintained
status table: the evaluator joins the issue definition to the catalog on every
run, and the workflow updates the dashboard from that result.

Passing CI, a green check, a catalog maturity label, or a validation badge for
a different platform never completes a target by itself.

## Pieces

| Piece | Purpose |
|-------|---------|
| `.github/ISSUE_TEMPLATE/model-coverage.yml` | Issue form with the machine-readable fenced YAML target definition and prose intake fields. |
| `tools/model_coverage.py` | Deterministic evaluator. Reads issues JSON plus the catalog and emits JSON or Markdown. |
| `tools/model_coverage_sync.py` | Read-only/dry-run planner and opt-in GitHub Projects v2 writer. |
| `tools/model_coverage_intake.py` | Issue-event helper for first-intake default assignment and idempotent validation feedback without project access. |
| `.github/workflows/model-coverage.yml` | Read-only checks on PR/main/issue events; issue intake feedback; trusted default-branch-only sync. |
| `tests/test_model_coverage.py` | Offline tests for parsing, completion rules, idempotence, pagination, write guards, and intake feedback. |

## Status and completion rules

Each target platform is classified from the current repository state:

| Tracker status | Meaning |
|----------------|---------|
| `planned` | The recipe or exact target platform is not in the repository yet. |
| `in-progress` | A matching recipe/platform exists at `day-zero` maturity. |
| `contributed` | A matching recipe/platform exists at `contributed`, `validated`, or `production` maturity. |
| `validated` | The recipe/platform has committed benchmark evidence whose tested platform exactly matches the target and whose catalog validation state is `same-engine` (the current engine). An `earlier-engine` run on the exact platform is shown as compatible evidence but does **not** count as independent validation of the current engine. |
| `blocked` | The platform entry is explicitly blocked; it never completes. |

A tracked issue whose definition cannot be evaluated is projected as `Error`
with `Coverage` explicitly set to unknown. If validation or catalog evaluation
fails, the narrow stale-reconciliation path marks existing tracker-owned items
`Stale`. Neither state preserves an old `Validated` value.

Model delivery status is:

- `planned` — every target platform is still planned.
- `in-progress` — at least one matching recipe exists, but not every target platform is at least `contributed`.
- `contributed` — every target platform is at least `contributed`, but not every platform is independently `validated`.
- `validated` — every target platform is independently `validated`.
- `blocked` — at least one target platform is blocked.

Model delivery status is the lifecycle. The separate `completed` boolean is
true only when **every** listed target platform meets its own completion
criterion, so a `contributed`-status model is complete for a
`completion: contributed` issue but incomplete for a `completion: validated`
issue. The Markdown report and `Coverage` project field prefix the result with
`complete` or `partial`.

Each platform uses the model-level `completion` unless that platform entry sets
its own `completion`. This allows a mixed plan, for example a `contributed`
default with one `validated` production target. `coverage.completed_platforms`
counts each platform against its own requirement:

- `contributed` completes a platform at `contributed` or `validated` status.
- `validated` completes a platform only at `validated` status.

A model is never marked complete when only part of its planned recipe/platform
scope is ready. Repository maturity is still recorded separately on every
target row. A catalog validation badge for another stack/version is reported as
a different platform and does not satisfy `validated`. An `earlier-engine` run
is compatible evidence but not independent validation of the current engine.
Validated rows link the committed `result.json` and, when the evidence records
a Forge `pr_number`, the supporting pull request; recipe IDs link their catalog
source when the build is clean.

`validated` requires all of:

1. catalog maturity `validated` or `production`;
2. committed evidence referenced by the recipe (`run.yaml` and normalized
   `result.json` through the catalog's existing evidence validation);
3. the selected validation badge's `tested_platform` exactly equal to the
   target `stack`/`version`;
4. catalog validation state `same-engine`, with a non-empty evidence run ID.

The evaluator filters evidence to the exact target platform and calls the
catalog's existing `validation_badge` helper again, so an earlier cross-platform
badge cannot hide a later exact run. Within exact-platform evidence it prefers
a same-engine run over an earlier-engine run. If same-engine candidates exist
but are unusable, the evaluator falls back to the full exact-platform set only
to explain earlier-engine evidence; the target remains `contributed`.

`earlier-engine` is retained in the report for transparency and may be useful
for operational decisions, but it is never accepted as independent validation
of the current engine. It is not inferred from a green CI run or a
compatibility claim.

## Issue definition and intake

Create one issue per model using the **Model coverage target** form. The form
prefills the marker comment and one fenced block in the editable body. Keep both,
keep exactly one fenced `yaml` block, and edit the block in place.

**Do not include customer data.** Use public model and platform details and
sanitized evidence only. Do not include customer names, customer data or prompts,
customer environment identifiers, private hostnames or IPs, private logs,
credentials, or confidential blockers. This issue and every linked reproduction
must contain no customer data.

The form's prose fields collect public model/checkpoint links, hardware type and
count, new-versus-refresh work scope, required serving features, and stretch
goals. Those fields are not parsed. Only the single fenced YAML block affects
automated completion, so do not add a second fenced YAML block.

```yaml
schema_version: 1
model_id: glm-5.3
# owner: "@octocat"  # optional; omit or leave empty for the default triage owner
completion: validated
# jira: RHOAIENG-123
blockers: []
targets:
  - recipe_id: glm-5.3-dep8-mtp3-hisparse
    platforms:
      - stack: rhoai
        version: "3.6"
      - stack: vllm
        version: v0.31.0
  # Add another required hardware target as a second targets[] entry.
  - recipe_id: another-required-hardware-recipe
    platforms:
      - stack: rhoai
        version: "3.6"
```

### Required versus stretch scope

Every entry under `targets` is **required** and counts toward model completion.
Add one `targets[]` entry per required hardware target, using the recipe's
distinct `recipe_id`; a second required GPU count or node topology is another
entry, not an optional key.

Stretch goals are optional work that should stay visible without blocking
required completion. Describe them in the form's **Stretch goals** prose field
(and, if useful, in additional prose below the YAML block). Do **not** add an
`optional` target key, a second fenced YAML block, or optional entries under
`targets`; the evaluator rejects unknown target keys and would count any entry
there as required. Stretch prose is never parsed and never affects coverage or
completion counters.

### Rules

- `model_id` matches `models/<model-id>/model.yaml` when the model exists. A
  model that is not yet in the repository is a valid planned entry; it is
  reported under `planned_models`.
- `owner` is optional. Omit it, use YAML `null`, or leave it empty to use
  `@thameem-abbas` as the default triage owner. An explicit valid GitHub user or
  team handle (`@octocat` or `@org/team`) is authoritative and is never replaced
  by assignment changes. Obvious textual placeholders such as `TBD` are rejected
  with an actionable error that points back to the default-owner policy.
  - For an ownerless definition, the evaluator uses the issue's current
    assignees as the tracker owner. One assignee renders as `@login`; multiple
    assignees render in GitHub's assignment order as `@first, @second`. This
    keeps the dashboard consistent when a maintainer delegates ownership
    without editing YAML.
  - If an ownerless issue has no assignees, the evaluator uses the default
    triage owner. The issue-event intake workflow attempts a one-time assignment
    to `@thameem-abbas` on the first `opened` event. Assignment is best-effort
    and requires all of: a readable open issue and catalog, a parseable ownerless
    YAML definition, no current assignees, and no `assigned`/`unassigned` event
    in GitHub's paginated issue history. Any prior assignment history, including
    a later manual unassignment, blocks replay, so rerunning an old `opened`
    event can never reassign the issue. Malformed YAML is not assignment-eligible
    and receives feedback only. A failed issue or catalog read skips assignment;
    feedback is still attempted where possible. The workflow never clears or
    replaces existing assignees, does not backfill existing issues, and does not
    reassign after an edit or manual unassignment. A later delegation is
    preserved.
  - A team handle is a valid explicit tracker owner but is never treated as a
    GitHub user login for automatic assignment.
  - The form intentionally does not set a default assignee. Doing so would also
    assign explicitly owned issues, which the YAML owner must control.
- `completion` is `contributed` or `validated` and applies to every platform
  unless a platform entry sets its own `completion`.
- `recipe_id` is the v4 `recipe_id` field inside `recipe.yaml`, not the recipe
  directory name. Recipe IDs are scoped by model: the same recipe ID may
  legitimately exist under another model. An ID that matches no catalog recipe
  for this model produces a warning and remains `planned`; that is expected for
  a planned recipe. The warning points out the lookup rule so a directory-name
  typo or a cross-model copy is visible without rejecting future work.
- `platforms` are exact `stack`/`version` values declared by that recipe. An
  absent pair stays `planned`, which is the normal state for a backlog entry.
  An optional `completion` on a platform overrides the model-level bar for that
  platform only. RHOAI 3.6 examples use the quoted string `version: "3.6"`;
  other quoted versions are accepted and evaluated, with absent pairs remaining
  planned. Unquoted numeric-looking versions such as `3.6` are parsed as numbers
  and fail with a quoting hint. Numeric-looking versions from any stack should be
  quoted.
- `blockers` is an optional public list (at most 20). Do not include
  confidential details.
- `jira` is an optional internal Jira **key only**. The evaluator never calls
  Jira and renders only:
  **Refer to internal Jira for more details on this task.**
  Do not paste Jira descriptions, links, comments, or discussions.
- Replace the form's placeholder values. Unknown keys, duplicate model
  declarations, duplicate target platforms, malformed YAML, non-string mapping
  keys, a non-integer `schema_version`, invalid Jira keys, unquoted
  numeric-looking versions, and unfilled placeholders are hard errors. They
  include the issue number and field location. Two issues declaring the same
  `model_id` invalidate **both**; the tracker does not publish the first one as
  complete. Split hardware variants across separate `targets` entries under one
  model issue, not across duplicate model issues.
- The `model-coverage` label is a one-time repository setup prerequisite. Verify
  it before relying on label-based recognition:
  `gh label list --repo openshift-psap/inference-playbooks --search model-coverage`.
  The repository's label has been created for the RHOAI 3.6 pilot; a fork or
  renamed repository must create it once with
  `gh label create model-coverage --color 5319E7 --description "Model coverage tracker entry"`.
  The form's label and the marker comment both identify a tracking issue; either
  one is sufficient. If the label is absent, the marker still identifies an
  existing tracking issue, but label-based queues will not include it. The
  marker lives inside the editable target definition because GitHub issue forms
  do not submit `markdown` block content.

Closing a tracking issue does not retire the plan or free the model ID. The
evaluator reads the issue body and ignores issue state, so a closed issue still
owns its `model_id` and is still evaluated. To untrack a model, remove **both**
the `model-coverage` label and the
`<!-- inference-playbooks:model-coverage-tracker -->` marker comment. Removing
only one of them leaves the issue tracked. Deleting or emptying the fenced YAML
without removing both identifiers produces an invalid tracked definition
(`Error`); it does not untrack the model. Transfer or delete the issue if it
should disappear entirely.

## Intake assignment and validation feedback

The issue-event intake job (`tools/model_coverage_intake.py`) runs on tracking
issues for `opened`, `edited`, `reopened`, `assigned`, and `unassigned` events
with a job-scoped `issues: write` `GITHUB_TOKEN`. It checks out the trusted
default branch and does not receive project credentials or the
`forge-evidence` App secret. It remains active when
`MODEL_TRACKER_WRITE_ENABLED` is unset or `false`.

- On the first `opened` event, an ownerless definition is assigned to
  `@thameem-abbas` only when the issue is open, the YAML parses and omits
  `owner`, the issue has no assignees, and the paginated issue history contains
  no `assigned`/`unassigned` event. Any assignment history means first-intake
  assignment already happened or a human delegated it; it is never replayed.
  Existing assignees are preserved; explicit YAML owners are never assigned;
  edits, later unassignment, and assignment/unassignment events do not trigger
  reassignment or retroactive backfill. Immediately before writing, the apply
  step re-reads the issue and assignment history and skips the assignment if a
  manual assignment, owner edit, closure, or prior history appeared after
  planning. It verifies that the default login appears in the read-back
  assignee list and records a failure if the API ignored it; a read-back request
  failure is recorded as an uncertain outcome, not as success or a skip.
- The job creates or updates one issue-specific validation comment with marker
  `<!-- inference-playbooks:model-coverage-feedback -->`. It is authored by the
  workflow identity (`github-actions[bot]`), is idempotent, and only edits a
  comment with that exact marker and author. Human comments and the separate
  dashboard-sync marker `<!-- inference-playbooks:model-coverage -->` are never
  edited. A foreign comment with the feedback marker is preserved and does not
  suppress creation of a separate owned feedback comment. Assignment and
  unassignment events update this feedback comment to show the new effective
  owner without assigning anyone.
- After assignment handling, the apply path refreshes the authoritative issue,
  re-evaluates it against the already loaded catalog and issue snapshot, and
  re-reads comments before building feedback. A concurrent owner or definition
  edit therefore drives the posted result, and a no-longer-tracked issue skips
  stale feedback. The workflow summary and result artifact report planned versus
  applied, skipped, failed, and uncertain actions separately.
- The feedback comment states whether the definition is valid, the effective
  owner and its source, coverage status, unknown-recipe warnings, and correction
  guidance. It also states that dashboard sync is disabled when
  `MODEL_TRACKER_WRITE_ENABLED` is not `true`.
- If the triggering definition is invalid, the intake job posts or updates the
  feedback comment and then exits with status 1, so a green issue-event run
  cannot be mistaken for valid intake. Operational API failures exit with status
  2 even when feedback or assignment succeeded, and the audit result is still
  written. The read-only check job still uses `--allow-errors`, publishes the
  same errors in its workflow summary and artifacts, and allows valid models to
  be processed. Warnings such as an unknown planned recipe ID do not fail the
  run.
- The read-only parser and evaluator remain pure; they do not assign, comment,
  or call the GitHub API. Only the explicit intake apply step performs those
  writes.

## Existing untracked models

Any model in the catalog without a tracking issue appears under
`untracked_models` in JSON and **Existing models without a tracking issue** in
Markdown. This is a report, not an automatic completion or an error. Until the
agreed backlog is filed as tracking issues, the evaluator reports all current
models as untracked.

Malformed definitions return exit code 1 locally. The workflow check runs the
evaluator with `--allow-errors`, writes the report and errors as artifacts, and
publishes them in the workflow summary. When dashboard sync is enabled, the sync
job marks each affected project item `Error` while continuing to update valid
models; when it is disabled, the intake feedback comment and the check summary
are the discoverable validation result. The issue-event intake job also fails
for the invalid triggering issue after posting its feedback. Untracked models do
not fail the run; they are kept visible so the backlog cannot silently disappear.

## Local evaluation

No token or network access is needed for the evaluator itself because it reads
the repository and an issues JSON file. Fetch issues with the gh CLI:

```sh
gh api --paginate --slurp \
  "repos/openshift-psap/inference-playbooks/issues?state=all&per_page=100" \
  > /tmp/model-coverage-issue-pages.json
python3 - <<'PY'
import json
from pathlib import Path

pages = json.loads(Path("/tmp/model-coverage-issue-pages.json").read_text())
issues = [issue for page in pages for issue in page if "pull_request" not in issue]
Path("/tmp/model-coverage-issues.json").write_text(json.dumps(issues))
PY

python3 tools/model_coverage.py \
  --issues /tmp/model-coverage-issues.json \
  --format json --out .build/model-coverage/report.json

python3 tools/model_coverage.py \
  --issues /tmp/model-coverage-issues.json \
  --format markdown --out .build/model-coverage/report.md
```

`--issues -` reads JSON from stdin. The JSON is deterministic: sorted keys,
stable model/action ordering, no timestamps, and catalog source SHA from the
normal catalog build identity.

The CLI exits:

- `0` — evaluation succeeded, including untracked/planned model reports;
- `0` with warnings — unknown recipe IDs and other non-blocking definition
  warnings are printed to stderr and included in the report, but do not fail the
  run;
- `0` with `--allow-errors` — issue definitions are invalid but the report was
  written (catalog/input failures still exit 2); this is the workflow mode;
- `1` — one or more tracking issue definitions are invalid;
- `2` — catalog, input, or project/API failure.

## Projects dashboard sync

The sync reads the evaluator output, finds the Project v2 item for each
tracking issue, and updates only owned fields. Dry run is the default; it reads
the project and prints the planned mutations without writing.

### Project setup

1. Create a GitHub Project v2 owned by the repository organization (recommended)
   or by a user. Link it to `openshift-psap/inference-playbooks`.
2. Add these fields with **exact** names and types. The sync does not create,
   delete, or rename fields; it fails with the missing field/option when setup
   is incomplete.

   | Field | Type | Options |
   |-------|------|---------|
   | `Model ID` | Text | — |
   | `Owner` | Text | — |
   | `Completion Criterion` | Single select | `Contributed`, `Validated` |
   | `Completed` | Single select | `Completed`, `Incomplete`, `Unknown` |
   | `Tracker Status` | Single select | `Planned`, `In progress`, `Contributed`, `Validated`, `Blocked`, `Error`, `Stale` |
   | `Blocked` | Single select | `Yes`, `No`, `Unknown` |
   | `Coverage` | Text | — |
   | `Jira` | Text | — |
   | `Blockers` | Text | — |
   | `Tracker Issue` | Text | — |
   | `Last Evaluated` | Text | — |

   `Completed` is filterable and distinct from the lifecycle `Tracker Status`.
   `Blocked` is `Yes` for manually declared blockers or a repository-blocked
   target; `Blockers` carries the manual blocker text. `Last Evaluated` stores
   the evaluated catalog SHA (no timestamp, to avoid churn).

   Keep the default `Status` field and any human fields (priority, notes,
   target dates, etc.) outside the tracker's owned set. The sync never writes
   them.
3. Store the project number from its URL
   (`https://github.com/orgs/ORG/projects/NUMBER`).
4. Create repository **variables**:
   - `MODEL_TRACKER_PROJECT_OWNER` — organization or user login;
   - `MODEL_TRACKER_PROJECT_NUMBER` — numeric project number;
   - `MODEL_TRACKER_BOT_LOGIN` — optional fallback login for tracker
     comments. The workflow normally derives `<app-slug>[bot]` from the minted
     App token, so set this only for a non-App credential or a local run whose
     token cannot read `viewer`.
   - `MODEL_TRACKER_WRITE_ENABLED` — leave unset or `false` until the dry run
     and dashboard are reviewed. Set to `true` only to enable the trusted sync.
5. Reuse the existing organization-owned GitHub App and protected
   `forge-evidence` environment from benchmark ingestion. No tracker-specific
   secret or personal access token is required.
   - Grant the App organization **Projects: write**, repository
     **Issues: write**, and repository **Contents: read** (Metadata read is
     implicit), then approve the updated permissions on the installation.
   - Store `FORGE_INGEST_APP_CLIENT_ID` as an environment variable and
     `FORGE_INGEST_APP_PRIVATE_KEY` as an environment secret on
     `forge-evidence`, and restrict that environment's deployment branch
     policy to `main`.
   - The `sync` and `stale` jobs mint a repository-scoped installation token
     only for their write step and revoke it at job completion. `owner` plus
     `repositories` limits repository access without dropping the
     organization Projects permission; the job's default `GITHUB_TOKEN` stays
     read-only.
   - PR jobs never receive App credentials, and the workflow never runs sync
     for a pull request event. Comments are authored by the App's
     `<app-slug>[bot]` identity; only marker comments owned by that exact login
     (or `MODEL_TRACKER_BOT_LOGIN`) are updated.
6. Verify the `model-coverage` label exists (see above) and open the first
   tracking issue. The label is repository setup, not created by the workflow.
   The issue-event intake job uses only the workflow `GITHUB_TOKEN` with
   repository Issues write; it does not need project or App credentials.

### Dry run

With a read-capable project credential:

```sh
GH_TOKEN=<project-capable-token> \
python3 tools/model_coverage_sync.py \
  --repository openshift-psap/inference-playbooks \
  --project-owner ORG \
  --project-number NUMBER
```

The output contains the evaluator report plus a plan with `add_item`,
`update_field`, `clear_field`, and `update_comment`/`create_comment` actions.
Plan and report files are written before any mutation; `--result-out` records
applied actions and, on failure, the failed action plus the actions that had
already completed. Review the plan before applying.

`--issues` may supply a snapshot for evaluation, but the sync does not trust it
as a write source. Every tracking entry is checked against the selected
repository before planning: any supplied `html_url` or `url` must exactly match
`OWNER/REPO` and the issue number as a browser or API URL, and the supplied
`node_id` must match the authoritative issue read from GitHub. The plan uses
the authoritative issue body and `node_id`. Foreign or inconsistent identities
fail before any project or comment mutation; outdated bodies are refreshed
from GitHub.

To rehearse the catalog-failure path without evaluating:

```sh
GH_TOKEN=<project-capable-token> \
python3 tools/model_coverage_sync.py \
  --repository openshift-psap/inference-playbooks \
  --project-owner ORG --project-number NUMBER \
  --mark-stale --stale-reason "manual stale rehearsal"
```

`--mark-stale` updates only same-repository items whose content Issue is in
this repository, whose `Tracker Issue` value equals that Issue URL, and whose
`Tracker Status` is one of the tracker-owned status values. It never adds
items or posts comments, and it leaves foreign or human project items alone.

### Apply

```sh
MODEL_TRACKER_WRITE_ENABLED=true \
MODEL_TRACKER_ALLOW_LOCAL_WRITE=true \
GH_TOKEN=<project-capable-token> \
python3 tools/model_coverage_sync.py \
  --repository openshift-psap/inference-playbooks \
  --project-owner ORG \
  --project-number NUMBER \
  --apply
```

Live writes are refused unless all applicable guard conditions hold:

- `MODEL_TRACKER_WRITE_ENABLED=true`;
- in Actions, the event is not `pull_request`, `pull_request_target`, or
  `workflow_run`;
- the ref is the event repository's `refs/heads/<default_branch>`
  (`MODEL_TRACKER_DEFAULT_BRANCH`, set from
  `github.event.repository.default_branch` in Actions), except the `issues`
  event, which is allowed only with `GITHUB_REPOSITORY` present;
- local runs additionally set `MODEL_TRACKER_ALLOW_LOCAL_WRITE=true`.

The sync is idempotent:

- one project item per issue URL; existing items are reused;
- only changed owned field values are written; `clear_field` is used only when
  an optional text field becomes empty;
- one comment marker `<!-- inference-playbooks:model-coverage -->`, authored by
  the exact configured/authenticated identity, is created or updated;
- duplicate owned marker comments are **reported and left in place** for manual
  review, never deleted; marker comments owned by another identity or by a human
  are never edited;
- an invalid definition updates or creates its item as `Error`, sets
  `Completed` and `Blocked` to `Unknown`, replaces old `Coverage` with an
  explicit unknown value, and rewrites the owned comment with the error and
  evaluated SHA. A malformed issue that has no project item yet is added with
  those same error values;
- valid models in the same report are still updated; one bad issue does not
  abort the run;
- `Last Evaluated` and comments carry the evaluated catalog SHA, with no
  timestamp churn.

API failures include the `gh` operation and stderr text and stop the run.
Project items and fields are read with pagination; issues and comments use
paginated REST reads. The applied plan and result/audit are written to disk
before and after mutations so CI can upload them even on failure.

The stale path is separate: a validation or evaluator failure in CI records a
reconciliation failure and runs the narrow `--mark-stale` job; a catalog
failure during sync builds the same explicit stale plan and returns nonzero
after retaining the plan/audit. Normal sync also marks previously owned,
same-repository project items that are no longer in the report `Stale`/`Unknown`
and leaves them in the project for manual review. It does not add items for the
stale path, write comments, delete items, or touch human fields.

### Sync disabled mode

`MODEL_TRACKER_WRITE_ENABLED` unset or `false` is an explicit supported
operating mode, not proof of a runtime failure. In this mode:

- the read-only `check` job still validates, tests, evaluates, uploads its report
  artifact, and publishes the report and definition errors in the workflow
  summary, which also states that dashboard sync is disabled;
- the issue-event intake job still assigns the default triage owner on first
  intake when appropriate and still posts or updates the issue-specific feedback
  comment;
- the `sync` and `stale` jobs are skipped, so no Project items, fields, or
  dashboard sync comments are written, and no App token is minted.

Do not enable the sync, change repository variables, or alter authentication to
obtain intake feedback. The feedback comment and workflow summary are the
intended feedback path while sync remains disabled. Enabling sync later is a
separate reviewed operation with the project setup and App permissions above.

## Workflow

`.github/workflows/model-coverage.yml` runs:

- on every push to `main`;
- on tracking-issue changes (`opened`, `edited`, `reopened`, `closed`,
  `assigned`, `unassigned`, `labeled`, `unlabeled`, `transferred`);
- manually with `workflow_dispatch`;
- daily for reconciliation, so merged evidence updates coverage even without a
  tracker edit;
- on relevant pull requests for read-only tests/evaluation.

The `push` trigger keeps the literal `main` branch because workflow trigger
configuration cannot read the event context. A fork that renames its default
branch must update `on.push.branches`; the `sync`/`stale` write guard, checkout,
and `MODEL_TRACKER_DEFAULT_BRANCH` still derive the event repository's actual
default branch at runtime.

The `check` job has `contents: read` and `issues: read` and never has a write
token. It runs `python3 tools/validate.py --current` and
`python3 -m pytest tests/test_model_coverage.py -q` — the same commands used
locally — then evaluates issues without project access. It uses
`--allow-errors` so invalid definitions still produce report/error artifacts
instead of blocking every valid model update. The report, including a
`## Validation` section with definition errors and warnings, is written to the
workflow summary. Report artifacts are uploaded on success and failure.

The `feedback` job runs on tracking-issue `opened`, `edited`, `reopened`,
`assigned`, and `unassigned` events, independent of
`MODEL_TRACKER_WRITE_ENABLED`. It checks out the trusted default branch, has
only `contents: read` and `issues: write`, and uses the workflow
`GITHUB_TOKEN`. It never receives the `forge-evidence` App secret or project
credentials. It attempts the default triage-owner assignment only on first
intake: open issue, ownerless parseable YAML, no assignees, and no
`assigned`/`unassigned` history. It writes the issue-specific feedback comment
for every listed event and revalidates assignment eligibility immediately
before writing. If the triggering definition is invalid, the job posts or
updates the feedback and then fails so the run is not mistaken for valid
intake; valid models in the same evaluation are unaffected because the check
job uses `--allow-errors`.

The `sync` job exists only after a successful `check`, only when
`MODEL_TRACKER_WRITE_ENABLED == 'true'`, and only for
default-branch/issues/manual/schedule contexts. It checks out the default
branch, runs in the protected `forge-evidence` environment, mints a
short-lived repository-scoped App token with organization Projects write,
Issues write, and Contents read, and applies the same plan used by the dry
run. The token is passed only to the sync step and revoked at job completion;
the job's default `GITHUB_TOKEN` remains read-only. Comment ownership is
resolved from the token's `app-slug` output as `<app-slug>[bot]`, falling back
to `MODEL_TRACKER_BOT_LOGIN` when no slug is available. Pull requests cannot
reach this job.

The `stale` job runs when the validation or evaluator step failed (for example
a schema failure or catalog build failure), not when a test-only step failed,
and only when the same trusted-context conditions hold. It runs in the same
`forge-evidence` environment, mints the same narrow App token, and invokes
`--mark-stale --apply` before uploading its plan/result. All jobs share one
`model-coverage` concurrency group, so pushes, issue edits, manual runs, and
the schedule cannot write in parallel. `cancel-in-progress: false` protects an
active run, and `queue: max` queues up to GitHub's documented maximum of 100
pending runs instead of replacing a pending run. This is a bounded queue, not
an unbounded delivery guarantee; a burst beyond the bound can still drop
pending runs.

## Pilot verification

The RHOAI 3.6 pilot tracking issue
[#120](https://github.com/openshift-psap/inference-playbooks/issues/120)
uses the form's target-definition format, has the `model-coverage` label, declares an explicit
owner, and uses the quoted `version: "3.6"` example for RHOAI 3.6. Its issue-event workflow
run completed the read-only check and skipped project sync:
https://github.com/openshift-psap/inference-playbooks/actions/runs/42649345183.

An offline evaluator run against the submitted issue body and the local catalog
at `a1c43c0` produced zero definition errors and zero warnings. It parsed
`glm-5.3` as planned with owner `@thameem-abbas` from the YAML definition; the
target `glm-5.3-dep8-mtp3-hisparse` / `rhoai 3.6` remains planned because that
platform is not yet in the repository. This verifies submitted-body parsing and
evaluator output, not a live dashboard write.

Live Project and dashboard-comment sync remain untested with
`MODEL_TRACKER_WRITE_ENABLED` disabled. The separate intake feedback job
introduced with this change remains untested live until it is merged; it does
not require that switch. After merge, the next tracking issue edit exercises
the feedback path without enabling project sync.

## Ownership and maintenance

- Each model has exactly one tracking issue. An explicit YAML owner, or the
  effective owner derived from issue assignees for an ownerless definition, is
  the accountable owner shown in the report and dashboard.
- Owners update blockers, Jira keys, and target scope by editing the fenced YAML
  in their issue. Delegating an ownerless issue by changing assignees does not
  require a YAML edit; an explicit YAML owner remains authoritative.
- Maintainers add planned models by opening a new issue from the form. To retire
  a plan, remove both the `model-coverage` label and the tracker marker after an
  explicit decision; closing the issue alone does not retire it or free the
  model ID.
- Recipe authors do not maintain a second status file. When a recipe merges,
  the next main push/evaluation changes `planned`/`in-progress` to
  `contributed`. When benchmark evidence for the exact platform is committed,
  the scheduled/manual reconciliation changes it to `validated`.
- Human project fields are never overwritten; only the eleven fields listed
  above are tracker-owned.
- If a model is in the catalog but has no issue, add an issue rather than
  editing the tracker code or the report.

## Limitations

- The tracker cannot infer a planned recipe ID from prose. A target
  `recipe_id` that matches no catalog recipe for this model is reported as a
  warning and stays `planned`, not a hard error, because planned recipes
  legitimately do not exist yet. Check the recipe's `recipe_id` field rather
  than its directory name. Recipe IDs are scoped per model; the same ID under a
  different model produces a scoped warning but does not conflict.
- Stretch goals are prose only. They are intentionally not represented in the
  YAML schema, are never parsed, and never count toward coverage or completion.
  Required work must stay in `targets`.
- Automatic default assignment is best-effort and limited to the first `opened`
  event. It requires readable issue and catalog state, parseable ownerless YAML,
  an open issue, no current assignees, and no prior `assigned`/`unassigned`
  history. Malformed YAML is deliberately not assignment-eligible, and a failed
  issue or catalog read can skip assignment; feedback is still attempted where
  possible. There is no retroactive backfill for existing ownerless issues, and
  later edits or manual unassignment do not reassign the issue. Replaying an
  original `opened` event cannot reassign because assignment history blocks it.
  An explicit YAML owner is never auto-assigned.
- `validated` completion requires the catalog's `same-engine` badge for the
  exact target platform. An `earlier-engine` run is compatible evidence, not
  independent validation of the current engine; neither a different
  stack/version, a compatibility signature alone, nor CI success is accepted.
- The tracker does not call Jira, copy Jira content, or verify that a Jira key
  exists.
- The sync does not create the Project, label, fields, options, variables, or
  secrets. Those are one-time manual setup steps. The intake feedback job also
  does not create the label.
- Read-only GitHub issue/GraphQL reads were exercised from this worktree; no
  live Project or issue was mutated. A live dashboard demonstration requires
  project credentials. Opening a tracking issue and merging a matching recipe
  exercises the same evaluator/sync code path after setup.
- One project is supported per repository configuration. Multi-project fan-out
  is deliberately out of scope.
