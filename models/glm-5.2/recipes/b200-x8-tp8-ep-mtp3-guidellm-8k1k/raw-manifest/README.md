# Original contributed manifests

Author: Yuchen Fama (`cemigo114`). These inputs are retained as contribution
history, not as the authoritative deployment configuration.

- [`rhoai-3.5.yaml`](rhoai-3.5.yaml): [PR #4](https://github.com/openshift-psap/inference-playbooks/pull/4),
  commit `4f879b83a68fd97cb6e06d950e942bbccc58ba09`, originally
  `models/glm-5.2/rhoai/3.5/aggregated-b200/manifests/glm-5-2-aggregated-b200-llmisvc.yaml`.
- [`rhoai-3.4.yaml`](rhoai-3.4.yaml): [PR #3](https://github.com/openshift-psap/inference-playbooks/pull/3),
  commit `80bd534fd68feffebaa6f33c26d164c5a4c55046`, originally
  `models/glm-5.2/rhoai/3.4/aggregated-b200/manifests/glm-5-2-aggregated-b200-llmisvc.yaml`.

Both target `RedHatAI/GLM-5.2-NVFP4-FP8`, upstream vLLM v0.27.0,
one eight-B200 node, TP8 + EP and MTP3. They require the HF token secret,
two writable cache PVCs, GPU operator and version-specific KServe configs.
The upstream image and startup-installed InstantTensor are a runtime
workaround, not the bundled RHOAI runtime or a customer recommendation.

The contributor reports direct 3.4 smoke on 2026-08-31 and gateway 3.5
smoke on 2026-09-10. No sanitized raw response, benchmark run provenance,
normalized measurement or accuracy evidence accompanied these submissions.
The GuideLLM 8K/1K workload selected during conversion remains untested.

Use [`../recipe.yaml`](../recipe.yaml) and its referenced final artifacts.
The 3.4 pin preserves the original bytes. The 3.5 conversion resolves source
environment-derived serving values into annotated recipe flags and adds
controller-aware TLS handling; it does not inherit deployment verification.
