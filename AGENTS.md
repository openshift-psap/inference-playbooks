# Inference Playbooks repository guide

This repository stores reproducible inference playbooks. A playbook is more
than a manifest: it is a deployable configuration with auditable benchmark
evidence and a concise generated reader view.

## Repository layout

Use this hierarchy for new model playbooks:

```text
schema/
  recipe.schema.json
  model.schema.json
  CHANGELOG.md
hardware-profiles/
  <hardware-profile>.yaml
models/<model-id>/
  model.yaml
  <stack>/<stack-version>/
    model-ops/
    recipes/<hardware-profile>/
      <workload-profile>/
        <deployment-mode>[--<suffix>]/
          recipe.yaml
          manifests/
          guides/
          benchmarks/
          results/
catalog/
tools/
.github/workflows/
```

`<stack>` is a serving stack such as `rhoai` or `llm-d`. `<stack-version>` is
the version the recipe targets (for example, `3.5`). Do not place RHOAI- or
llm-d-specific model operations directly under the model root: downstream
framework limitations are part of the stack/version context.

`<hardware-profile>` is a normalized, lowercase, hyphenated identifier such as
`h200-sxm8` or `mi300x-8gpu`. In a recipe path it is a navigation selector, not
the source of physical facts. Each `recipe.yaml` must explicitly reference the
matching root-level `hardware-profiles/<hardware-profile>.yaml`.
`<workload-profile>` is one of the reusable benchmark workloads from PR #7:
`guidellm-8k1k`, `aiperf-agentx-128k`, or
`aiperf-agentx-unlimited-context`. `<deployment-mode>` identifies the
configuration pattern, such as `tp8-aggregated`, `tp8-replicas-2`, or
`pp2-tp8`. A suffix is allowed only when more than one recipe shares the same
deployment mode (for example, `tp8-aggregated--prefix-cache-off`).

Each recipe declares `optimization_intent: latency` or
`optimization_intent: throughput`. This is a catalog label, not a directory
level or a claim inferred from the path. Multiple recipes for the same workload
may have the same intent.

## Ownership and source of truth

- `models/<model-id>/model.yaml` owns model identity and model-wide metadata:
  family, parameter count, Hugging Face identifier, license/access
  requirements, and available quantizations.
- `hardware-profiles/<hardware-profile>.yaml` owns hardware and
  applicable network/interconnect facts. Keep GPU model/count/memory,
  topology, host requirements, and any RDMA/RoCE/DRA/SR-IOV configuration
  together.
- `recipe.yaml` owns the workload-specific serving configuration, compatibility
  claim, references to manifests/benchmark runs, and display-safe summary.
- `manifests/` contains generated deployment artifacts. Do not hand-edit them;
  change recipe inputs and run the renderer.
- `benchmarks/` contains reproducible harness inputs, workload definitions, and
  trace references. `results/` contains sanitized raw run artifacts and their
  parser-generated normalized results.
- `guides/` contains explanatory prose. It may embed generated tables, but it
  must link to the underlying recipe, manifest, and evidence rather than copy
  mutable values.
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

Raw output is authoritative. Parsers/normalizers must be standalone local tools
that run in pre-commit and CI; README/catalog summaries are generated from the
normalized result and must never be hand-copied. Preserve explicit units and
missing values. Do not fabricate a metric from a log or silently drop an
unknown field.

Do not commit credentials, private prompts, hostnames/IPs, model weights, or
large sensitive logs. For essential large artifacts, commit their provenance
and immutable checksum plus a durable external location.

## Validation and generated files

`tools/validate.py` validates schemas, cross-file references, profile
immutability, and required benchmark provenance. `tools/render.py` renders
manifests, recipe READMEs, and catalog outputs. `tools/doctor.py` performs
configuration linting.

CI and pre-commit must run the same local commands. CI should calculate the
affected recipe set from the diff:

- changes below one deployment-mode recipe validate/render only that recipe;
- a newly added hardware profile does not fan out to existing recipes;
- a corrected hardware profile validates/renders only recipes that explicitly
  reference that profile;
- renderer/schema/validator changes validate/render every recipe;
- a documented hardware-profile correction validates; an identity change fails.

Generated-file checks must fail on drift; automation should not silently commit
changes to a contributor's branch.
