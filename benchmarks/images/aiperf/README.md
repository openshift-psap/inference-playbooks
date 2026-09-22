# AIPerf Benchmark Image

Build this image when the NVIDIA NGC image is unavailable to cluster nodes or
when a benchmark needs a pinned AIPerf version. It is based on the small
`python:3.12-slim` image because AIPerf is a CPU-only load generator; a
benchmark pod does not need the inference server's CUDA/vLLM image. It installs
AIPerf once at build time, avoiding package installation and dependency drift
each time a benchmark Job starts.

The default is `aiperf==0.12.0`, which requires Python 3.11 or newer. The
Python base and virtual environment are readable by OpenShift's arbitrary UID.

## Build and Publish

Choose a registry that Janus can pull from, then build for its `linux/amd64`
nodes. Use an immutable tag after the first build.

```bash
export AIPERF_IMAGE=quay.io/<organization>/aiperf:0.12.0
docker buildx build --platform linux/amd64 --push \
  --tag "$AIPERF_IMAGE" \
  --file benchmarks/images/aiperf/Containerfile \
  benchmarks/images/aiperf
```

To build a different pinned AIPerf version:

```bash
docker buildx build --platform linux/amd64 --push \
  --build-arg AIPERF_VERSION=0.11.0 \
  --tag "$AIPERF_IMAGE" \
  --file benchmarks/images/aiperf/Containerfile \
  benchmarks/images/aiperf
```

For a private registry, create an `imagePullSecret` in every namespace that
runs a benchmark, then add its name under `spec.template.spec.imagePullSecrets`
in the Job manifest. Set the Job's `image` field to the published
`$AIPERF_IMAGE` value.

The NVIDIA-maintained alternative is
`nvcr.io/nvidia/ai-dynamo/aiperf:0.12.0`. It requires an NGC pull credential;
do not use the obsolete `nvcr.io/nvidia/aiperf` or `ghcr.io/nvidia/aiperf`
paths.
