# Model coverage tracker change log

## Change

- Issue: #105 — Build a CI-backed tracker for planned and completed model
  coverage.
- Base commit: `d1258fe` (`d1258fe8e745ad7ee45ffa42e79c7351e1a2ab00`).
- Branch: `feat/model-coverage-tracker`, created locally in a git worktree from
  the base commit. The branch was never pushed.
- Date: 2026-11-02.
- External changes: **NONE** (no mutations). No issue, label, Project, field,
  comment, variable, secret, commit, or push was created. A local worktree
  branch exists; remote Project authentication is still unavailable because the
  current maintainer `gh` token lacks `read:project`.

## Worktree setup

1. `git worktree add <worktree-path> -b feat/model-coverage-tracker d1258fe`
2. `cd <worktree-path>` and run all edits, tests, and validation there.
3. No commit or push was made from the worktree.

## Read-only GitHub access performed

- A real GraphQL variable query succeeded from this worktree.
- Fetched 30 repository issues with the authenticated `gh` token.
- Evaluator result over those issues: 0 tracked, 4 untracked, 0 errors.
- These were read-only calls, separate from the external-changes statement:
  no Project, issue, comment, label, field, variable, secret, or branch was
  mutated or pushed.

## Files added

- `.github/ISSUE_TEMPLATE/model-coverage.yml` — issue form with one fenced,
  machine-readable YAML target definition.
- `.github/workflows/model-coverage.yml` — read-only PR/main/issue checks and a
  separately gated trusted-default-branch Projects sync.
- `tools/model_coverage.py` — deterministic evaluator. Joins tracking issues to
  `tools/catalog.py::build_catalog` output and emits JSON/Markdown.
- `tools/model_coverage_sync.py` — idempotent Projects v2 and marker-comment
  sync. Dry run by default; `gh` CLI plus Python standard library only.
- `tests/test_model_coverage.py` — 47 offline tests for parsing, partial
  completion, per-platform criteria, exact-platform and same-engine validation,
  recipe scoping, untracked models, deterministic output, idempotence,
  human-field preservation, error/stale reconciliation, untracked-item stale
  reconciliation, foreign-item scoping, comment ownership, returned comment IDs,
  GraphQL transport, pagination, API failures, and write guards.
- `docs/model-tracker.md` — maintainer workflow, status rules, Project setup,
  credentials, CI behavior, local commands, limitations, and rollback pointer.
- `docs/model-tracker-change-log.md` — this record.

## Files modified

- `README.md` — links the tracker from the Models section. No generated catalog
  or manifest changes.

No recipe, schema, hardware profile, catalog artifact, manifest, Jira content,
or benchmark result was changed.

## Implementation notes

- Reuses `build_catalog`, `compatibility_signature`/`validation_badge` results,
  and `load_unique_yaml` rather than duplicating maturity or evidence logic.
- Evidence is filtered to the exact target stack/version before calling
  `validation_badge` again, and a same-engine run is preferred, so an earlier
  cross-platform badge cannot hide a later exact run.
- A model is complete only when every declared recipe/platform target meets its
  own `contributed` or `validated` criterion (per-platform override, model-level
  default). Partial targets remain incomplete.
- `validated` requires committed recipe evidence whose tested platform exactly
  matches the target and whose badge is `same-engine`. CI success, recipe
  maturity alone, a badge for a different platform, and `earlier-engine`
  compatibility evidence do not independently validate the current engine.
- Malformed definitions, duplicate model declarations (both invalidated),
  duplicate target platforms, unknown/non-string keys, a non-integer
  `schema_version`, and invalid Jira keys are actionable errors with the issue
  number and field location.
- Catalog models without a tracking issue appear under `untracked_models`; this
  is a report, not an implicit completion.
- Jira support is only an optional key plus the fixed note
  “Refer to internal Jira for more details on this task.” There is no Jira
  retrieval or content copy.
- Projects sync owns only `Model ID`, `Owner`, `Completion Criterion`,
  `Completed`, `Tracker Status`, `Blocked`, `Coverage`, `Jira`, `Blockers`,
  `Tracker Issue`, and `Last Evaluated`. Human fields and human comments are
  never overwritten. Marker comments are owned only when their exact login
  matches the configured/resolved sync identity; duplicates are warned about
  and never deleted. Real writes require `MODEL_TRACKER_WRITE_ENABLED=true`, a
  project-scoped secret, and a trusted default-branch context; PR events are
  rejected. The default remains dry run.
