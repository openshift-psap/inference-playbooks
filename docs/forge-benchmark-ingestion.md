# Forge benchmark evidence ingestion

The importer supports Forge bundle contract v1 (namespace placement only) and
v2 (namespace placement plus an explicitly recorded CI node-selector override
for a Deployment). For v2, execution configuration and bundle metadata must
agree, authored selectors cannot conflict, and an authored `nodeName` is not
allowed. Source/submitted bundle checksums and complete document comparison
still reject any other launch changes. Unknown versions remain unsupported.
CI placement is validated at import time only: it is not copied into recipe
inputs, rendered manifests, or catalog fields. Existing execution references
retain the audit trail without publishing private node-selector values.

This page describes post-merge ingestion and backfill. For importing directly
into an open recipe PR, see the [maintainer workflow](../tests/forge/README.md#maintainer-workflow-open-pr).
Bot commit signing and PR-wide signature verification are described under
[commit signatures and PR verification](#commit-signatures-and-pr-verification-issue-100).

`benchmark-ingest.yml` finds merged recipe PRs on a `main` push, or backfills
one merged PR through `workflow_dispatch`. It selects only the latest explicit
successful `psap-forge-bot[bot]` report, ordered by GitHub `created_at` then ID.
The primary `[mlflow results]` or `[MLFlow Test Results]` link must identify a
parent run on the configured origin. Its workspace selects the MLflow request
header: `forge-sandbox`, `forge-inference-playbooks`, and the configured
`MLFLOW_WORKSPACE` are approved. Links without a workspace use `MLFLOW_WORKSPACE`
(default `forge-sandbox`). Empty, repeated, or unapproved workspace values and
primary links disagreeing on workspace/parent are rejected. Nested run/log links
are ignored. Each PR selects its own workspace, including in a multi-PR push;
there is no workspace probing or retry in another workspace.
Failed, ambiguous, stale, unavailable, or unattributed evidence fails closed.
No cluster access is used. Imports open a separate evidence PR, never push to
`main`, and never promote maturity or platform verification.

Current Forge executions publish consolidated KPIs on the selected parent at
`01__test/<ordinal>__postprocessing/kpis/kpis.json`. Import these without
enumerating children; ignore `historical_data` archives, never substitute them.
Legacy executions without consolidated parent KPIs retain direct-child intake.
The selected execution's tested revision must be the final recipe PR head or
an ancestor of it, with unchanged recipe and deployment inputs. Evidence and
receipts retain the actual tested SHA, not the final head or squash-merge SHA.
For tagless current executions, use recorded `FORGE_FOREIGN_TESTING_PULL_PULL_SHA`,
not the Forge checkout's `PULL_PULL_SHA`. This is the CI-requested/tested recipe context, not
an independent attestation of the actual checkout. A missing/malformed primary link,
empty execution, attribution mismatch, unsupported/malformed artifact, or
permission/network error fails ingestion. **There is no fallback to older
executions.** No trusted success report and automation evidence PRs remain no-ops.

Successful complete ingestion writes one positive `forge-import.json` receipt
beside its first output `run.yaml`. The parent must be `FINISHED`; for legacy
direct-child discovery, any nonterminal direct child blocks admission before
writes. Terminal `FAILED`/`KILLED` children remain ineligible. The receipt binds the entire normalized positive output set,
source PR/revision, origin/workspace/parent identity, recipe ownership, and Git
blob identities for each raw/result/run file. It stores no empty-run outcomes.

Repeat ingestion builds one local lookup from committed recipe references and
anchored receipts. A selected admitted execution is a successful no-op with no
MLflow requests or KPI replay. Receipt/source files must be committed and clean;
blob identities, references, metadata, and current deployment attribution must
agree. Unreferenced receipts cannot authorize a skip. Full CI integrity/replay
validation remains a separate gate; admission receipts assume reviewed importer
output and attest a completed discovery snapshot, not future server immutability.
Legacy evidence without a receipt requires normal selected-execution discovery
and validation once before admission; immutable evidence and original comment
provenance are preserved. Direct `fetch_forge_evidence.py` still accepts child
links but does not create ingestion receipts; its explicit `--tested-revision`
must still match the recorded revision exactly.

The standalone ingestion change (PR1) adds the evidence tools, schemas,
offline validator replay, tests, and workflow without depending on site/catalog
tooling or changing existing recipes. The catalog/UI change (PR2) consumes this
contract to show individual runs, smoke stat cards, and MLflow/Prow provenance
links. It adds a site-build check after ingestion validation; generated bundles
remain build artifacts and are not included in the evidence import PR.
The separate Pages workflow builds and publishes reviewed evidence after merge
to `main`; ingestion itself does not publish the site or promote recipe maturity.

## Execution metadata

Consolidated parent imports do not require custom `playbooks.*` tags. Intake
always checks the recorded execution artifacts, including the versioned bundle
contract for current producers. Complete tags must agree with those artifacts;
they cannot bypass validation. Recorded inputs supply the benchmark-run fields:

- `01__test/000__ci_metadata/fournos_fjob.yaml`: foreign recipe PR number and
  requested/tested recipe SHA from `spec.env`, checked against the final PR history.
- `01__test/001__inference_playbooks_test_dir/config.yaml`: explicit recipe and
  manifest selection, matched to the tested recipe, model, profile, and platform.
- `01__test/run.log`: the recorded common Forge test invocation. It is not
  reconstructed from configuration.

Within that test directory, the serving inputs are fixed by kind:

| Kind | Launch configuration | Runtime digest source |
| --- | --- | --- |
| Deployment | `src/<unique-name>-deployment.yaml`, `vllm` container arguments | Matching ready serving-container status in `01__test/run.log` |
| LeaderWorkerSet | `src/<unique-name>-lws.yaml`, nested leader/worker templates and shell `vllm serve` arguments | `<ordinal>__capture_lws_state/artifacts/leaderworkerset.pods.yaml` |
| LLMInferenceService | `src/<unique-service-name>.yaml`, `spec.model`, template/worker configuration and captured serving-pod arguments | `<ordinal>__capture_llmisvc_state/artifacts/llminferenceservice.pods.yaml` |

Only the known `src/` and test directory are listed to resolve variable launch
names and capture ordinals. There is no recursive runtime-artifact search or
alternate digest-source fallback. LWS selects `vllm`, `vllm-leader`, or
`vllm-worker` containers from its role templates. LLMInferenceService selects
the `main` container running vLLM, not router, sidecar, or init containers.
The captured launch spec must match the selected committed manifest at the
tested recipe SHA, including platform overrides and Kustomize patches already
materialized there. Historical renderers/PR code are never executed. Only the
audited Forge additions are normalized: run labels/selectors, pod-level UID
removal, and absent HOME/USER defaults. Other environment/security/storage/
argument/resource changes fail. Undeclared HF-to-PVC rewrites and RDMA resource
rewrites currently fail rather than being guessed. Declared manifests also
participate in local receipt-input checks.

Model identity and load location are separate: an HF checkpoint, filesystem
path, and API alias need not be equal. Any supplied model identity labels must
match the checkpoint declaration; aliases cannot authorize another PVC/path.
Harnesses need not duplicate those labels: for example, current AIPerf output
can contain only phase/tool/concurrency labels. Such records retain tested
execution/manifest attribution without synthesized model labels or normalized
`model_identity`. Missing labels are not independent proof of weight identity.
LWS runtime model arguments must agree with the tested launch. LLMInferenceService
controller-generated local paths must resolve to its declared PVC/mount/subpath,
not a hardcoded `/mnt/models` exception.

Declared revisions come from serving arguments or `VLLM_ADDITIONAL_ARGS`;
omission uses the accepted `main` default. Model-less LWS workers share the
leader's model load. LLMInferenceService supports HF and PVC model URIs; PVC
contents, cache preparation, and weight revisions are not independently checked.
The recipe and ordinary run model labels supply checkpoint identity. A local
`/mnt/models` path or `spec.model.name` request alias is not treated as an HF ID.

Serving roles must provide ready runtime image digests. Different serving-role
digests are explicitly unsupported by the existing single-`image_digest` contract;
no role is silently preferred. Missing or ambiguous required records reject
intake. LLMInferenceService prefill-role layouts remain unsupported.

Any supplied attribution tags must agree with acquired execution values. No
execution logs/configuration are committed: run-level provenance uses the existing
parent MLflow URL, while raw KPIs and their existing checksum remain unchanged.

The existing model identity checks are unchanged: new KPI `served_model` labels
identify the exact served checkpoint, alongside declared base model/quantization.
Historical base-only labels do not establish a different quantized checkpoint.
For manifest-bound parent imports, legacy `product_version` is preserved as raw
descriptive metadata, not compared with an engine version. Forge PR #322 at
`768a2858` derives that label from the first recipe platform even when another
platform manifest is selected. The selected tested manifest determines run
platform attribution; unbound legacy tagged imports retain their version check.

### Existing tagged execution contract

Legacy tagged intake remains supported. The parent/benchmark run must be
`FINISHED`. Parent tags are inherited; child tags override them. Its tags are:

| Tag | Meaning |
| --- | --- |
| `playbooks.recipe_path` | Canonical `models/<model>/recipes/<recipe>/recipe.yaml` |
| `playbooks.tested_revision` | Exact 40-character PR head SHA tested by Forge |
| `playbooks.pr_number` | Source recipe PR number |
| `playbooks.hardware_profile` | Exact root-relative profile path |
| `playbooks.hardware_profile_revision` | Profile revision at execution |
| `playbooks.platform_stack` | Recipe platform stack, not the host OS/platform |
| `playbooks.platform_version` | Exact recipe platform version |
| `playbooks.model_revision` | Declared model revision tested: `main`, tag, branch, or commit hash |
| `playbooks.image_digest` | Resolved `sha256:…` image digest |
| `playbooks.command` | Exact reproducible harness/Forge invocation, without secrets |

The model repository is the tested recipe's `serving.model`. Its declared
revision is recorded exactly as supplied, without resolving it to a commit hash.
Tagged intake supplies `main` explicitly for the default revision. Tagless
execution intake uses the serving arguments' default when omitted. Identifiers must be
nonempty, contain no whitespace or control characters, and pass the shared
artifact privacy guard. A Hugging Face repository with its declared revision
is sufficient model identity; a model commit pin is not required.

Optional `playbooks.prow_job_id` and `playbooks.prow_job_url` add execution
links. `playbooks.expected_concurrencies` is a JSON object, for example
`{"guidellm": [1,4,16,32,64,128], "aiperf": [1,4,16]}`. A result is `full-sweep`
only when its points exactly match the explicit intended levels. Otherwise
it is `partial`; ≤2 points, `ci-quick`, and smoke workload keys are `smoke`.

The real PR #34 execution tested recipe SHA `eecf4808…`, not its final PR head
`e4fddc2…`. It is ineligible for import, regardless of tag availability or the
separate Forge SHA. Its fixture is usable for normalizer tests, **not automatic
publication**. Its JSON formatting is
compact, but all source values, labels, and unknown KPIs are retained.
Names/GPU counts are never guessed from paths, cluster aliases, or KPI labels.
KPI model/version labels, when present, must agree with the selected
model/vLLM platform.

Recipe, model, profile, overrides, pinned manifests, and config inputs must
match the tested Git snapshot. Only additions to `benchmark_runs` are ignored
for recipe replay/idempotence. Model comparisons ignore the retired top-level
`presentation` metadata in historical snapshots; this does not restore a current
model field. Every other model field must match, including unknown fields.
Other deployment inputs retain exact Git comparisons, and receipt admission
still requires clean, committed inputs. Updating an existing immutable run with
different content is rejected. Distinct workloads within one harness artifact are
rejected rather than merged; use separate benchmark runs/artifacts.

## Runner and secrets

Create protected environment `forge-evidence`, with:

- Variables: `MLFLOW_TRACKING_URI`, `MLFLOW_WORKSPACE` (defaults to
  `forge-sandbox`; a fallback for links without a workspace, not a restriction
  to one workspace), optional `MLFLOW_INSECURE_TLS=true` for self-signed TLS.
  Prefer a trusted CA and leave insecure TLS disabled.
- Secrets: read-only `MLFLOW_USERNAME`, `MLFLOW_PASSWORD`. The account needs
  read access to each workspace used by selected reports; permission errors fail
  ingestion without falling back to another workspace or older report.
- Optional repository-level `FORGE_INGEST_RUNNER`: JSON runner labels with MLflow
  network access, e.g. `["self-hosted", "forge-evidence"]`. Default is `ubuntu-latest`.
  A custom runner must provide Bash, Git, and the GitHub CLI (`gh`).
- Variable `FORGE_INGEST_APP_CLIENT_ID` and secret `FORGE_INGEST_APP_PRIVATE_KEY`:
  an organization-owned GitHub App installed only on this repository, with
  Contents and Pull requests read/write (Metadata read is implicit). The
  model-coverage workflow reuses this App and additionally requires
  organization Projects write and repository Issues write. No webhooks
  or event subscriptions are needed. Restrict the `forge-evidence` environment
  deployment branch policy to `main` before storing the App private key.
  The workflow mints a repository-scoped installation token only for publication
  and revokes it at job completion. Discovery uses the read-only `GITHUB_TOKEN`.
  PR authorship and commit author/committer use the App's `[bot]` identity, not
  the dispatching user. App-authored PRs trigger normal PR CI.
  App credentials are required: there is no personal-token or `GITHUB_TOKEN`
  publication fallback. The old `FORGE_INGEST_GITHUB_TOKEN` is no longer used.

Dispatch the workflow from `main`; other refs are deliberately excluded.

Credentials are read from the environment, never CLI arguments or output.
HTTP requests include `X-MLflow-Workspace`, have timeouts but no artifact byte-size cap, and
reject redirects to avoid credential leakage. The bot workflow checks out
trusted `main`, never executes code from the recipe PR head, and uses unique
import branches so unrelated pending imports are not overwritten.

## Commit signatures and PR verification (issue #100)

**Status: implementation is under review in the same PR as this page; the live
acceptance matrix below is not yet complete, and issue #100 stays open until it
is recorded.** Do not cite this section as proof that evidence publication is
signed until every row has passed on the live repository. The change adds
signature coverage; it does not weaken an existing signature requirement.

### Observed gap in PR #99

The initial bot commit `9d1d95296ba9c2eab7f4602b60fe5ea61a942f21` on PR #99
reported `verified=false` with reason `unsigned`. The later maintainer follow-up
`c9168c9c4e3b55ac0781166bc6bd9a20aca69b5e` reported `verified=true` with a valid
SSH signature. PR #99 merged on 2026-11-02T16:42:46Z as squash commit
`4fdfc3abd09bc0dd8a4ed6b763324bdfbd5b02bc`.

The post-merge workflow supplied `author` and `committer` to
`create-pull-request` but omitted `sign-commits` (which defaults to false).
Those identity inputs do not sign commits. The maintainer's SSH signature
was valid; the initial bot commit was the unsigned entry in that PR's history.

This shows that checking only the PR head is insufficient: the head was signed,
while the earlier bot commit was not. The squash-merge signature is evaluated
separately and is not evidence about the PR's head commits. GitHub's
[rule suite 4861109655](https://api.github.com/repos/openshift-psap/inference-playbooks/rulesets/rule-suites/4861109655)
reports `pass` for the active `required_signatures` and `pull_request` rules on
the verified squash commit, rather than a bypass. Its tree matches the final
PR tree, preserving both the evidence and the maintainer fix.

The existing open-PR publisher is independently confirmed: [run 41051697419](https://github.com/openshift-psap/inference-playbooks/actions/runs/41051697419)
published `4254fc090d3a308b39edd2d95750c667d52b343d` to PR #19, with GitHub
`verified=true`, reason `valid`. PR #19 nevertheless contains an older unsigned
contributor commit (`08e193196d7fcaafd0e789a23023da75de21b5a0`); signing a new
import does not repair earlier history.

### Publication contract

- **Post-merge path** (`benchmark-ingest.yml`): use trusted
  `tools/post_merge_evidence.py` to prepare from the existing evidence branch
  (or the trusted main revision for a new branch), then append an App-authenticated
  `createCommitOnBranch` commit with an atomic expected-head guard. Only evidence
  paths are published; maintainer changes remain in the branch history. An
  identical import creates no commit. A concurrent branch change fails rather
  than being overwritten. Repeating an import after its PR is closed or merged
  does not reopen it; new evidence for that branch requires a fresh dispatch
  from current `main`.
- **Open-PR path** (`benchmark-import-pr.yml`): keep the existing
  `createCommitOnBranch` GraphQL publication. GitHub already signs that commit
  and `expectedHeadOid` still provides the atomic head guard; it must not be
  replaced with an unsigned push.
- **Patch delivery** (fork or oversized evidence): the contributor applies and
  commits the patch in their own environment. The resulting commit is signed
  only if the contributor signs it; it is not treated as a bot publication and
  is not exempt from the PR signature check.

The former `create-pull-request` action supports `sign-commits: true`, but its
update path rebuilds and force-updates the automation branch. That can discard
maintainer fixes on repeat imports. The append-only publisher addresses both
signing and preservation; it does not reattribute existing maintainer commits.

A commit-message trailer such as `Signed-off-by` or `Co-authored-by`, an
artifact, or a workflow summary is not a cryptographic signature and is never
accepted as one. The only accepted evidence is GitHub's commit verification
reporting `verified: true` for a valid key. There is no allow-unsigned override,
skip label, or fallback that treats an unverified commit as acceptable.

### Verification tool contract

Use `tools/verify_pr_signatures.py`:

```sh
python3 tools/verify_pr_signatures.py --repository OWNER/REPO --pr-number N
```

It is read-only and uses a read-only token (`contents: read`, plus
`pull-requests: read` for PR lookup). It lists **all** commits on the PR, follows
pagination, and fails if the API reports a truncated or incomplete list rather
than silently auditing a subset. For each commit it requires GitHub verification
`verified: true` with a valid-signature reason and reports the SHA, author, and
reason. Any unsigned, unverified, unknown, or missing result fails the run with a
nonzero exit status; API, permission, and pagination errors also fail closed.
The tool never commits, pushes, approves, or merges, and it has no bypass flag.
Verification runs after publication, including no-op repeats with an existing
PR. A failure reports the problematic history; it does not roll back or erase
the already-published evidence.

`commit-signatures.yml` wires this check into automatic PR verification on open,
`synchronize`, reopen, ready-for-review, and edit events, so every push re-audits the full
commit list. That workflow runs tooling from the trusted default branch with a
read-only token and never checks out or executes PR code. It would have reported
#99's unsigned commit after the maintainer follow-up. This adds a visible CI
check for all PRs; it does not change required checks or repository protection
settings. An outdated event head or a head/base change during verification fails
rather than reporting success for a different history.

### Live acceptance matrix

Each row must be exercised on the live repository and recorded with the
workflow run URL, resulting commit SHA, and GitHub verification result in the
PR or evidence artifact. Status is **pending** for every row until then.

| Path | Scenario | Expected | Status |
| --- | --- | --- | --- |
| Post-merge | Initial evidence PR | Bot commit `verified: true` | Pending live validation |
| Post-merge | Different selected execution | New import commit signed; prior valid evidence preserved | Pending live validation |
| Post-merge | Repeat of an admitted execution | No new commit; verification still passes | Pending live validation |
| Post-merge | Maintainer signed follow-up on the evidence PR | Follow-up `verified: true`; full history passes | Pending live validation |
| Post-merge | Automation update after maintainer fix | Fix preserved; every resulting commit verified | Pending live validation |
| Open PR (`createCommitOnBranch`) | Initial publication | Commit `verified: true`, head guard holds | Pending live validation |
| Open PR (`createCommitOnBranch`) | Replacement | Replacement commit signed; atomic head guard holds | Pending live validation |
| Open PR (`createCommitOnBranch`) | Repeat / no-op | No commit created; verification passes | Pending live validation |
| Open PR (`createCommitOnBranch`) | Maintainer signed follow-up on the PR | Follow-up `verified: true`; full history passes | Pending live validation |
| Open PR (`createCommitOnBranch`) | Automation update after maintainer fix | Fix preserved; every resulting commit verified | Pending live validation |

### Rollout and remediation

For maintainer follow-ups, configure a GPG or SSH signing key and register it
as a signing key on GitHub, then use `git commit -S` (or repository-local
`git config commit.gpgsign true`). After pushing, run the verifier above;
local signing success alone does not establish GitHub verification. See
[GitHub's signing guide](https://docs.github.com/en/authentication/managing-commit-signature-verification/signing-commits).

Both publication workflows are restricted to `main` (and `forge-evidence` is
restricted to the `main` branch policy), so this fix cannot be live-tested from a
PR branch. A check on this PR is not acceptance evidence for the new gate
either; merge the reviewed tooling first, then run the matrix on disposable or
active evidence PRs. Do not publish evidence to `main`. A no-op scenario must
show that no commit was created, not merely that verification did not run.

Remediation for an open PR that already contains an unsigned commit preserves
its evidence and context:

- keep the PR, artifacts, run IDs, and tested revision;
- adding a signed follow-up does **not** repair an unsigned ancestor. Preserve
  the old head in a backup ref, record the affected SHAs in the PR, and coordinate
  with contributors before replacing history;
- in a separate local branch, replay/re-sign the affected commits with a
  GitHub-registered signing key, preserving original authorship. Compare the
  final tree to the saved head (`git diff OLD_HEAD NEW_HEAD` must be empty),
  including evidence checksums and maintainer fixes;
- update the same PR branch only after explicit maintainer approval, using a
  lease against the recorded old head. Keep the PR discussion and explain SHA
  replacements; reviews may need renewal. If history replacement is prohibited,
  create a signed replacement PR and cross-link both PRs instead;
- re-run the full-history verifier and required checks before merge;
- do not suppress or waive the finding while remediation is pending.

PR #99 is already merged; its finding is recorded here for the audit trail and
is not rewritten retroactively. Future imports must use the signed paths above,
and the PR verification gate stays in place even if an individual commit is
later found to have been signed by an unexpected but valid key.

## Local/offline use

```sh
python3 tools/normalize_kpis.py tests/fixtures/forge-kpis-pr34-example.json \
  --output /tmp/opencode/forge-normalized --run-id forge-example \
  --deployment-scope single-node --accelerator-key nvidia-h200-x8

# With MLflow credentials in the environment and the tested SHA available in Git:
python3 tools/fetch_forge_evidence.py --run-id <parent-or-child-run-id> \
  --pr-number 34 --tested-revision <PR-head-sha> --tracking-uri <https-uri>

# Alternatively select the latest successful bot report for a merged PR (requires gh):
GITHUB_REPOSITORY=openshift-psap/inference-playbooks \
  python3 tools/ingest_forge.py --pr-number 34

python3 -m pytest tests/test_forge_evidence.py -q
python3 tools/validate.py --current
```

Each harness receives its own `results/forge-<artifact-run-id>-<harness>/` containing
unaltered `kpis.json`, normalized `result.json`, and `run.yaml` provenance.
For consolidated parent KPIs, the artifact run ID and both existing MLflow run-ID
fields identify the parent; GuideLLM and AIPerf retain separate harness outputs.
SHA-256 and deterministic offline normalization are checked by the shared
validator in local/pre-commit/CI. Unknown KPIs stay in the raw artifact.
Missing percentiles are null, GuideLLM latency seconds become milliseconds,
and AIPerf scalar tests assemble by intended concurrency. Summary metrics use
the highest observed concurrency, explicitly recorded as `reference_concurrency`.

Raw artifacts must be sanitized **at source**: no private prompts, credentials,
hostnames/IPs. Intake rejects common sensitive fields and obvious IP/credential
strings; this is a guard, not a replacement for evidence-PR review. Cluster
labels must be public aliases. With PR2 applied, the evidence workflow validates
and checks catalog/site generation before opening the import PR. The UI displays
only referenced committed runs; smoke and partial evidence are labeled rather
than presented as full sweeps. Synthetic sweep fixtures are test-only, not
published model evidence.
