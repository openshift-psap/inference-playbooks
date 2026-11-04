# Inference Playbooks repository guide

This repository stores reproducible inference playbooks. A playbook is more
than a manifest: it is a deployable configuration with auditable benchmark
evidence and a concise generated reader view.

## Main-branch changes

Do not push directly to `main`. Changes must be reviewed and merged through a
pull request. A direct push is permitted only as a break-glass response with
explicit approval from a repository owner; record the approval and reason in
the resulting commit or incident record.

CI verifies the GitHub signature of every commit in a PR, not only its head or
eventual squash commit. A `Signed-off-by` trailer is not a signature. Use
`tools/verify_pr_signatures.py --repository OWNER/REPO --pr-number N` to check
published history; see the [signature workflow and remediation guide](docs/forge-benchmark-ingestion.md#verification-tool-contract).

## Container images

Use fully qualified container image references; never use a short image name.
Prefer a maintained upstream image appropriate for the workload; identify and
explain custom runtime choices.

New recipes/platforms and changed effective images require `image_usage`
(`default` or `custom`) and resolvable engine metadata. Default RHOAI runtimes
declare a variant and use the exact release mapping in `engine-versions/index.yaml`.
Standalone vLLM and defaults without a mapping need an explicit image-bound
`engine` declaration. Custom images require a non-empty `image_usage.note` and
an explicit declaration; they never inherit a default-runtime engine mapping.

An explicit `engine.image` must equal the effective image reference. Declaration
binding with reviewer verification of the supporting source is the accepted
contract; automatic container inspection is not required. Do not infer an engine
version from an image tag. Tags are accepted for recipe engine resolution;
benchmark evidence retains its separate digest/provenance requirements.

Notify the user when changing an image reference in a platform override: a
different reference discards inherited `image_usage` and `engine`. This includes
tag-to-digest changes; the tools do not establish that different strings identify
identical contents. Identify the replacement in the override, including a fresh
note and image-bound declaration for a custom image. If supporting information
is unavailable, report what is missing rather than inventing it. Base metadata
may be inherited only when the effective image reference is exactly unchanged.
See [engine-version metadata](docs/engine-versions.md) for examples and provenance.

## Repository layout

Use this hierarchy for new model playbooks:

```text
schema/
  recipe.schema.json
  platform-overrides.schema.json
  model.schema.json
  CHANGELOG.md
hardware-profiles/
  <hardware-profile>.yaml
engine-versions/
  index.yaml
models/<model-id>/
  model.yaml
  recipes/<recipe-id>/
    recipe.yaml
    platforms/
      <stack>-<version>.yaml   # per-platform override files
    raw-manifest/              # initial-release intake, when used
    config/
    manifests/
      <stack>-<version>/       # generated per platform
    guides/
    benchmarks/
    results/
benchmarks/                     # shared workloads and contributor runner
docs/
site/                           # authored catalog UI
.build/site/                    # ignored generated site/catalog bundle
tools/
.github/workflows/
```

`<recipe-id>` encodes hardware and deployment context as a prefix, such as
`h200-x8-pp2-tp8-agentx-128k` or `h200-x8-mtp-single-gpu-8k1k`. Stack,
version, hardware, and workload are no longer path segments — they are fields
in `recipe.yaml`.

Each `recipe.yaml` declares a `platforms` array with one or more entries.
Each platform entry specifies `stack`, `version`, and `overrides` (path to a
file under `platforms/`). Override files are validated against
`platform-overrides.schema.json`. A platform with no customizations uses an
empty override file.

Each `recipe.yaml` must explicitly reference the matching root-level
`hardware-profiles/<hardware-profile>.yaml`.

Recipe v4 requires `deployment.scope` to be either `single-node` or
`multi-node`.

Each recipe declares `optimization_intent` as a concise catalog label.
`latency` and `throughput` are the standard values, but a recipe creator may
use a more specific free-form intent. This is not a directory level or a claim
inferred from the path. Multiple recipes for the same workload may have the
same intent.

Each recipe declares `maturity` with one of four levels:

- `day-zero` — initial config with minimal confidence.
- `contributed` — provided by other Red Hat engineers, reviewed but not
  independently tested on our infrastructure.
- `validated` — tested on our silicon with benchmark evidence against ground
  truth. Schema requires `benchmark_runs` and `image`.
- `production` — validated and hardened for production use. Same schema
  enforcement as `validated`.

Use `day-zero` for brand-new or raw-intake recipes. Use `contributed` when
a recipe comes from a trusted internal source but has not been independently
tested on our infrastructure. Do not set `validated` or `production` without
committed benchmark evidence.

## Ownership and source of truth

For the initial release only, a contributor may open a PR containing raw YAML
or JSON manifests under a leaf's `raw-manifest/` without creating
`recipe.yaml`. The PR should identify the model, stack/version, hardware,
workload, deployment pattern, and known prerequisites; uncertain details may
be called out for review. Thibrahi or Saketh converts the submission in the
same PR before merge. CI checks raw syntax and duplicate keys on submission,
then requires a sibling `recipe.yaml` and full validation before merge. Do not
require raw-only contributors to author notes, benchmark results, or generated
files. This exception ends after the initial release.

Do not submit Kubernetes `Service` or `ServiceList` objects. This includes raw
intake and preserved submissions, `config/`, pinned manifests, generated outputs,
multi-document bundles, and `List.items`, regardless of filename. Services
created by controllers at runtime are not submitted artifacts. See
[raw submission rules](CONTRIBUTING.md#what-to-submit).

- `models/<model-id>/model.yaml` owns model identity and model-wide metadata:
  family, parameter count, Hugging Face identifier, license/access
  requirements, and available quantizations.
- `hardware-profiles/<hardware-profile>.yaml` owns hardware and
  applicable network/interconnect facts. Keep GPU model/count/memory,
  topology, host requirements, and any RDMA/RoCE/DRA/SR-IOV configuration
  together.
- `recipe.yaml` owns the workload-specific serving configuration via the
  `serving` block (image, model, parallelism, args, env, resources, port),
  multi-platform targeting via `platforms`, and references to
  manifests/benchmark runs.
- `platforms/` contains per-platform override files referenced by
  `platforms[].overrides`. Override merge: image/resources/router replace,
  env appends, args merge by flag. Templates derive the K8s component kind
  from `(platform.stack, parallelism.mode)`.
- `config/` contains optional Kustomize overlay patches when
  `serving.config_overrides` is true.
- `raw-manifest/` preserves the initial submitted inputs for maintainer
  conversion. It may contain multi-document YAML or JSON and an optional
  README. After conversion, `config/` and `recipe.yaml` are authoritative.
- `manifests/` contains renderer-owned outputs and explicitly declared hand-authored
  inputs. Do not hand-edit renderer-owned outputs; change `recipe.yaml`,
  `platforms/`, or `config/` and run the renderer. A declared `pinned_manifest`
  (with `pinned_reason`) or `deployment.manifests[]` entry with `generated: false`
  identifies a hand-authored exception. Do not add exceptions merely to hide drift.
- `benchmarks/` contains reproducible harness inputs, workload definitions, and
  trace references. `results/` contains sanitized raw run artifacts and their
  parser-generated normalized results.
- `guides/` contains explanatory prose. It may embed generated tables, but it
  must link to the underlying recipe, manifest, and evidence rather than copy
  mutable values. Optional `guides/notes.yaml` holds catalog presentation,
  decision rationale, intentional omissions, image-choice status, feature
  claims, quickstart steps, insights, known issues, and sizing pointers. Recipe
  `notes` references it. Display specs point to source fields rather than copy
  mutable values. A manifest import may leave rationale fields empty; do not
  invent explanations or evidence.
- `site/` owns the catalog UI. `tools/build-site.py` generates the ignored
  `.build/site/` bundle, including `catalog.json` and build provenance. Do not
  hand-edit or commit that output, or recreate the obsolete `catalog/catalog.json`.

## Immutable hardware profiles

Hardware-profile files are stable but corrigible. Each profile contains a
`profile_revision` and a correction log. Corrections may fix factual errors or
add missing stable detail, but must increment `profile_revision` and explain
the change. A different accelerator type or count requires a new profile file
because it changes the `accelerator_key`.

Every benchmark run records the referenced profile path and the profile revision
known when it ran. Git history preserves the prior profile contents when later
corrections add missing facts.

Network configuration remains part of the hardware profile until the project
has enough independently validated network variants to justify a separate
profile type. Do not infer hardware or network properties from directory names,
hostnames, or log text.

Dependency direction is one-way: recipes and benchmark runs reference a
hardware profile; hardware profiles must not reference recipes, result runs,
or their documentation. This prevents circular evidence dependencies.

Every hardware profile provides an `accelerator_key` derived only from
accelerator vendor, type, and count (for example `nvidia-h200-x8`). Catalogs
use this key to compare/select runs that use the same accelerators but differ
in CPU, memory, provider, storage, or network configuration. Do not use the
host-specific profile ID for that comparison.

## Benchmark evidence

Every published benchmark metric must be traceable to a committed result run.
A result run records:

- harness configuration and exact command;
- workload definition and sanitized trace/prompt provenance;
- model revision and image digest;
- stack, driver, CUDA, storage, and relevant environment details;
- raw harness output, checksums, and a parser-generated normalized result.

Each run record points to its normalized `result.json`; the result points back
to exactly one run ID. Validation rejects duplicate run IDs, unreferenced
results, and references that escape a recipe or run directory.

Raw output is authoritative. Parsers/normalizers must be standalone local tools
that run in pre-commit and CI; README/catalog summaries are generated from the
normalized result and must never be hand-copied. Preserve explicit units and
missing values. Do not fabricate a metric from a log or silently drop an
unknown field.

Do not commit credentials, private prompts, hostnames/IPs, model weights, or
large sensitive logs. For essential large artifacts, commit their provenance
and immutable checksum plus a durable external location.

## Tooling

- `tools/validate.py` — validates JSON schemas (draft 2020-12) with a shared
  `referencing.Registry` across the schema files, cross-file references, profile
  revisions, recipe layout, per-platform constraint checking, and benchmark
  provenance. `--current` is a baseline-free check; use staged/diff validation
  before submission as described below.
- `tools/render.py` — renders Kubernetes manifests from v4 recipes. Iterates
  each platform entry, merges overrides, evaluates flag constraints, selects
  a Jinja2 template from `(platform.stack, parallelism.mode)`, and writes to
  `manifests/<stack>-<version>/`. Supports eligible pinned manifests (verbatim copy),
  Kustomize overlays (`config_overrides: true`), and shell-safe quoting for
  LWS `sh -c` args via `shellquote` filter.
- `tools/constraints.py` — stackable flag constraint engine. Constraints in
  `schema/flag-constraints.yaml` scope by `model_type`, `platform`, and
  `parallelism` to remove or force specific vLLM flags. Evaluated
  per platform entry.
- `tools/resolve-model.py` — populates `model.yaml` from HuggingFace metadata.
  Supports gated models when `HF_TOKEN` is set.
- `tools/recipe_evidence.py` — shared YAML loading with duplicate-key detection.
- `tools/check_manifests.py` — checks generated manifest drift using isolated
  rendering; it does not rewrite contributor files.
- `tools/engine_versions.py` — resolves effective image/engine identification
  after overrides against declarations and the maintained release index.
- `tools/catalog.py` / `tools/build-site.py` — generate a catalog preview / the
  complete site bundle. Local builds do not publish the site.

## Schema files

```text
schema/
  recipe.schema.json              # v4 recipe with serving block, platforms array
  platform-overrides.schema.json  # per-platform override files
  model.schema.json               # model.yaml (schema_version: 2)
  hardware-profile.schema.json    # accelerator, topology, networking
  engine-index.schema.json        # maintained default-runtime release mappings
  engine-version.schema.json      # image-bound engine declaration
  flag-constraints.schema.json    # stackable flag constraint rules
  benchmark-run.schema.json       # benchmark run provenance
  benchmark-result.schema.json    # normalized benchmark results
  recipe-notes.schema.json        # guides/notes.yaml structure
  container-runtime.schema.json   # container spec definitions
  deployment.schema.json          # deployment component refs
  llmisvc.schema.json             # LLMInferenceService component
  leaderworkerset.schema.json     # LeaderWorkerSet component
  llmd-router.schema.json         # llm-d router component
  flag-constraints.yaml           # constraint rules (data, not schema)
```

## Template rendering

Templates live in `templates/<stack>/`. Current template map:

| Stack | Mode | Template | K8s Kind |
|-------|------|----------|----------|
| vllm | tp, dp, tp+dp | `vllm/deployment.yaml.j2` | Deployment |
| vllm | pp, tp+pp | `vllm/lws.yaml.j2` | LeaderWorkerSet |
| rhoai | tp | `rhoai/llmisvc.yaml.j2` | LLMInferenceService |
| rhoai | pp, tp+pp | `rhoai/llmisvc-pp.yaml.j2` | LLMInferenceService |

Templates use `shellquote` (not `tojson`) for args in LWS templates where
`command: ["sh", "-c"]` requires shell-safe quoting. PP/TP+PP templates
support separate `leader_args` and `worker_args` via role blocks.

Pinned manifests (`pinned_manifest` in a platform entry, with `pinned_reason`)
bypass template generation and copy a hand-authored manifest verbatim. They do
not bypass manifest policy or earlier renderer checks. Currently, nonempty base
`serving.prefill` or a truthy base `serving.router.strategy` is rejected before
pin handling; notify the user when a requested configuration hits this limit.
A pin already at its output path is authoritative, not independently reproduced
by the drift checker. See [manifest drift](docs/manifest-drift.md).

## Recipe versions

- **v4** (current): flat layout, `serving` block, `platforms` array,
  template-driven rendering, flag constraints, per-platform overrides.
- **v3** (migration required): `schema_version: 3` with
  `deployment.components` referencing hand-authored manifests. No longer
  validates. Must be migrated to v4.

## Validation and generated files

For raw intake, local `tools/validate.py --current` checks syntax and layout
without requiring a recipe. CI uses `--require-converted-raw`, so a raw-only PR
cannot merge until maintainer conversion and normal recipe validation pass.
Raw-only leaves are not rendered; once `recipe.yaml` is added, normal
affected-recipe selection applies.

Use the local tools that back CI and pre-commit. `tools/validate.py` scans the
repository's recipes and evidence; affected-recipe selection scopes the separate
manifest checks, not all validation. Selection rules are:

- changes below one recipe select that recipe;
- model metadata changes select that model's recipes;
- a newly added hardware profile does not fan out to existing recipes;
- a corrected hardware profile selects only recipes that explicitly
  reference that profile;
- tools, templates, schemas, engine index, workflow, or pre-commit changes select
  every recipe;
- a documented hardware-profile correction validates; an identity change fails.

`--current` skips historical profile-correction and additions-only file-flow
checks, and may only warn for unclassified legacy images. Diff validation
requires resolvable image metadata for new recipes/platforms and changed images;
malformed or conflicting declarations still fail without a baseline.

For staged changes, run:

```sh
python3 tools/validate.py --base HEAD --cached --require-converted-raw
python3 tools/check_manifests.py --base HEAD --cached
```

For committed changes, use `--base <base-sha> --head <head-sha>` with both tools.
CI uses `tools/ci_baseline.py` to select the tested PR merge's actual base parent.
The pre-commit validator allows raw-only intake; the conversion-required command
above also checks the CI merge requirement. Install `tools/requirements.txt` and
Kustomize v5.6.0 for overlays; see [manifest checks](docs/manifest-drift.md).

After a hardware-profile correction, regenerate only its referencing recipes'
outputs and inspect their diffs. Publication is a separate full-repository gate:
`python3 tools/build-site.py` validates sources, checks all manifests, and builds
`.build/site/`. For catalog/UI changes, also run the catalog/browser checks in
[the local preview guide](docs/catalog-contract.md#local-preview). For contributor
runner changes, use [its developer verification](docs/contributor-benchmarks.md#developer-verification),
including the applicable image scan and container smoke checks.

File-flow warnings in diff/staged validation and the maintained PR comment are
advisory ownership checks, not validation failures. Explain the warning to the
user and determine whether the file is obsolete, a declared recipe input/output,
or an intentionally shared helper needing agreed validation ownership. Do not
move files simply to silence a warning; a preparation Job does not automatically
need its own serving recipe. See [file-flow remediation](docs/recipe-evidence.md#resolving-a-file-flow-warning).

Generated-file checks must fail on drift; automation should not silently commit
changes to a contributor's branch.
