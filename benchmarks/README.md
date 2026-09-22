# Common Benchmark Jobs

This directory contains Kubernetes Job templates that can benchmark any
OpenAI-compatible inference endpoint. Apply each Job in the same namespace as
the target Service, after making the required substitutions described below.

## Before You Apply a Job

Edit the `env` section in the manifest. The placeholder values are intentional:
they prevent a benchmark from being accidentally directed at the wrong model.

| Variable | Required change | Used by |
|---|---|---|
| `ENDPOINT` | Set to the target's in-cluster base URL, including its port; for example, `http://my-model:8000`. Do not add `/v1/...`. | Both Jobs |
| `MODEL` | Set to the model identifier accepted by the endpoint. For vLLM, this is normally the `--served-model-name` value. | Both Jobs |
| `TOKENIZER` | Set to the Hugging Face model ID or another tokenizer path that matches the served model. | Both Jobs |
| `image` | The templates use `quay.io/rh-ee-thibrahi/aiperf:0.12.0`. Replace it only if the target cluster cannot pull it or requires a different AIPerf version. | AIPerf AgentX |

The target must be reachable from the Job namespace and expose OpenAI-compatible
endpoints. For AgentX, it must support streaming chat completions and report
token usage. Add the appropriate service-account, CA, and authentication
configuration when the endpoint requires mTLS or an API key; no credentials are
included in these templates.

## GuideLLM: 8K Input / 1K Output

[`manifests/guidellm-8k1k-job.yaml`](manifests/guidellm-8k1k-job.yaml) runs
synthetic requests with 8,000 prompt tokens and 1,000 output tokens. It tests
fixed concurrent streams of 1, 4, 16, 32, 64, and 128, for 300 seconds per
stream count.

Change the `--data`, `--profile`, or `--constraint` command arguments only
when defining a different workload. Make sure the server's configured maximum
model length accepts the requested input plus output tokens, including any
chat-template overhead. The template uses GuideLLM `v0.7.3`; keep the command
syntax aligned with that image tag when upgrading it.

## AIPerf: AgentX

Both AgentX manifests run the date-pinned AgentX trace corpus with the
`inferencex-agentx-mvp` scenario. This is not an 8K/1K workload: it replays
multi-turn, long-context coding sessions.

Choose exactly one context variant:

- [`manifests/aiperf-agentx-128k-job.yaml`](manifests/aiperf-agentx-128k-job.yaml)
  sets `--max-context-length 128000`. Use it only when the server accepts 128K
  contexts. This filters out larger traces, so results are comparable only to
  runs with the same cap.
- [`manifests/aiperf-agentx-unlimited-context-job.yaml`](manifests/aiperf-agentx-unlimited-context-job.yaml)
  omits `--max-context-length`. Use it only when the server accepts every trace
  request in the corpus. A server-side context rejection counts toward AgentX's
  context-overflow rate and can invalidate the run.

The templates match InferenceX's standard replay settings: the complete
393-trace corpus, pre-canned assistant responses, a 25–75% trajectory-start
window, seed 42, 10% failed-request threshold, 10-minute cache warmup, and
30-minute warmup grace period. The AgentX scenario requires a minimum
`--benchmark-duration` of 900 seconds; the templates use the standard 1,800
seconds. Its scenario-locked cache-bust and idle-gap settings are injected by
the scenario, so do not supply conflicting flags.

The Job downloads the public trace corpus and tokenizer on first use. If egress
is restricted, provide an approved image/cache strategy before submitting it.

### AIPerf Image

The AgentX templates use the project image
`quay.io/rh-ee-thibrahi/aiperf:0.12.0`, built from the supplied Containerfile.
Confirm that the target cluster can pull it before starting a benchmark. If it
is made private, create an image pull secret in every benchmark namespace.

NVIDIA's published alternative is `nvcr.io/nvidia/ai-dynamo/aiperf:0.12.0`,
which requires an NGC pull credential in the benchmark namespace. The obsolete
`nvcr.io/nvidia/aiperf` and `ghcr.io/nvidia/aiperf` image paths are not usable.

For clusters without NGC access, build and publish the provided
[`images/aiperf/Containerfile`](images/aiperf/Containerfile). It starts from
the small CPU-only Python base and installs a pinned `aiperf==0.12.0` in an
isolated virtual environment. The image is compatible with OpenShift's
arbitrary UID policy and eliminates per-Job package installation. See
[`images/aiperf/README.md`](images/aiperf/README.md) for the build and
registry-pull-secret steps.

## Storage, Resources, and Job Lifecycle

All benchmark Jobs write artifacts to `/results` on a PersistentVolumeClaim
named `benchmark-results`. Create that claim in the benchmark namespace before
applying a Job, choosing a storage class, size, and access mode that fit the
cluster's storage policy. Results remain available after the Job's seven-day TTL
cleanup. A reusable post-TTL retrieval pod is tracked in
[issue #13](https://github.com/openshift-psap/inference-playbooks/issues/13).
Until it lands, copy artifacts from the completed benchmark pod before TTL
cleanup. The AIPerf cache is still temporary; replace `hf-cache` with a PVC if
repeated runs should reuse downloads.

This PVC-backed collection workflow is for standalone benchmark runs. It is not
needed when running through the Forge CI framework, which handles result
collection.

The CPU and memory requests are starting points for a single load-generator
pod. Increase them if the client becomes the bottleneck, and set node selectors,
tolerations, or a service account to match your cluster's policies. Do not add a
GPU request unless the benchmark client specifically needs one.

Job names are namespace-scoped. Delete a completed Job before rerunning the
same manifest. Do not rely on changing `metadata.name` to retain multiple runs:
the current manifests use fixed artifact filenames on the shared PVC. Use a
separate PVC for each retained run, or copy/archive the artifacts before the
next run.

## Run and Collect Results

```bash
kubectl apply --dry-run=client -f benchmarks/manifests/guidellm-8k1k-job.yaml
kubectl apply -f benchmarks/manifests/guidellm-8k1k-job.yaml -n <namespace>
kubectl wait --for=condition=complete job/guidellm-8k1k -n <namespace> --timeout=45m
POD=$(kubectl get pods -n <namespace> -l job-name=guidellm-8k1k -o jsonpath='{.items[0].metadata.name}')
kubectl cp -n <namespace> "$POD":/results ./guidellm-8k1k-results
```

This command requires the completed benchmark pod to still exist. Run it before
the Job's seven-day TTL cleanup; after that cleanup, use the retrieval workflow
from [issue #13](https://github.com/openshift-psap/inference-playbooks/issues/13)
once it is available.

Use the equivalent AIPerf Job name and allow at least two hours for its
download, warmup, 30-minute profile, and result export. Check Job logs when a
Job fails:

```bash
kubectl logs -n <namespace> job/aiperf-agentx-128k
```
