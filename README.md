# Inference Playbooks

Inference Playbooks is a curated, reproducible collection of deployment
recipes for serving large language models on OpenShift/Kubernetes with GPU
accelerators. Each playbook connects a model, serving stack, hardware
profile, workload, deployment configuration, and — when available —
auditable benchmark evidence. It helps operators select, deploy, validate,
and compare practical inference configurations rather than treat manifests
or benchmark numbers in isolation.

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
| GLM-5.2-FP8 | [h200-x8-pp2-tp8-agentx-128k-vllm](models/glm-5.2/recipes/h200-x8-pp2-tp8-agentx-128k-vllm/recipe.yaml) | vLLM v0.23.0 | PP2+TP8 (LWS) |
| GLM-5.2-FP8 | [h200-x8-pp2-tp8-agentx-128k-rhoai](models/glm-5.2/recipes/h200-x8-pp2-tp8-agentx-128k-rhoai/recipe.yaml) | RHOAI 3.5 | PP2+TP8 (LLMI) |
| Qwen3-235B-A22B | [h200-x8-pp2-tp8-agentx-128k](models/qwen3-235b-a22b/recipes/h200-x8-pp2-tp8-agentx-128k/recipe.yaml) | RHOAI 3.5 | PP2+TP8 (LLMI) |
| GLM-5 / GLM-5-FP8 | — | — | [Model Ops](models/glm-5/model-ops/) only |

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
