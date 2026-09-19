# GLM 5.2 NVFP4-FP8 Aggregated on B200 — RHOAI 3.5

Aggregated (single-node) `LLMInferenceService` for
[RedHatAI/GLM-5.2-NVFP4-FP8](https://huggingface.co/RedHatAI/GLM-5.2-NVFP4-FP8)
on a single **8×B200** node (TP=8, expert parallel) via KServe's
`LLMInferenceService` v1alpha2 CRD on Red Hat OpenShift AI **3.5**.

Uses the RHOAI 3.5 `LLMInferenceServiceConfig` refs
(`v3-5-0-kserve-config-*`) and serves over the gateway-generated
`HTTPRoute` with KServe-mounted TLS certs.

| # | Pattern | Manifest | Workload | Notes |
|---|---------|----------|----------|-------|
| 1 | Aggregated, TP=8 + EP + MTP | [LLMInferenceService](manifests/glm-5-2-aggregated-b200-llmisvc.yaml) | General serving | Single 8×B200 node; expert parallel; MTP (3 tokens); fp8 KV cache; InstantTensor loader; gateway TLS |

## Status

- Runtime-validated on an internal RHOAI 3.5 cluster (2026-09-10):
  gateway `/v1/models` and `/v1/chat/completions` returned HTTP 200
  through the generated RHOAI 3.5 `HTTPRoute`.
- Derived from the [RHOAI 3.4 recipe](../../3.4/) that passed direct
  runtime smoke on an internal cluster (2026-08-31).

Source-faithful port of Rob Shaw's llm-d aggregated B200 recipe
([base](https://github.com/robertgshaw2-redhat/llm-d-models/blob/main/glm5.2/aggregated/base/glm.yaml),
[gke-b200](https://github.com/robertgshaw2-redhat/llm-d-models/blob/main/glm5.2/aggregated/gke-b200/kustomization.yaml)).

## Requirements

- KServe with `LLMInferenceService` v1alpha2 CRD (RHOAI 3.5)
- One node with 8× NVIDIA B200 GPUs (`nvidia.com/gpu.product: NVIDIA-B200`)
- NVIDIA GPU operator
- HuggingFace token secret `llm-d-hf-token` (key `HF_TOKEN`):

  ```
  kubectl create secret generic llm-d-hf-token --from-literal=HF_TOKEN=hf_xxx
  ```

- PVCs `glm-5-2-hf-cache` (Hugging Face cache) and `glm-5-2-jit-cache`
  (JIT/compile cache), or replace those names in the manifest with
  equivalent OpenShift storage.

## Model Storage

Two cache options (the manifest ships **A**):

- **A — PVC cache (shown):** `uri: pvc://glm-5-2-hf-cache` satisfies
  KServe's required model URI without launching storage-initializer.
  vLLM serves `MODEL` directly and uses the PVC as the Hugging Face
  cache, so weights survive pod restarts and avoid repeat downloads.
- **B — HF initializer:** set `uri: hf://RedHatAI/GLM-5.2-NVFP4-FP8` and
  remove the `hf-cache` PVC volume/mount and `HF_HUB_CACHE` override to
  let the KServe storage-initializer fetch from Hugging Face instead.

## Networking: Gateway TLS

In the validated RHOAI 3.5 environment the generated workload `Service`
advertised HTTPS upstream, so the custom upstream vLLM command serves
with the mounted KServe TLS certs (`--enable-ssl-refresh`,
`--ssl-certfile /var/run/kserve/tls/tls.crt`,
`--ssl-keyfile /var/run/kserve/tls/tls.key`) and all health probes use
`scheme: HTTPS`.

## Notes

- **`replicas: 0` by default.** Set `replicas: 1` only when one B200 node
  has 8 free GPUs.
- **InstantTensor is installed at pod startup** via an init container into
  a writable `/opt/pydeps` (with `PYTHONPATH` override), because no
  prebuilt Red Hat-compatible image with the needed GLM 5.2 / InstantTensor
  support has been validated yet. This is a runtime workaround, not a
  product-native path — the preferred future path is a custom/approved
  image with InstantTensor already included, then drop the init container
  and `PYTHONPATH`.
- Image is upstream `docker.io/vllm/vllm-openai:v0.27.0`, matching the
  source URLs above.

For the PP=2 TP=8 multi-node H200 recipe on RHOAI 3.5, see the
[version README](../README.md).