- An invalid issue definition marks its item `Error` with unknown coverage and
  a SHA-stamped error comment; valid models in the same run still update.
  Validation or evaluator failure reaches the narrow `--mark-stale` job. A
  catalog build failure during sync builds an explicit stale plan, retains the
  plan/audit, and returns nonzero. Normal sync also marks previously owned,
  same-repository items that are no longer in the report `Stale`/`Unknown`
  without deleting them.
- Pagination covers GraphQL project items/fields and paginated REST issues and
  comments. GraphQL raw input uses the required `{query, variables}` body. API
  failures include the failed `gh` operation and stderr.
- The issue form’s `model-coverage` label must be created manually before use.
  No Project, field, label, variable, or secret was created.

## Review fixes

The maintainer review found nine substantive gaps; all are fixed with
regression tests:

1. GraphQL raw input now sends `{query, variables}` instead of spreading
   variables beside `query`; the exact transport payload is tested.
2. Validation evidence is filtered to the exact target stack/version and passed
   back through `validation_badge`. Competing ordered evidence prefers an exact
   run; `same-engine` is required, and `earlier-engine` is compatible evidence,
   not independent validation.
3. Recipe IDs are scoped per model. A target missing under this model is
   `planned` even when the same ID exists under another model.
4. `platforms[].completion` can override the model default; coverage counts and
   rendered rows use each platform's own criterion.
5. Invalid issue definitions no longer abort sync. Their items become `Error`
   with unknown coverage, old `Validated` values are replaced, both duplicate
   model issues are invalidated, valid models still update, and comments/fields
   carry the evaluated SHA. Validation or evaluator failure has an explicit
   `--mark-stale` path and workflow job; sync also handles a catalog build
   failure explicitly.
6. All events share one concurrency group. The check job uses `--allow-errors`,
   reports are uploaded always, the plan is persisted before apply, result/audit
   files capture completed actions on failure, and sync artifacts are uploaded
   always.
7. Comment ownership matches the exact configured/authenticated login (not any
   `Bot`), duplicate comments are warned about and never deleted, and
   installation-token identity falls back to `MODEL_TRACKER_BOT_LOGIN` or REST
   `/user`.
8. Non-string mapping keys raise `DefinitionError` instead of `TypeError`, and
   `schema_version` must be an integer (a float `1.0` is rejected).
9. Filterable `Completed` and `Blocked` fields are separate from the lifecycle
   `Tracker Status`; `Blockers` carries manual blockers, and the new
   `Last Evaluated` field carries the SHA. Unused variables were removed while
   touching this code.

## Final review fixes

1. Catalog stale reconciliation now requires same-repository Issue content,
   an owned `Tracker Status`, and a `Tracker Issue` value equal to the content
   URL. Foreign, unowned, and mismatched project items are untouched.
2. Normal sync compares previously owned same-repository items against all
   report model and error URLs. Items that disappeared from the report become
   `Stale`/`Unknown`, are warned about, and are not deleted. Deleted or
   transferred issues fall back to the owned `Tracker Issue` URL while still
   requiring the repository URL gate.
3. The workflow records either a validation or evaluator failure (`id:
   validate`, `id: evaluate`) for the stale job; a test-only failure does not
   trigger it. A catalog build failure inside sync creates an explicit stale
   plan, writes the plan/audit, and returns nonzero without treating unrelated
   API errors as blanket stale mutations.
4. Created-comment audit entries record the REST-returned comment ID, and
   update-comment plan actions preserve `previous_body` for rollback.
5. Rollback instructions distinguish `git revert <squash-sha>` from
   `git revert -m 1 <merge-sha>` and require cancelling in-flight runs and
   disabling the workflow first. The change log now records the local worktree
   branch and successful read-only GitHub reads separately from the no-mutation
   statement.

## Validation performed

Commands were run in this worktree with the local Python environment:

```sh
python3 -m pytest tests/test_model_coverage.py -q
# 47 passed

python3 -m pytest tests -q --ignore=tests/e2e
# 838 passed, 14 skipped, 35 subtests passed (full suite before the final
# narrowly-scoped review fixes; the tracker suite was rerun after them)

python3 tools/validate.py --current
# Validated schemas, references, and hardware-profile corrections.
# Exit 0. Existing legacy-engine warnings were unchanged.

python3 -m py_compile tools/model_coverage.py tools/model_coverage_sync.py
# Exit 0.
```

