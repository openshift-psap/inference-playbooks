# Inference Playbooks

Deployment playbooks, manifests, benchmark scripts, and guides for serving large language models on OpenShift/Kubernetes with GPU accelerators.

## Current status

The repository contains existing deployment guides and manifests in the legacy
topology-based layout. New playbooks use the Recipe v3 evidence structure below.
The v3 schemas, immutable hardware profiles, evidence validator, affected-recipe
selection, and reusable benchmark workloads are now on `main`. Migrating the
existing playbooks and adding the renderer/catalog are follow-up work.

## Recipe v3 layout

```
hardware-profiles/
  <hardware-profile>.yaml
models/<model-id>/
  model.yaml
  <stack>/<stack-version>/
    model-ops/
    recipes/<hardware-profile>/<workload-profile>/<deployment-mode>[--<suffix>]/
      recipe.yaml
      manifests/
      guides/
      benchmarks/
      results/
```

- `<stack>` is a serving stack such as `vllm`, `rhoai`, or `llm-d`; the version
  is part of that stack context.
- `<workload-profile>` is one of `guidellm-8k1k`, `aiperf-agentx-128k`, or
  `aiperf-agentx-unlimited-context`.
- `<deployment-mode>` names the configuration pattern, such as
  `tp8-aggregated`, `tp8-replicas-2`, or `pp2-tp8`. Add `--<suffix>` only when
  more than one recipe shares that deployment mode.
- `recipe.yaml` explicitly names its root-level hardware profile. The path is a
  navigation selector, not a source of hardware facts.
- `deployment.scope` is always `single-node` or `multi-node`; `match.nodes` is
  retired. `optimization_intent` is catalog metadata, not a path level, and may
  be a custom concise label.

Hardware profiles contain accelerator, host-topology, and applicable network
facts together. They are stable but corrigible: a factual correction increments
`profile_revision` and adds a correction-log entry. Changing accelerator type
or count requires a new profile, while `accelerator_key` supports comparisons
across different hosts with the same accelerator type/count.

See [the recipe-evidence guide](docs/recipe-evidence.md) for evidence and
profile rules, and [AGENTS.md](AGENTS.md) for the full contributor contract.

## Validation

Install the local validation dependencies and validate repository content:

```bash
python3 -m pip install -r tools/requirements.txt
python3 tools/validate.py --current
```

CI validates profile corrections and calculates only the recipes affected by a
change. A corrected hardware profile selects just recipes that explicitly
reference it; schema, validator, renderer, or workflow changes select all
recipes. README rendering and catalog generation will be enabled when the
renderer lands.

## Common Benchmarks

Reusable benchmark Jobs live in [`benchmarks/manifests/`](benchmarks/manifests/).
Set the `ENDPOINT`, `MODEL`, and `TOKENIZER` environment values in
each manifest before applying it in the same namespace as the target Service.

- [GuideLLM 8K/1K](benchmarks/manifests/guidellm-8k1k-job.yaml) — synthetic
  8,000-input / 1,000-output requests at concurrent streams 1, 4, 16, 32, 64,
  and 128.
- [AIPerf AgentX 128K](benchmarks/manifests/aiperf-agentx-128k-job.yaml) —
  long-context, multi-turn agentic trace replay filtered to 128K contexts.
- [AIPerf AgentX unlimited context](benchmarks/manifests/aiperf-agentx-unlimited-context-job.yaml)
  — the same replay without context filtering; requires an endpoint that can
  accept every request in the trace corpus.

Model playbooks may add benchmark manifests beside a topology only when the
workload is specific to that model, framework, or deployment shape.

## Models

| Model | Framework | Versions | Start Here |
|-------|-----------|----------|------------|
| GLM-5.2-FP8 | vLLM | [v0.23.0](models/glm-5.2/vllm/v0.23.0/) | [Deployment Guides](models/glm-5.2/vllm/v0.23.0/README.md) |
| GLM-5 / GLM-5-FP8 | vLLM | [latest](models/glm-5/vllm/latest/) | [Model Ops](models/glm-5/model-ops/) |
| GLM-5.2-FP8 | RHOAI | [3.5](models/glm-5.2/rhoai/3.5/) | [Deployment Guides](models/glm-5.2/rhoai/3.5/README.md) |
| Gemma-4-26B-A4B-FP8 | vLLM | [v0.24.0](models/gemma-4/vllm/v0.24.0/) | [Deployment Guides](models/gemma-4/vllm/v0.24.0/README.md) |

## Planned

- **llm-d** — prefix-cache-aware routing, P/D disaggregation, KV tiering

Existing content under these frameworks uses the legacy `<version>/<topology>/`
layout. Add new playbooks using the Recipe v3 layout above.
