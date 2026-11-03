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
| `.github/ISSUE_TEMPLATE/model-coverage.yml` | Issue form with the machine-readable fenced YAML target definition. |
| `tools/model_coverage.py` | Deterministic evaluator. Reads issues JSON plus the catalog and emits JSON or Markdown. |
| `tools/model_coverage_sync.py` | Read-only/dry-run planner and opt-in GitHub Projects v2 writer. |
| `.github/workflows/model-coverage.yml` | Read-only checks on PR/main/issue events; trusted default-branch-only sync. |
| `tests/test_model_coverage.py` | Offline tests for parsing, completion rules, idempotence, pagination, and write guards. |

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

## Issue definition

Create one issue per model using the **Model coverage target** form. The form
prefills the marker comment and a fenced block in the editable body. Keep both,
keep exactly one fenced `yaml` block, and edit the block in place:

```yaml
schema_version: 1
model_id: kimi-k3
owner: "@kimi-owner"
completion: contributed
# jira: RHOAIENG-123
blockers: []
targets:
  - recipe_id: kimi-k3-vllm-h200-pp2-tp8
    platforms:
      - stack: vllm
        version: v0.29.0
```

Rules:

- `model_id` matches `models/<model-id>/model.yaml` when the model exists. A
  model that is not yet in the repository is a valid planned entry; it is
  reported under `planned_models`.
- `owner` is one GitHub user or team handle (`@octocat` or `@org/team`).
- `completion` is `contributed` or `validated` and applies to every platform
  unless a platform entry sets its own `completion`.
- `recipe_id` is the v4 `recipe_id` field, not the directory name. Recipe IDs
  are scoped by model: the same recipe ID may legitimately exist under another
  model.
- `platforms` are exact `stack`/`version` values declared by that recipe. An
  absent pair stays `planned`, which is the normal state for a backlog entry.
  An optional `completion` on a platform overrides the model-level bar for that
  platform only.
- `blockers` is an optional public list (at most 20). Do not include
  confidential details.
- `jira` is an optional internal Jira **key only**. The evaluator never calls
  Jira and renders only:
  **Refer to internal Jira for more details on this task.**
  Do not paste Jira descriptions, links, comments, or discussions.
- Replace the form’s placeholder values. Unknown keys, duplicate model
  declarations, duplicate target platforms, malformed YAML, non-string mapping
  keys, a non-integer `schema_version`, invalid Jira keys, and unfilled
  placeholders are hard errors. They include the issue number and field
  location. Two issues declaring the same `model_id` invalidate **both**; the
  tracker does not publish the first one as complete.
- The label `model-coverage` must exist before the form is used. Create it
  manually once (this repository change did not create it):
  `gh label create model-coverage --color 5319E7 --description "Model coverage tracker entry"`.
  The form's label and the marker comment both identify a tracking issue;
  either one is sufficient. The marker lives inside the editable target
  definition because GitHub issue forms do not submit `markdown` block content.

Closing a tracking issue does not retire the plan or free the model ID. The
evaluator reads the issue body and ignores issue state, so a closed issue still
owns its `model_id` and is still evaluated. To untrack a model, remove **both**
the `model-coverage` label and the
`<!-- inference-playbooks:model-coverage-tracker -->` marker comment. Removing
only one of them leaves the issue tracked. Deleting or emptying the fenced YAML
without removing both identifiers produces an invalid tracked definition
(`Error`); it does not untrack the model. Transfer or delete the issue if it
should disappear entirely.

## Existing untracked models

Any model in the catalog without a tracking issue appears under
`untracked_models` in JSON and **Existing models without a tracking issue** in
Markdown. This is a report, not an automatic completion or an error. Until the
agreed backlog is filed as tracking issues, the evaluator reports all current
models as untracked.

