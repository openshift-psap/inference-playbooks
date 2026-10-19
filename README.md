# Inference Playbooks

## Leadership staging snapshot

This branch, **`staging/all-pr-manifests`**, combines all 11 open PR heads
captured on 2026-10-19, including draft submissions. It is an integration
preview, not a production release or evidence that these configurations have
been independently tested. `main` and the published catalog are unchanged.

### Included recipe and manifest submissions

| PR | Submission | Browse the configuration |
|----|------------|--------------------------|
| [#3](https://github.com/openshift-psap/inference-playbooks/pull/3) | GLM-5.2 B200, RHOAI 3.4 | [Original manifest](models/glm-5.2/rhoai/3.4/aggregated-b200/manifests/) |
| [#4](https://github.com/openshift-psap/inference-playbooks/pull/4) | GLM-5.2 B200, RHOAI 3.4 / 3.5 | [Recipe and manifests](models/glm-5.2/recipes/b200-x8-tp8-ep-mtp3-guidellm-8k1k/) |
| [#10](https://github.com/openshift-psap/inference-playbooks/pull/10) | GLM-5.2 H200, TP8 + MTP | [Recipe and manifests](models/glm-5.2/recipes/h200-x8-tp8-mtp-agentic-131k/) |
| [#19](https://github.com/openshift-psap/inference-playbooks/pull/19) | Kimi K3 H200, TP8 × PP2 | [Recipe and manifests](models/kimi-k3/recipes/h200-x8-pp2-tp8-8k1k/) |
| [#23](https://github.com/openshift-psap/inference-playbooks/pull/23) | Nemotron-3-Ultra-550B H200, multi-node P/D with two LeaderWorkerSets | [v4 recipe and pinned bundle](models/nemotron-3-ultra-550b/recipes/h200-x32-pd-3p1d-tp8-agentx-128k/) |
| [#33](https://github.com/openshift-psap/inference-playbooks/pull/33) | GLM-5.3 H100 ×64, multi-node P/D DisaggregatedSet with LWS roles (draft) | [v4 recipe and pinned bundle](models/glm-5.3/recipes/h100-x64-pd-pcp8dcp8-ep32-dp16tp2-agentx-275k/) |
| [#35](https://github.com/openshift-psap/inference-playbooks/pull/35) | Gemma 4 B200, base / NVFP4 (draft) | [Base recipe](models/gemma-4/recipes/b200-x8-base-single-gpu-8k1k/) · [NVFP4 recipe](models/gemma-4/recipes/b200-x8-nvfp4-single-gpu-8k1k/) |
| [#40](https://github.com/openshift-psap/inference-playbooks/pull/40) | GLM-5.3 H200, aggregated DP8/EP8 + HiSparse | [Recipe and manifests](models/glm-5.3/recipes/h200-x8-dep8-mtp3-hisparse-agentx-128k/) |
| [#43](https://github.com/openshift-psap/inference-playbooks/pull/43) | GLM-5.3 H200, prefill/decode (draft) | [Recipe and pinned manifest](models/glm-5.3/recipes/h200-x16-pd-pcp8dcp8-dep8-mtp3-offload-agentx-128k/) |

Also merged: [#44](https://github.com/openshift-psap/inference-playbooks/pull/44)
(single-node companion and scope-aware renderer support) and
[#9](https://github.com/openshift-psap/inference-playbooks/pull/9)
(CI experiment marker; no recipe).

### Preview status and limitations

- Browse the configurations above directly on this branch. The catalog link
  below still points to the published **main** site, not this snapshot.
- PRs #33 and #23 now have v4 recipes with reproducible manual-pin fallbacks
  for unsupported P/D templates. Deployment pattern, multi-node scope,
  asymmetric topology and required controllers are documented; original
  raw/legacy inputs remain available for provenance.
- The B200 GLM-5.2 recipe has an explicit, independently unverified vLLM
  Deployment target instead of an automatically inferred RHOAI companion.
- Strict raw-conversion validation, manifest drift checks and the local
  catalog build pass. The H100 contribution still requires access to its
  private image; its bundled vLLM version is unknown and shown as unresolved.
- No validation requirements were disabled, benchmark numbers fabricated, or
  maturity levels promoted for this preview. Benchmark/UI completeness is
  not a condition for including submitted manifests in this snapshot.

Build and browse the staging catalog locally (this does not publish Pages):

```bash
python3 tools/build-site.py --out .build/staging-site
python3 -m http.server 8000 --directory .build/staging-site
```

---

Inference Playbooks is a curated, reproducible collection of deployment
recipes for serving large language models on OpenShift/Kubernetes with GPU
accelerators. Each playbook connects a model, serving stack, hardware
profile, workload, deployment configuration, and — when available —
auditable benchmark evidence. It helps operators select, deploy, validate,
and compare practical inference configurations rather than treat manifests
or benchmark numbers in isolation.

**[Browse the interactive recipe catalog](https://openshift-psap.github.io/inference-playbooks/)**
to explore deployment configurations and available benchmark evidence.

## Supported Accelerators

- NVIDIA B200
- NVIDIA H200
- NVIDIA H100

## Target Workloads

Reusable benchmark Jobs live in
[`benchmarks/manifests/`](benchmarks/manifests/). Set the `ENDPOINT`,
`MODEL`, and `TOKENIZER` environment values in each manifest before
applying it in the same namespace as the target Service.

- [GuideLLM 8K/1K](benchmarks/manifests/guidellm-8k1k-job.yaml) —
  synthetic 8 000-input / 1 000-output requests at concurrent streams
  1, 4, 16, 32, 64, and 128.
- [AIPerf AgentX 128K](benchmarks/manifests/aiperf-agentx-128k-job.yaml)
  — long-context, multi-turn agentic trace replay filtered to
  128K contexts.
- [AIPerf AgentX unlimited context][agentx-unlimited] — the same replay
  without context filtering; requires an endpoint that can accept every
  request in the trace corpus.

[agentx-unlimited]: benchmarks/manifests/aiperf-agentx-unlimited-context-job.yaml

Model playbooks may add benchmark manifests beside a topology only when
the workload is specific to that model, framework, or deployment shape.

## Models

| Model | Recipe | Platforms | Mode |
|-------|--------|-----------|------|
| Gemma-4-26B-A4B-FP8 | [h200-x8-mtp-single-gpu-8k1k](models/gemma-4/recipes/h200-x8-mtp-single-gpu-8k1k/recipe.yaml) | vLLM v0.24.0 | TP (single-GPU) |
| Gemma-4-26B-A4B-FP8 | [Forge smoke recipe](models/gemma-4/vllm/v0.24.0/recipes/nvidia-h200-sxm-8x-nvlink-r1/guidellm-8k1k/tp1/recipe.yaml) | vLLM v0.24.0 | TP (single-GPU) |
| GLM-5.2-FP8 | [h200-x8-pp2-tp8-agentx-128k-vllm](models/glm-5.2/recipes/h200-x8-pp2-tp8-agentx-128k-vllm/recipe.yaml) | vLLM v0.23.0 | PP2+TP8 (LWS) |
| GLM-5.2-FP8 | [h200-x8-pp2-tp8-agentx-128k-rhoai](models/glm-5.2/recipes/h200-x8-pp2-tp8-agentx-128k-rhoai/recipe.yaml) | RHOAI 3.5 | PP2+TP8 (LLMI) |
| GLM-5.2-NVFP4-FP8 | [b200-x8-tp8-ep-mtp3-guidellm-8k1k](models/glm-5.2/recipes/b200-x8-tp8-ep-mtp3-guidellm-8k1k/guides/README.md) | RHOAI 3.5 (rendered), 3.4 (pinned) | Single-node TP8+EP+MTP3; contributed |
| Qwen3-235B-A22B | [h200-x8-pp2-tp8-agentx-128k](models/qwen3-235b-a22b/recipes/h200-x8-pp2-tp8-agentx-128k/recipe.yaml) | RHOAI 3.5 | PP2+TP8 (LLMI) |
| GLM-5 / GLM-5-FP8 | — | — | [Model Ops](models/glm-5/model-ops/) only |
| GLM-5.2-NVFP4-FP8 | [Aggregated B200](models/glm-5.2/rhoai/3.4/README.md) | RHOAI 3.4 | TP8 (raw manifest) |

## Recipe Layout

```text
hardware-profiles/
  <hardware-profile>.yaml

models/<model-id>/
  model.yaml
  recipes/<recipe-id>/
    recipe.yaml
    platforms/
      <stack>-<version>.yaml   # per-platform overrides
    config/                    # optional Kustomize overlay
    manifests/
      <stack>-<version>/       # generated per platform
    guides/
    benchmarks/
    results/
```

### Recipe v4

Recipe v4 uses a flat layout with a `serving` block and a `platforms`
array. Contributors define image, model, parallelism, and annotated
args; templates generate Kubernetes manifests per platform. Stack and
version are fields in `recipe.yaml`, not path segments.

```bash
python3 tools/render.py models/.../recipe.yaml --dry-run   # preview manifests
python3 tools/render.py models/.../recipe.yaml             # write to manifests/
```

Per-platform override files under `platforms/` customize image, args,
env, resources, or router for a specific stack version. Override merge:
image/resources/router replace, env appends, args merge by flag.

See [CONTRIBUTING.md](CONTRIBUTING.md) and
[schema/examples/recipe-v4-example.yaml](schema/examples/recipe-v4-example.yaml).

### Recipe v3 (migration required)

The v3 schema (`schema_version: 3`, `deployment.components`) is no
longer supported. Recipes must be migrated to v4 before they can pass
validation. Pre-schema content (guides, manifests) under old
`models/<model-id>/<stack>/<version>/` paths is retained for reference.

### Key conventions

- `recipe_id` encodes hardware and deployment context (e.g.,
  `h200-x8-pp2-tp8-agentx-128k`). Stack, version, hardware profile,
  and workload profile are recipe fields, not path segments.
- `recipe.yaml` explicitly names its root-level hardware profile.
- `deployment.scope` is always `single-node` or `multi-node`.
  `optimization_intent` is catalog metadata, not a path level.
- Optional `notes: guides/notes.yaml` links reader-facing context and
  reasons for deployment choices; see the
  [notes example](schema/examples/recipe-notes.yaml).

### Day-zero examples

- [Gemma-4 single-GPU TP](models/gemma-4/recipes/h200-x8-mtp-single-gpu-8k1k/recipe.yaml) — vLLM Deployment
- [GLM-5.2 PP2+TP8 RHOAI](models/glm-5.2/recipes/h200-x8-pp2-tp8-agentx-128k-rhoai/recipe.yaml) — LLMInferenceService
- [GLM-5.2 PP2+TP8 vLLM](models/glm-5.2/recipes/h200-x8-pp2-tp8-agentx-128k-vllm/recipe.yaml) — LeaderWorkerSet
- [Qwen3-235B PP2+TP8](models/qwen3-235b-a22b/recipes/h200-x8-pp2-tp8-agentx-128k/recipe.yaml) — RHOAI multi-platform
- [v4 schema example](schema/examples/recipe-v4-example.yaml) — annotated template

See the [contribution guide](CONTRIBUTING.md) for the full flow.

### Hardware profiles

Hardware profiles contain accelerator, host-topology, and applicable
network facts together. They are stable but corrigible: a factual
correction increments `profile_revision` and adds a correction-log entry.
Changing accelerator type or count requires a new profile, while
`accelerator_key` supports comparisons across different hosts with the
same accelerator type/count.

See [the recipe-evidence guide](docs/recipe-evidence.md) for evidence
and profile rules, [the recipe contribution guide](CONTRIBUTING.md)
for the creation and review flow, and [AGENTS.md](AGENTS.md) for the
full contributor contract.

## Validation

Install the local validation dependencies and validate repository
content:

```bash
python3 -m pip install -r tools/requirements.txt
python3 tools/validate.py --current
```

CI validates profile corrections and calculates only the recipes
affected by a change. A corrected hardware profile selects just recipes
that explicitly reference it; schema, validator, renderer, or workflow
changes select all recipes.

## Current Status

- Recipe v4 schema with flat layout, multi-platform rendering, serving
  block, role blocks, flag constraints, pinned manifests, and per-platform
  override merging.
- Template renderer (`tools/render.py`) with Jinja2 templates for vLLM
  (Deployment, LeaderWorkerSet) and RHOAI (LLMInferenceService).
- Validator (`tools/validate.py`) with schema validation, per-platform
  constraint checking, layout validation, and benchmark evidence checks.
- Four day-zero v4 recipes across Gemma-4, GLM-5.2, and Qwen3-235B.
- Immutable hardware profiles, evidence validation, affected-recipe
  selection, and reusable benchmark workloads.

## Planned

- **llm-d templates** — prefix-cache-aware routing, P/D disaggregation
- **Template consolidation** — reduce per-platform × per-mode template
  proliferation ([#26](https://github.com/openshift-psap/inference-playbooks/issues/26))
- **EP/wide-EP modes** — expert parallelism for MoE models
- **Catalog generation** — aggregate validated recipes into browsable index
- **README rendering** — generated per-recipe summaries from recipe data
