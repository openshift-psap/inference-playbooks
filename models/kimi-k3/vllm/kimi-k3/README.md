# Kimi K3 on vLLM

## Forge profile1 smoke

[Recipe v3](recipes/nvidia-h200-x8/guidellm-8k1k/pp2-tp8/recipe.yaml)
starts Kimi K3 with TP=8 on each of two H200 nodes and PP=2 across the nodes.
Forge stages `moonshotai/Kimi-K3` in a shared 2Ti model-cache PVC and runs
profile1 (1K/1K). This checks startup and a short request; it does not validate
the declared 8K/1K workload or RoCE throughput. The Kimi Humming image tag and
its root-run SITU bootstrap are retained from the submitted Kimi recipe and
still need target-cluster verification.