Malformed definitions return exit code 1 locally. The workflow check runs the
evaluator with `--allow-errors`, writes the report and errors as artifacts, and
lets the sync job mark each affected project item `Error` while continuing to
update valid models. Untracked models do not fail the run; they are kept visible
so the backlog cannot silently disappear.

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
   - `MODEL_TRACKER_BOT_LOGIN` — the exact login that authors tracker
     comments (the PAT/service-account or GitHub App bot login). Set this when
     the token cannot read `viewer`; otherwise the tool resolves it and falls
     back to REST `/user`.
   - `MODEL_TRACKER_WRITE_ENABLED` — leave unset or `false` until the dry run
     and dashboard are reviewed. Set to `true` only to enable the trusted sync.
5. Create repository **secret** `MODEL_TRACKER_PROJECT_TOKEN` with the minimum
   scopes:
   - classic PAT: `project` and `public_repo` (or `repo` for a private fork);
   - fine-grained token: organization Projects read/write, repository Issues
     read/write, Contents read, Metadata read.

   The default `GITHUB_TOKEN` is used only for read-only checks. The project
   credential is passed only to the sync step. PR jobs never receive it and the
   workflow never runs sync for a pull request event. Prefer a dedicated
   service account or GitHub App token: comments are authored by that exact
   identity, and only marker comments whose `user.login` matches
   `MODEL_TRACKER_BOT_LOGIN` (or the resolved viewer/`/user` login) are updated.
6. Add the `model-coverage` label (see above) and open the first tracking issue.

The current maintainer `gh` token available during this implementation lacks
`read:project`. No project, field, issue, label, comment, variable, secret, or
branch was changed. Do not use this environment for the first live setup; use a
project-scoped credential and follow the steps above.

### Dry run

With a read-capable project credential:

```sh
GH_TOKEN="$MODEL_TRACKER_PROJECT_TOKEN" \
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
GH_TOKEN="$MODEL_TRACKER_PROJECT_TOKEN" \
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
GH_TOKEN="$MODEL_TRACKER_PROJECT_TOKEN" \
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

## Workflow

`.github/workflows/model-coverage.yml` runs:

- on every push to `main`;
- on tracking-issue changes (`opened`, `edited`, `reopened`, `closed`,
  `labeled`, `unlabeled`, `transferred`);
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
instead of blocking every valid model update. Report artifacts are uploaded on
success and failure.

The `sync` job exists only after a successful `check`, only when
`MODEL_TRACKER_WRITE_ENABLED == 'true'`, and only for
default-branch/issues/manual/schedule contexts. It checks out the default
branch, receives the project secret only in its sync step, and applies the same
plan used by the dry run. A missing secret fails with an explicit setup
message. Pull requests cannot reach this job.

The `stale` job runs when the validation or evaluator step failed (for example
a schema failure or catalog build failure), not when a test-only step failed,
and only when the same trusted-context conditions hold. It invokes
`--mark-stale --apply` and uploads its plan/result. All jobs share one
`model-coverage` concurrency group, so pushes, issue edits, manual runs, and
the schedule cannot write in parallel or create duplicate comments.

## Ownership and maintenance

- Each model has exactly one tracking issue and one owner.
- Owners update blockers, Jira keys, and target scope by editing the fenced YAML
  in their issue.
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

- The tracker cannot infer a planned recipe ID from prose. A typo in a target
  `recipe_id` that matches no recipe is reported as `planned`, not a hard
  error, because planned recipes legitimately do not exist yet. Recipe IDs are
  scoped per model; the same ID under a different model does not conflict.
- `validated` completion requires the catalog's `same-engine` badge for the
  exact target platform. An `earlier-engine` run is compatible evidence, not
  independent validation of the current engine; neither a different
  stack/version, a compatibility signature alone, nor CI success is accepted.
- The tracker does not call Jira, copy Jira content, or verify that a Jira key
  exists.
- The sync does not create the Project, label, fields, options, variables, or
  secrets. Those are one-time manual setup steps.
- Read-only GitHub issue/GraphQL reads were exercised from this worktree; no
  live Project or issue was mutated. A live dashboard demonstration requires
  project credentials. Opening a tracking issue and merging a matching recipe
  exercises the same evaluator/sync code path after setup.
- One project is supported per repository configuration. Multi-project fan-out
  is deliberately out of scope.
