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

## Benchmark-client compatibility

The public GuideLLM 0.7.4 and 0.8.1 images inspected for this smoke contain
Transformers 5.10.1. That version rejects this checkpoint's `full_attention` /
`linear_attention` layer types during tokenizer loading, even though vLLM can
serve the model. The failed attempt is not performance evidence.

The [compatibility image](../benchmarks/guidellm-compat/Containerfile) preserves
the audited GuideLLM 0.7.4 base digest and changes only Transformers to 5.15.0
and its required Safetensors dependency to 0.8.0. The replacement wheels are
hash-locked; `pip check` verifies the complete installed environment. The only
source adjustment replaces a public, credential-shaped API-key schema example
with an obvious placeholder after verifying its upstream provenance. It does
not change authentication defaults or disable secret scanning. No model files
are rewritten, and nothing is installed dynamically in benchmark Jobs.

Build from the repository root with Podman's `--squash-all` so the superseded
example is absent from inherited layers too; replacing only the live file does
not satisfy the scan gate:

```sh
COMPAT_DIR=models/nemotron-3.5-lightning-30b-a3b/recipes/h200-x1-tp1-ci-quick/benchmarks/guidellm-compat
COMPAT_IMAGE=quay.io/rh-ee-thibrahi/inference-playbooks-guidellm:v0.7.4-transformers5.15.0
podman build --squash-all -f "$COMPAT_DIR/Containerfile" \
  --label org.opencontainers.image.revision="$(git rev-parse HEAD)" \
  -t "$COMPAT_IMAGE" "$COMPAT_DIR"
python3 tools/scan_runner_image.py "$COMPAT_IMAGE"
```

After a successful scan, publish to a repository you control and pass its
immutable digest as `--harness-image` during preparation. The returned request
and run retain that exact image reference. The source commit label and pinned
base/build inputs preserve provenance even though the runtime layers are
flattened.

The [published release record](../benchmarks/guidellm-compat/release.json) binds
the registry digest to the scanned local image and source commit. For this
release, prepare the smoke package with:

```sh
python3 tools/prepare_contributor.py \
  models/nemotron-3.5-lightning-30b-a3b/recipes/h200-x1-tp1-ci-quick/recipe.yaml \
  --stack vllm --version v0.31.0 --workload guidellm-smoke \
  --harness-image quay.io/rh-ee-thibrahi/inference-playbooks-guidellm@sha256:c1a66d1131dee8852d87c9383ebc2dddb27d2ec730953b967b82c9976c7a0d22 \
  --output "$HOME/playbooks-runs/nemotron35-package-001"
```
