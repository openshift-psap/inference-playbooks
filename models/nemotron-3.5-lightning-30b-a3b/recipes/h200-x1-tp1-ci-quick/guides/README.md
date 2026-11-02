# Nemotron 3.5 Lightning FP8: single-H200 workflow smoke

This initial recipe tests the portable contributor workflow with
[RedHatAI's FP8 checkpoint](https://huggingface.co/RedHatAI/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-FP8).
It is **day-zero**, not a performance recommendation or independent validation.

Sources of truth:

- [Model identity](../../../model.yaml).
- [Serving configuration and pinned checkpoint/image](../recipe.yaml).
- [OpenShift admission and cold-start overlay](../config/kustomization.yaml).
- [Generated deployment](../manifests/vllm-v0.31.0/deployment.yaml).
- [Hardware capability profile](../../../../../hardware-profiles/nvidia-h200-sxm-8x-nvlink-r1.yaml).

The node profile describes eight H200 GPUs; this recipe requests only one.
The model is public and ungated at intake, so the overlay removes the renderer's
default HF-token Secret requirement. Credentials are not needed for this model.
OpenShift assigns the non-root UID through `restricted-v2`; no SCC grants or
privileged namespace are required. Cache data uses the existing writable
`emptyDir` mount and is discarded when the serving Pod is removed.

## Run the smoke

Follow the [portable contributor guide](../../../../../docs/contributor-benchmarks.md),
selecting this recipe, stack `vllm`, version `v0.31.0`, and workload
`guidellm-smoke`. That audited workload maps to Forge's `ci-quick`: synthetic
1k/1k tokens at concurrency 1 and 5, for 60 seconds per window. The nominal
`guidellm-8k1k` recipe classification does not turn smoke evidence into an 8k/1k
result; only the workload recorded in each imported run identifies what ran.

The first execution may download the checkpoint and compile GPU kernels.
Use the packaged monitor while the attempt runs, then validate and review the
returned archive before applying it. Imported `benchmark_runs` references are
the evidence index; do not copy metrics into this guide or promote maturity
based on `ci-quick` alone.
