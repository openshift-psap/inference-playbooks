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

## Forge profile1 smoke

[Recipe v3](recipes/nvidia-h200-sxm-8x-nvlink-r1/guidellm-8k1k/tp1/recipe.yaml)
runs the FP8 base checkpoint through Forge with TP=1 on one H200 GPU. It leaves
MTP off because the current Forge cache stages one Hugging Face repository,
while the draft model is separate. Gemma is gated, so Forge's configured
Hugging Face credential must have access to the base checkpoint. Forge runs
profile1 (1K/1K); that smoke does not validate the recipe's 8K/1K workload.
