# Gemma 4 base on B200

This day-zero [recipe](../recipe.yaml) serves the selected checkpoint with
one GPU from an eight-B200 node. Its [hardware profile](../../../../../hardware-profiles/nvidia-b200-8x-r1.yaml)
records the confirmed GPU type and node count; other hardware facts remain unspecified.

Apply the [generated Deployment](../manifests/vllm-v0.31.0/deployment.yaml)
after preparing a B200-capable Kubernetes/OpenShift node and the `hf-token`
Secret with key `HF_TOKEN`. Serving configuration and image version are
authoritative in the recipe. CPU and memory allocations are initial values
carried over from the existing H200 Gemma recipe and require verification.
The manifest requests one GPU but does not enforce B200 node selection;
ensure scheduling targets a B200 node, especially in a mixed-GPU cluster.

Use the shared [GuideLLM 8K/1K job](../../../../../benchmarks/manifests/guidellm-8k1k-job.yaml),
setting its endpoint, model, and tokenizer for this deployment. No deployment
validation or benchmark evidence is attached, and no latency, throughput, or
quality improvement is claimed. The NVFP4 recipe changes both checkpoint
quantization and KV-cache dtype, so comparisons measure the combined configuration.

The [NVFP4 model card](https://huggingface.co/RedHatAI/gemma-4-26B-A4B-it-NVFP4)
documents the quantized checkpoint.
