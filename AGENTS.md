# Inference Playbooks repository guide

This repository stores reproducible inference playbooks. A playbook is more
than a manifest: it is a deployable configuration with auditable benchmark
evidence and a concise generated reader view.

## Main-branch changes

Do not push directly to `main`. Changes must be reviewed and merged through a
pull request. A direct push is permitted only as a break-glass response with
explicit approval from a repository owner; record the approval and reason in
the resulting commit or incident record.

## Container images

Use fully qualified container image references; never use a short image name.
Use a maintained upstream image appropriate for the workload.

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
catalog/
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
- `manifests/` contains generated deployment artifacts. Do not hand-edit them;
  change recipe inputs in `recipe.yaml` or `config/` and run the renderer.
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
- `catalog/` is generated output and must not be hand-edited.

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
  `referencing.Registry` across 13 schema files, cross-file references, profile
  revisions, recipe layout, per-platform constraint checking, and benchmark
  provenance. Run `python3 tools/validate.py --current` locally.
- `tools/render.py` — renders Kubernetes manifests from v4 recipes. Iterates
  each platform entry, merges overrides, evaluates flag constraints, selects
  a Jinja2 template from `(platform.stack, parallelism.mode)`, and writes to
  `manifests/<stack>-<version>/`. Supports pinned manifests (verbatim copy),
  Kustomize overlays (`config_overrides: true`), and shell-safe quoting for
  LWS `sh -c` args via `shellquote` filter.
- `tools/constraints.py` — stackable flag constraint engine. Constraints in
  `schema/flag-constraints.yaml` scope by `model_type`, `platform`, and
  `parallelism` to remove or force specific vLLM flags. Evaluated
  per platform entry.
- `tools/resolve-model.py` — populates `model.yaml` from HuggingFace metadata.
  Supports gated models when `HF_TOKEN` is set.
- `tools/recipe_evidence.py` — shared YAML loading with duplicate-key detection.

## Schema files

```text
schema/
  recipe.schema.json              # v4 recipe with serving block, platforms array
  platform-overrides.schema.json  # per-platform override files
  model.schema.json               # model.yaml (schema_version: 2)
  hardware-profile.schema.json    # accelerator, topology, networking
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

Pinned manifests (`pinned_manifest` in platform entry) bypass rendering and
copy a hand-authored manifest verbatim. Use when a converter is WIP.

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

CI and pre-commit must run the same local commands. CI should calculate the
affected recipe set from the diff:

- changes below one recipe validate/render only that recipe;
- a newly added hardware profile does not fan out to existing recipes;
- a corrected hardware profile validates/renders only recipes that explicitly
  reference that profile;
- renderer/schema/validator changes validate/render every recipe;
- a documented hardware-profile correction validates; an identity change fails.

After a hardware-profile correction, render only recipes that explicitly
reference that profile. Catalog generation is a separate required aggregation
step: regenerate `catalog/` from the validated recipe set after those targeted
renders, then verify the resulting generated-file diff.

Generated-file checks must fail on drift; automation should not silently commit
changes to a contributor's branch.
