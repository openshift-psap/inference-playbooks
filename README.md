# Inference Playbooks

Deployment playbooks, manifests, benchmark scripts, and guides for serving large language models on OpenShift/Kubernetes with GPU accelerators.

## Directory Convention

```
models/<model>/<framework>/<version>/<topology>/
                                     ├── manifests/
                                     ├── guides/
                                     ├── benchmarks/
                                     └── results/
```

- **model** — model family and quantization (e.g. `glm-5.2` for GLM-5.2-FP8)
- **framework** — serving framework (`vllm`, `llm-d`, `rhoai`)
- **version** — framework version (e.g. `v0.23.0`)
- **topology** — deployment shape (`single-node`, `multi-node-lws`)
- **model-ops/** — framework-agnostic download/sync jobs, at model level

## Common Benchmarks

Reusable benchmark Jobs live in [`benchmarks/manifests/`](benchmarks/manifests/).
Set the `ENDPOINT`, `MODEL`, and (for AIPerf) `TOKENIZER` environment values in
each manifest before applying it in the same namespace as the target Service.

- [GuideLLM 8K/1K](benchmarks/manifests/guidellm-8k1k-job.yaml) — synthetic
  8,000-input / 1,000-output requests at concurrent streams 1, 4, and 16.
- [AIPerf AgentX](benchmarks/manifests/aiperf-agentx-job.yaml) — long-context,
  multi-turn agentic trace replay; requires an endpoint that accepts 128K
  contexts.

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

Follow the same `<version>/<topology>/` convention when adding content under these frameworks.
