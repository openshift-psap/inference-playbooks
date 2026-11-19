"""One committed synthetic recipe shared by unit tests and the CI import proof.

The recipe, manifest, and inputs are intentionally minimal. They let tooling own
the contract under test (Git closure, launch validation, import planning) while
the metrics stay synthetic; nothing here is model evidence.
"""
import shutil
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]

SMOKE_WORKLOADS = ('guidellm-smoke', 'aiperf-smoke')
# Synthetic harness pins: no image is pulled in these proofs, and the workload
# metadata only needs a fully qualified digest reference to validate.
HARNESS_IMAGES = {
    'guidellm-smoke': 'ghcr.io/vllm-project/guidellm@sha256:' + 'b' * 64,
    'aiperf-smoke': 'nvcr.io/nvidia/aiperf@sha256:' + 'e' * 64,
}


def commit(repo: Path):
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.org',
                    'commit', '-m', 'synthetic fixture'], check=True, capture_output=True)


def build_source_repo(destination: Path):
    """Create and commit models/test/recipes/test-recipe under ``destination``."""
    repo = destination / 'repo'
    repo.mkdir(parents=True)
    shutil.copytree(REPO / 'schema', repo / 'schema')
    root = repo / 'models/test/recipes/test-recipe'
    root.mkdir(parents=True)
    recipe = {'schema_version': 4, 'recipe_id': 'test-recipe', 'model_id': 'test',
              'platforms': [{'stack': 'vllm', 'version': 'v0.24.0', 'overrides': 'platforms/vllm.yaml'}],
              'hardware_profile': 'hardware-profiles/test.yaml', 'workload_profile': 'guidellm-8k1k',
              'deployment_mode': 'tp1', 'optimization_intent': 'latency', 'maturity': 'contributed',
              'deployment': {'scope': 'single-node'},
              'serving': {'model': 'example/model', 'image': 'docker.io/vllm/vllm-openai:v0.24.0',
                          'parallelism': {'mode': 'tp', 'tp': 1}, 'args': []}}
    (root / 'recipe.yaml').write_text('# retain authored comment\n' + yaml.safe_dump(recipe, sort_keys=False))
    (repo / 'models/test/model.yaml').write_text('model_id: test\nhuggingface_id: example/model\n')
    (root / 'platforms').mkdir()
    (root / 'platforms/vllm.yaml').write_text('{}\n')
    profile = repo / 'hardware-profiles/test.yaml'
    profile.parent.mkdir()
    profile.write_text('profile_revision: 1\naccelerator_key: nvidia-h200-x1\n')
    server = {'name': 'vllm', 'image': recipe['serving']['image'],
              'command': ['vllm', 'serve'], 'args': ['example/model'],
              'ports': [{'containerPort': 8000}],
              'resources': {'limits': {'nvidia.com/gpu': '1'}}}
    deployment = {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': 'test'},
                  'spec': {'replicas': 1, 'selector': {'matchLabels': {'app': 'test'}},
                           'template': {'metadata': {'labels': {'app': 'test'}}, 'spec': {
                               'securityContext': {'runAsUser': 65534, 'runAsNonRoot': True}, 'containers': [server]}}}}
    manifest = root / 'manifests/vllm-v0.24.0/deployment.yaml'
    manifest.parent.mkdir(parents=True)
    manifest.write_text(yaml.safe_dump(deployment))
    subprocess.run(['git', '-C', str(repo), 'init'], check=True, capture_output=True)
    commit(repo)
    return repo, root, deployment