Additional local checks:

```sh
python3 tools/model_coverage.py --issues <empty-issues.json> --format markdown
# Reported no tracker errors and listed the four existing repository models as
# untracked: gemma-4, glm-5.2, kimi-k3, qwen3-235b-a22b.

python3 tools/model_coverage.py --issues <empty-issues.json> --format json
# Deterministic JSON with catalog source SHA and empty model list.
```

No live Projects sync or mutation was run. Read-only issue/GraphQL reads were
performed as recorded above. The sync planner, idempotence, pagination, write
guards, and API error paths are covered by injected-client tests.

## Final parent-agent verification

- Personally reviewed the evaluator, synchronization code, and workflow; returned
  findings to DeepSeek on EnMaaS for correction in two review rounds.
- Re-ran the final focused suite: `47 passed`.
- Ran a local synthetic tracking-issue demonstration against the real catalog:
  Gemma-4 was correctly `in-progress`, with `0/1` contributed targets complete.
  No issue was created and the example owner was a placeholder. Temporary
  reports are under `/tmp/opencode/model-tracker-demo/`.
- At the end of local verification, changes were uncommitted on
  `feat/model-coverage-tracker`. On 2026-11-03 the user authorized committing
  and pushing this branch. Live Project setup and dashboard verification remain
  pending credentials and deployment.

## Rollback

### Disable writes first

1. Cancel in-flight `Model coverage` runs in the Actions UI or with
   `gh run cancel <run-id>`. Setting the variable does **not** stop jobs that
   already passed their write-gate check.
2. Set `MODEL_TRACKER_WRITE_ENABLED` to `false` and/or disable the workflow
   (`gh workflow disable model-coverage.yml` or the Actions UI). The variable
   only affects future jobs; disabling the workflow prevents new runs.
3. Revert this change through a reviewed pull request on a new branch. Use the
   right form for the merge strategy:
   - squash merge: `git revert <squash-merge-commit-sha>`
   - merge commit: `git revert -m 1 <merge-commit-sha>` (mainline parent 1)
   Open the PR and merge normally. Do **not** use `git reset --hard`,
   force-push, or rewrite the default branch.
4. Reverting the workflow stops all tracker writes. Reverting the issue form
   stops new entries; existing tracking issues remain readable but will no
   longer be evaluated by CI.

### Remove only tracker-owned external state

These are manual maintainer actions only if live sync was enabled and cleanup
is requested. They were **not** performed by this change.

- Remove only the eleven tracker-owned project fields from
  `docs/model-tracker.md#project-setup`: `Model ID`, `Owner`,
  `Completion Criterion`, `Completed`, `Tracker Status`, `Blocked`, `Coverage`,
  `Jira`, `Blockers`, `Tracker Issue`, `Last Evaluated`. Do not delete default
  or human fields.
- Remove only tracker-owned comments (bot or authenticated service identity)
  whose body starts with `<!-- inference-playbooks:model-coverage -->`.
  Preserve human comments and human-authored marker text. GitHub UI or
  `gh api --method DELETE repos/OWNER/REPO/issues/comments/COMMENT_ID` for a
  tracker comment ID confirmed by maintainers.
- Project items added by the sync are ordinary issue links; remove them only
  after the tracking issue decision is recorded, using the Project UI.
- Remove the `MODEL_TRACKER_PROJECT_TOKEN` secret and tracker variables if the
  workflow is permanently retired.

No repository history or generated artifact requires rollback beyond the
reviewed `git revert` PR.

## Known limitations

- A target `recipe_id` typo that matches no recipe is indistinguishable from a
  legitimate planned recipe and is reported as `planned`; recipe IDs are scoped
  per model, so an ID under another model is not a conflict.
- `earlier-engine` evidence is reported as compatible evidence but is not
  independent validation of the current engine.
- The tracker never queries or validates Jira.
- Project, label, fields, options, variables, and secret setup is manual.
- No live dashboard demonstration was possible without project credentials.
  Opening a tracking issue and merging a matching recipe exercises the same
  evaluator/sync code path after setup.
