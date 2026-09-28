# Forge recipe tests

The Forge catalog selects RHOAI `LLMInferenceService` manifests from this repository. Posting `/test forge` without a recipe selector runs clusterless YAML validation. To deploy one recipe, include a catalog ID and a currently Fournos-registered target that meets that recipe's prerequisites:

```text
/test forge
/cluster <registered-target>
/var inference_playbooks.recipe: qwen-pp-validation
/var inference_playbooks.namespace: <target-namespace>
```

Forge waits for the `LLMInferenceService` to become Ready, then removes the uniquely named test service. This checks deployment readiness; it does not run the recipe's benchmark commands. The initial Forge runner supports RHOAI `LLMInferenceService` recipes only.

For `qwen-pp-validation`, the target needs RHOAI/KServe with `LLMInferenceService` support, LeaderWorkerSet, and two nodes with one compatible NVIDIA GPU each; an L4 with 24 GB is sufficient. The target namespace and model access must already be available.

The GLM-5.2 catalog entry uses `pvc://glm52-fp8-weights`. Its `model_source` records where the weights came from; it does not make Forge download them. The target PVC must already be bound and populated before launching that recipe.
