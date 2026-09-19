# GLM 5.2 NVFP4-FP8 Aggregated on B200 — RHOAI 3.4

Aggregated (single-node) `LLMInferenceService` for
[RedHatAI/GLM-5.2-NVFP4-FP8](https://huggingface.co/RedHatAI/GLM-5.2-NVFP4-FP8)
on a single **8×B200** node (TP=8, expert parallel) via KServe's
`LLMInferenceService` v1alpha2 CRD on Red Hat OpenShift AI **3.4**.

Source-faithful port of Rob Shaw's llm-d aggregated B200 recipe
([base](https://github.com/robertgshaw2-redhat/llm-d-models/blob/main/glm5.2/aggregated/base/glm.yaml),
[gke-b200](https://github.com/robertgshaw2-redhat/llm-d-models/blob/main/glm5.2/aggregated/gke-b200/kustomization.yaml)).
Runtime-validated on an internal RHOAI 3.4 cluster (2026-08-31): B200 node,
8 GPUs, TP=8; `/v1/models` and `/v1/chat/completions` returned HTTP 200.

| # | Pattern | Manifest | Workload | Notes |
|---|---------|----------|----------|-------|
| 1 | Aggregated, TP=8 + EP + MTP | [LLMInferenceService](aggregated-b200/manifests/glm-5-2-aggregated-b200-llmisvc.yaml) | General serving | Single 8×B200 node; expert parallel; MTP (3 tokens); fp8 KV cache; InstantTensor loader |

## Which One?

- One B200 node with 8 free GPUs and you want a single-node aggregated
  serving path → **1**.

## Requirements

- KServe with `LLMInferenceService` v1alpha2 CRD (RHOAI 3.4)
- One node with 8× NVIDIA B200 GPUs (`nvidia.com/gpu.product: NVIDIA-B200`)
- NVIDIA GPU operator
- HuggingFace token secret `llm-d-hf-token` (key `HF_TOKEN`):

  ```
  kubectl create secret generic llm-d-hf-token --from-literal=HF_TOKEN=hf_xxx
  ```

- PVCs `glm-5-2-hf-cache` (Hugging Face cache) and `glm-5-2-jit-cache`
  (JIT/compile cache), or replace those names in the manifest with
  equivalent OpenShift storage.

## Notes

- **`replicas: 0` by default.** Set `replicas: 1` only when one B200 node
  has 8 free GPUs.
- **InstantTensor is installed at pod startup** via an init container into
  a writable `/opt/pydeps` (with `PYTHONPATH` override). This is a runtime
  workaround that reproduces the source manifest's dependency on OpenShift
  without writing to `/usr/local`. It is **validation evidence, not the
  recommended customer path** — a customer recipe should use an image with
  InstantTensor / GLM 5.2 NVFP4-FP8 loader support already built in, then
  drop the init container and `PYTHONPATH`.
- **`uri: pvc://glm-5-2-hf-cache`** satisfies KServe's required model URI
  without launching storage-initializer; vLLM serves `MODEL` directly and
  uses this PVC as the Hugging Face cache.
- Image is upstream `docker.io/vllm/vllm-openai:v0.27.0`, matching the
  source URLs above.

See the [RHOAI 3.5 recipe](../3.5/) for the version that targets
RHOAI 3.5 `LLMInferenceServiceConfig` refs and gateway TLS.
