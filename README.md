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
- **version** — framework version (`v0.23.0`, `latest`)
- **topology** — deployment shape (`single-node`, `multi-node-lws`)
- **model-ops/** — framework-agnostic download/sync jobs, at model level

## Models

| Model | Framework | Versions | Start Here |
|-------|-----------|----------|------------|
| GLM-5.2-FP8 | vLLM | [v0.23.0](models/glm-5.2/vllm/v0.23.0/) (production), [latest](models/glm-5.2/vllm/latest/) (benchmarks) | [Deployment Guides](models/glm-5.2/vllm/v0.23.0/README.md) |
| GLM-5 / GLM-5-FP8 | vLLM | [latest](models/glm-5/vllm/latest/) | [Model Ops](models/glm-5/model-ops/) |

## Planned

- **llm-d** — prefix-cache-aware routing, P/D disaggregation, KV tiering
- **RHOAI** — Red Hat OpenShift AI serving stack (KServe + ServingRuntime)

Follow the same `<version>/<topology>/` convention when adding content under these frameworks.
