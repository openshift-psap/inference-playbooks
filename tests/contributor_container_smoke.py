"""Run genuine pinned Forge CLI + Caliper in the built image; fake only Kubernetes/jobs.

No cluster access or GPU performance claims. Run using the command in the runner guide.
"""
import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import yaml

sys.path.insert(0, '/opt/playbooks/tools')
sys.path.insert(0, '/opt/forge/projects/inference_playbooks/orchestration')
import run_contributor as runner
from contributor_bundle import FORGE_REVISION, checksum, json_bytes, read_archive


with tempfile.TemporaryDirectory() as tmp:
    tool = os.environ.get('SMOKE_HARNESS', 'guidellm')
    assert tool in {'guidellm', 'aiperf'}
    root = Path(tmp)
    inputs = root / 'inputs'
    recipe_path = 'models/test/recipes/test-recipe/recipe.yaml'
    manifest_path = 'models/test/recipes/test-recipe/manifests/vllm-test/deployment.yaml'
    recipe = {'schema_version': 4, 'recipe_id': 'authoritative-recipe-id', 'model_id': 'test',
              'platforms': [{'stack': 'vllm', 'version': 'test', 'overrides': 'platforms/vllm.yaml'}],
              'hardware_profile': 'hardware-profiles/test.yaml', 'workload_profile': 'guidellm-8k1k',
              'deployment': {'scope': 'single-node'},
              'serving': {'model': 'example/model', 'parallelism': {'mode': 'tp', 'tp': 1}}}
    manifest = {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': 'test'},
                'spec': {'selector': {'matchLabels': {'app': 'test'}}, 'template': {
                    'metadata': {'labels': {'app': 'test'}}, 'spec': {
                        'securityContext': {'runAsUser': 65534, 'runAsNonRoot': True}, 'containers': [
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
        if args[0] == 'create':
            doc = json.loads(kwargs['input_text'])
            assert doc == {**manifest, 'metadata': {**manifest['metadata'], 'namespace': 'benchmark'}}
            assert '--raw=/apis/apps/v1/namespaces/benchmark/deployments' in args
            doc['metadata']['uid'] = 'fixture-deployment-uid'
            state['deployment'] = doc
            return SimpleNamespace(returncode=0, stdout=json.dumps(doc), stderr='')
        if args[0] == 'delete':
            assert json.loads(kwargs['input_text'])['preconditions']['uid'] == 'fixture-deployment-uid'
            state['deleted'] = True
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    def get_json(resource, **kwargs):
        if resource == 'namespaces':
            return {'metadata': {'name': 'benchmark'}}
        if resource == 'deployments.apps':
            return state.get('deployment') if kwargs.get('name') else {'items': []}
        if resource == 'replicasets.apps':
            return {'metadata': {'name': 'fixture-rs', 'namespace': 'benchmark', 'uid': 'fixture-rs-uid',
                    'ownerReferences': [{'apiVersion': 'apps/v1', 'kind': 'Deployment', 'name': 'test',
                                         'uid': 'fixture-deployment-uid', 'controller': True}]}}
        assert resource == 'pods', resource
        pod = {'metadata': {'name': 'fixture-pod', 'namespace': 'benchmark',
                            'ownerReferences': [{'apiVersion': 'apps/v1', 'kind': 'ReplicaSet',
                                                 'name': 'fixture-rs', 'uid': 'fixture-rs-uid', 'controller': True}]},
               'spec': state['deployment']['spec']['template']['spec'],
               'status': {'podIP': '192.0.2.1', 'conditions': [{'type': 'Ready', 'status': 'True'}],
                          'containerStatuses': [{'name': 'vllm', 'ready': True, 'imageID': 'sha256:' + 'c' * 64}]}}
        unrelated = copy.deepcopy(pod)
        unrelated['metadata']['ownerReferences'][0]['uid'] = 'unrelated-rs-uid'
        return {'items': [pod, unrelated]}
    def benchmark(**kwargs):
        assert kwargs['image'] == request['harness_image']
        from projects.core.library import env
        from projects.core.dsl.script_manager import get_script_manager
        # Keep registration real: imports must happen at FORGE_HOME, not /tmp.
        # Replacing the benchmark entrypoint alone previously hid this live failure.
        module_path = f'projects/{tool}/toolbox/run_{tool}_benchmark/main.py'
        assert get_script_manager().get_tasks_from_file(module_path), 'Forge DSL tasks registered under wrong working directory'
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
            records.append({'config': {'strategy': {'streams': concurrency}, 'run_id': 'fixture',
                                       'backend': {'api_key': None}},
                            'metrics': {'requests_per_second': {'successful': {'mean': 2.5}},
                                'output_tokens_per_second': {'successful': {'mean': 100.0}},
                                'tokens_per_second': {'successful': {'mean': 120.0}},
                                'request_concurrency': {'successful': {'mean': float(concurrency)}},
                                'request_totals': {'successful': 100, 'errored': 0}},
                            'scheduler': {'strategy': {'streams': concurrency}},
                            'scheduler_metrics': {'start_time': 1000.0, 'end_time': 1060.0}})
        target.write_bytes(json_bytes({'benchmarks': records, 'metadata': {'guidellm_version': '0.7.4'},
                                       'args': {'data': [{'prompt_tokens': 1000, 'output_tokens': 1000}]}}))
    original_instrument = runner.instrument
    def instrument(phase, *args):
        phase.oc = forge_oc
        phase.oc_get_json = get_json
        phase.wait_recipe.oc = forge_oc
        phase.wait_recipe.oc_get_json = get_json
        phase.run_guidellm_benchmark.run = benchmark
        from projects.aiperf.toolbox.run_aiperf_benchmark import main as aiperf
        aiperf.run = benchmark
        original_instrument(phase, *args)
    runner.instrument = instrument
    runner.admission_probe = lambda pod: pod
    runner.preflight = lambda *args: {'synthetic_fixture': True}
    os.environ['KUBECONFIG'] = str(root / 'nonexistent-test-kubeconfig')
    output = root / 'outputs'
    code = runner.execute(request, inputs, output, 'benchmark', 'fixture', Path('/opt/forge'), 60)
    if code:
        for path in (output / 'private-diagnostics').glob('*-error.txt'):
            print(path.read_text(), file=sys.stderr)
    assert code == 0, 'genuine Forge CLI/postprocessing/collection failed'
    progress = json.loads((output / 'progress.json').read_text())
    assert progress['stage'] == 'finished' and progress['outcome'] == 'succeeded'
    assert progress['serving_ready_containers'] == 1
    archive = output / 'evidence.tar.gz'
    envelope, files = read_archive(archive, checksum(archive.read_bytes()))
    assert 'progress.json' not in files, 'local progress must not enter evidence payloads'
    assert envelope['outcome'] == 'succeeded'
    assert json.loads(files['result.json'])['completeness'] == 'smoke'
    assert len(json.loads(files['runtime.json'])['pods_before']['items']) == 1
    assert any(action[0] == 'delete' for action in actions), 'Forge finalizers did not run'
    assert state['deleted'] and not any(action[0] == 'apply' for action in actions)
    assert b'${ENDPOINT}' not in files['kpis.json']
    from projects.caliper.engine.kpi.html_generator import generate_kpi_html_from_file
    kpi_path = root / 'kpis.json'
    kpi_path.write_bytes(files['kpis.json'])
    html = generate_kpi_html_from_file(kpi_path).read_text()
    assert kpi_path.read_bytes() == files['kpis.json'], 'HTML generation changed authoritative KPIs'
    if tool == 'aiperf':
        document = json.loads(files['kpis.json'])
        assert all(not kpi['is_curve'] for test in document['tests'] for kpi in test['kpis'])
        assert len({test['labels']['sweep_id'] for test in document['tests']}) == 1
        assert 'chart-aiperf_output_token_throughput_avg_vs_concurrency' in html
        assert "name: 'sweep/" in html
        # Presentation-only synthetic second point: prove sorting/connection,
        # without changing the real smoke workload or its archived scalar KPIs.
        from projects.caliper.engine.kpi.html_generator import HTMLGenerator
        sweep_fixture = copy.deepcopy(document)
        second = copy.deepcopy(sweep_fixture['tests'][0])
        second['run_id'] += '-synthetic-c4'
        second['labels']['concurrency'] = '4'
        for kpi in second['kpis']:
            if kpi['kpi_id'] == 'aiperf_output_token_throughput_avg':
                kpi['value'] = 800.0
        sweep_fixture['tests'].insert(0, second)
        fixture_path = root / 'synthetic-sweep.html'
        original_fixture = copy.deepcopy(sweep_fixture)
        HTMLGenerator().generate_kpi_html(sweep_fixture, fixture_path)
        assert sweep_fixture == original_fixture
        assert 'const rawData = [[1.0, 200.0], [4.0, 800.0]];' in fixture_path.read_text()
        assert kpi_path.read_bytes() == files['kpis.json']
    print(f'PASS: pinned Forge CLI -> synthetic {tool} jobs -> genuine Caliper -> portable bundle')
