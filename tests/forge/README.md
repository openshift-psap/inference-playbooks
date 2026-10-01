# Forge recipe tests

Posting `/test forge` without a recipe selector runs clusterless validation for both the legacy Forge catalog and canonical Recipe v3 files. To deploy one recipe, include its recipe ID, a currently Fournos-registered target, the target namespace, and the GPU count required by the recipe:

```text
/test forge
/cluster <registered-target>
/exclusive true
/gpu nvidia <gpu-count-required-by-the-recipe>
/var inference_playbooks.recipe: <recipe-id>
/var inference_playbooks.namespace: <target-namespace>
/var inference_playbooks.workload: profile1
```

Forge supports catalogued RHOAI `LLMInferenceService` manifests and Recipe v3 `LeaderWorkerSet` deployments with one auxiliary `Service`. It gives every test unique resource names, waits for readiness, runs Forge `profile1` (1,000 input and 1,000 output tokens), captures the deployed state, and removes the test resources.

For a recipe with an RDMA resource request, map its logical resource name to the registered target's extended resource name:

```text
/test forge
/cluster <registered-target>
/exclusive true
/gpu nvidia <gpu-count-required-by-the-recipe>
/var inference_playbooks.recipe: <recipe-id>
/var inference_playbooks.namespace: <target-namespace>
/var inference_playbooks.rdma_resource: <target-extended-rdma-resource>
```

The Kimi K3 PP2/TP8 recipe needs two H200 nodes with eight GPUs each, an RDMA-capable inter-node network, and shared model storage. Its `rdma/ib` request stays cluster-neutral; supply the resource mapping above for the target. Forge currently runs `profile1` (1K/1K), so that smoke does not validate the recipe's declared GuideLLM 8K/1K workload.

For a Recipe v3 Hugging Face model, declare `deployment.model_cache` with `volume_name`, `pvc_size`, and `model_directory_name`; set `wait_timeout_seconds` when the download needs longer than the Forge default. The manifest must mount that volume in its LeaderWorkerSet pod template(s). Forge uses the existing Hugging Face cache helper to create or reuse an RWX PVC, download the model declared in `models/<model-id>/model.yaml`, and replace the named volume with that claim before applying the LeaderWorkerSet. The PVC is retained for later runs. Set a capacity that fits the checkpoint; the Kimi K3 recipe declares 2Ti for its approximately 1.56 TB weights. Direct Kubernetes deployments use the manifest's `model` claim name and must provide a populated RWX PVC with the files under `Kimi-K3`.

For `qwen-pp-validation`, the target needs RHOAI/KServe with `LLMInferenceService` support, LeaderWorkerSet, and two nodes with one compatible NVIDIA GPU each; an L4 with 24 GB is sufficient. The target namespace and model access must already be available.

For `qwen-single-gpu-smoke`, the manifest uses RHOAI's built-in single-node topology with one replica and one NVIDIA GPU. It omits `parallelism` and `worker`, so it does not need LeaderWorkerSet. Forge reuses its shared Hugging Face model-cache helper before deployment. The target needs `LLMInferenceService` v1alpha2, one schedulable NVIDIA GPU, a ReadWriteMany storage class, and the target namespace.

The GLM-5.2 catalog entry uses `pvc://glm52-fp8-weights`. Its `model_source` records where the weights came from; it does not make Forge download them. The target PVC must already be bound and populated before launching that recipe.
