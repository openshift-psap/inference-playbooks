"""Run genuine pinned Forge CLI + Caliper in the built image; fake only Kubernetes/jobs.

No cluster access or GPU performance claims. Run using the command in the runner guide.
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import yaml

sys.path.insert(0, '/opt/playbooks/tools')
sys.path.insert(0, '/opt/forge/projects/inference_playbooks/orchestration')
import test_phase as phase
import run_contributor as runner
from contributor_bundle import FORGE_REVISION, checksum, json_bytes, read_archive


with tempfile.TemporaryDirectory() as tmp:
    tool = os.environ.get('SMOKE_HARNESS', 'guidellm')
    assert tool in {'guidellm', 'aiperf'}
    root = Path(tmp)
    inputs = root / 'inputs'
    recipe_path = 'models/test/recipes/test-recipe/recipe.yaml'
    manifest_path = 'models/test/recipes/test-recipe/manifests/vllm-test/deployment.yaml'
    recipe = {'schema_version': 4, 'recipe_id': 'test-recipe', 'model_id': 'test',
              'platforms': [{'stack': 'vllm', 'version': 'test', 'overrides': 'platforms/vllm.yaml'}],
              'hardware_profile': 'hardware-profiles/test.yaml', 'workload_profile': 'guidellm-8k1k',
              'deployment': {'scope': 'single-node'},
              'serving': {'model': 'example/model', 'parallelism': {'mode': 'tp', 'tp': 1}}}
    manifest = {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': 'test'},
                'spec': {'selector': {'matchLabels': {'app': 'test'}}, 'template': {
                    'metadata': {'labels': {'app': 'test'}}, 'spec': {'containers': [
                        {'name': 'vllm', 'image': 'docker.io/example/model@sha256:' + 'c' * 64,
                         'command': ['vllm', 'serve'], 'args': ['example/model'],
                         'ports': [{'containerPort': 8000}]}]}}}}
    data = {recipe_path: recipe, manifest_path: manifest,
            'models/test/model.yaml': {'model_id': 'test', 'huggingface_id': 'example/model'},
            'hardware-profiles/test.yaml': {'profile_revision': 1, 'accelerator_key': 'nvidia-h200-x1'}}
    for path, document in data.items():
        target = inputs / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(document))
    request = {'schema_version': 1, 'run_id': 'contributor-' + 'a' * 32,
               'recipe_path': recipe_path, 'manifest_path': manifest_path,
               'tested_revision': 'b' * 40, 'platform': {'stack': 'vllm', 'version': 'test'},
               'inputs': {path: checksum((inputs / path).read_bytes()) for path in data},
               'forge_revision': FORGE_REVISION, 'runner_image': 'quay.io/example/runner@sha256:' + 'd' * 64,
               'harness_image': 'ghcr.io/vllm-project/guidellm@sha256:' + 'e' * 64,
               'workload': tool + '-smoke'}
    state, actions = {}, []
    def forge_oc(*args, **kwargs):
        actions.append(args)
        if args[0] == 'apply':
            doc = yaml.safe_load(Path(args[2]).read_text())
            if doc['kind'] == 'Deployment':
                state['deployment'] = doc
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    def runtime_oc(*args):
        assert args[:2] == ('get', 'pods'), args
        return json_bytes({'items': [{'spec': state['deployment']['spec']['template']['spec'],
            'status': {'containerStatuses': [{'name': 'vllm', 'ready': True, 'imageID': 'sha256:' + 'c' * 64}]}}]})
    def benchmark(**kwargs):
        assert kwargs['image'] == request['harness_image']
        from projects.core.library import env
        if tool == 'aiperf':
            target = env.ARTIFACT_DIR / '001__run_aiperf_benchmark/artifacts/aiperf/profile_export_aiperf.json'
            target.parent.mkdir(parents=True)
            target.write_bytes(json_bytes({
                'request_count': {'avg': 10, 'unit': 'requests'},
                'error_request_count': {'avg': 0, 'unit': 'requests'},
                'request_throughput': {'avg': 5.0, 'unit': 'requests/sec'},
                'output_token_throughput': {'avg': 200.0, 'unit': 'tokens/sec'},
                'time_to_first_token': {'avg': 45.0, 'p95': 80.0, 'unit': 'ms'},
                'inter_token_latency': {'avg': 8.0, 'p95': 12.0, 'unit': 'ms'},
                'request_latency': {'avg': 400.0, 'p95': 600.0, 'unit': 'ms'},
            }))
            return
        target = env.ARTIFACT_DIR / '001__run_guidellm_benchmark/artifacts/results/benchmarks.json'
        target.parent.mkdir(parents=True)
        records = []
        for concurrency in (1, 5):
            records.append({'config': {'strategy': {'streams': concurrency}, 'run_id': 'fixture'},
                            'metrics': {'requests_per_second': {'successful': {'mean': 2.5}},
                                'output_tokens_per_second': {'successful': {'mean': 100.0}},
                                'tokens_per_second': {'successful': {'mean': 120.0}},
                                'request_concurrency': {'successful': {'mean': float(concurrency)}},
                                'request_totals': {'successful': 100, 'errored': 0}},
                            'scheduler': {'strategy': {'streams': concurrency}},
                            'scheduler_metrics': {'start_time': 1000.0, 'end_time': 1060.0}})
        target.write_bytes(json_bytes({'benchmarks': records, 'metadata': {'guidellm_version': '0.7.4'},
                                       'args': {'data': [{'prompt_tokens': 1000, 'output_tokens': 1000}]}}))
    phase.oc = forge_oc
    phase.run_guidellm_benchmark.run = benchmark
    from projects.aiperf.toolbox.run_aiperf_benchmark import main as aiperf
    aiperf.run = benchmark
    runner.oc = runtime_oc
    runner.preflight = lambda *args: {'synthetic_fixture': True}
    os.environ['KUBECONFIG'] = str(root / 'nonexistent-test-kubeconfig')
    output = root / 'outputs'
    code = runner.execute(request, inputs, output, 'benchmark', 'fixture', Path('/opt/forge'), 60)
    if code:
        for path in (output / 'private-diagnostics').glob('*-error.txt'):
            print(path.read_text(), file=sys.stderr)
    assert code == 0, 'genuine Forge CLI/postprocessing/collection failed'
    archive = output / 'evidence.tar.gz'
    envelope, files = read_archive(archive, checksum(archive.read_bytes()))
    assert envelope['outcome'] == 'succeeded'
    assert json.loads(files['result.json'])['completeness'] == 'smoke'
    assert any(action[0] == 'delete' for action in actions), 'Forge finalizers did not run'
    assert b'${ENDPOINT}' not in files['kpis.json']
    print(f'PASS: pinned Forge CLI -> synthetic {tool} jobs -> genuine Caliper -> portable bundle')
