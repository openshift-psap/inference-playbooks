# Forge recipe tests

Posting `/test forge` without a recipe selector runs clusterless validation for both the legacy Forge catalog and canonical Recipe v3 files. To deploy one recipe, include its recipe ID and a currently Fournos-registered target that meets that recipe's prerequisites:

```text
/test forge
/cluster <registered-target>
/var inference_playbooks.recipe: qwen-pp-validation
/var inference_playbooks.namespace: <target-namespace>
```

Forge supports catalogued RHOAI `LLMInferenceService` manifests and Recipe v3 `LeaderWorkerSet` deployments with one auxiliary `Service`. It gives every test unique resource names, waits for readiness, runs Forge `profile1` (1,000 input and 1,000 output tokens), captures the deployed state, and removes the test resources.

For a Recipe v3 test on Janus, request the GPUs in the PR comment and map the recipe's generic `rdma/ib` request to Janus's live RoCE resource:

```text
/test forge
/cluster janus
/exclusive true
/gpu nvidia 16
/var inference_playbooks.recipe: kimi-k3-vllm-h200-pp2-tp8
/var inference_playbooks.namespace: kserve-e2e-perf
/var inference_playbooks.workload: profile1
/var inference_playbooks.rdma_resource: nvidia.com/roce
```

Recipe v3 model files and other declared prerequisites must already exist on the target. For Recipe v3, Forge does not silently change serving flags, probes, or hostPath model storage. The Kimi K3 recipe therefore requires a complete `/mnt/local/kimi-k3/models/Kimi-K3` copy on both selected GPU nodes. The `kimi-k3-hostpath` ServiceAccount must exist and be allowed to use the `hostmount-anyuid` SCC; Forge does not create or grant it. Restricted targets should use a prepopulated shared PVC instead.

For `qwen-pp-validation`, the target needs RHOAI/KServe with `LLMInferenceService` support, LeaderWorkerSet, and two nodes with one compatible NVIDIA GPU each; an L4 with 24 GB is sufficient. The target namespace and model access must already be available.

For `qwen-single-gpu-smoke`, the manifest uses RHOAI's built-in single-node topology with one replica and one NVIDIA GPU. It omits `parallelism` and `worker`, so it does not need LeaderWorkerSet. Forge reuses its shared Hugging Face model-cache helper before deployment. The target needs `LLMInferenceService` v1alpha2, one schedulable NVIDIA GPU, a ReadWriteMany storage class, and the target namespace.

The GLM-5.2 catalog entry uses `pvc://glm52-fp8-weights`. Its `model_source` records where the weights came from; it does not make Forge download them. The target PVC must already be bound and populated before launching that recipe.
