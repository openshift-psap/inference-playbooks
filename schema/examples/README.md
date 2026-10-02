# Recipe examples

## Recipe v4 example

Recipe v4 uses a `serving` block — no hand-authored manifests needed.
Templates generate Kubernetes YAML from the serving configuration.

See [recipe-v4-example.yaml](recipe-v4-example.yaml) for a complete v4 recipe.

For the full v4 contributor flow, see
[CONTRIBUTING.md](../../CONTRIBUTING.md).

## v3 component fragments (legacy)

These files are fragments for `deployment.components` in v3 recipes. They
demonstrate schema shape for the legacy path where contributors hand-author
`config/` manifests.

| Starting point                                                    | Based on                                                                                                                                                                                                                     |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Deployment](deployment.components.yaml)                         | [Gemma vLLM Deployment](https://github.com/openshift-psap/inference-playbooks/blob/main/models/gemma-4/vllm/v0.24.0/single-node/manifests/gemma-4-26b-a4b-it-fp8-deployment.yaml)                                           |
| [LLMInferenceService](llmisvc.components.yaml)                   | [KServe DeepSeek P/D example](https://github.com/red-hat-data-services/kserve/blob/main/docs/samples/llmisvc/dp-ep/deepseek-r1-gpu-rdma-roce/llm-inference-service-dp-ep-deepseek-r1-pd-gpu-p-deepep-ht-d-pplx.yaml); `rhoai` |
| [llm-d optimized baseline](llmd-optimized.components.yaml)       | [llm-d optimized-baseline guide](https://github.com/llm-d/llm-d/tree/main/guides/optimized-baseline)                                                                                                                        |
| [llm-d P/D Deployments and router](llmd-pd.components.yaml)      | [llm-d P/D guide](https://github.com/llm-d/llm-d/tree/main/guides/pd-disaggregation)                                                                                                                                       |
| [P/D LeaderWorkerSets and router](lws-pd.components.yaml)        | [llm-d `release-0.8` wide EP guide](https://github.com/llm-d/llm-d/tree/release-0.8/guides/wide-ep-lws)                                                                                                                    |

## Reader notes

Optional [recipe notes](recipe-notes.yaml) hold the authored headline,
decision rationale across settings, omitted options, image compatibility,
feature claims, quickstart, insights, known issues, and sizing pointer. A
recipe links them with `notes: guides/notes.yaml`.

## Validation

```bash
python3 tools/validate.py --current    # schema + references
python3 tools/render.py --dry-run      # v4 manifest generation
```
