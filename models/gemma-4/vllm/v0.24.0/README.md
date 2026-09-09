# Gemma 4 26B-A4B FP8 on H200 — Deployment Guides

Validated deployment pattern for
[RedHatAI/gemma-4-26B-A4B-it-FP8-dynamic](https://huggingface.co/RedHatAI/gemma-4-26B-A4B-it-FP8-dynamic)
on NVIDIA H200, with vLLM. The manifest is what we actually ran —
config, MTP speculative decoding, and probes.

| # | Pattern | Manifest | Workload | Notes |
|---|---------|----------|----------|-------|
| 1 | Single node, TP=1 + MTP | [Deployment](single-node/manifests/gemma-4-26b-a4b-it-fp8-deployment.yaml) | General serving | MTP draft from `google/gemma-4-26B-A4B-it-assistant` (3 tokens); prefix caching on; 1 GPU |

## Which One?

- Model fits on a single GPU and you want a straightforward serving path → **1**.

## Requirements

- NVIDIA H200 node with GPU operator (Kubernetes or OpenShift)
- vLLM ≥ v0.24.0 (image `vllm/vllm-openai:v0.24.0`)
- HuggingFace token secret `hf-token` (key `HF_TOKEN`). Gemma is a **gated**
  repo — the same token must also cover the gated `-assistant` draft repo:

  ```
  kubectl create secret generic hf-token --from-literal=HF_TOKEN=hf_xxx
  ```

The manifest lives in
[`single-node/manifests/`](single-node/manifests/).
