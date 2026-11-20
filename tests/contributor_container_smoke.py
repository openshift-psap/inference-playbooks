"""Run the image's genuine pinned Forge CLI + Caliper for one smoke workload.

Kubernetes objects and benchmark jobs are synthetic: this proves packaging,
privacy filtering, and native-export collection, never GPU performance. The
host driver imports the archive this script writes with the real maintainer
importer, so container-produced evidence is validated end to end.

Run inside the built image; see the runner guide for the driver command.
"""
import argparse
import copy
import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import yaml

TOOLS_ROOT = Path(os.environ.get('TOOLS_ROOT', '/opt/playbooks/tools'))
FORGE_ROOT = Path(os.environ.get('FORGE_ROOT', '/opt/forge'))
sys.path.insert(0, str(TOOLS_ROOT))
sys.path.insert(0, str(FORGE_ROOT / 'projects/inference_playbooks/orchestration'))
import run_contributor as runner
from contributor_bundle import FORGE_REVISION, WORKLOADS, checksum, json_bytes, json_load, read_archive

assert not list(TOOLS_ROOT.rglob('__pycache__')), 'runner source acquired Python caches'
assert not list(TOOLS_ROOT.rglob('*.pyc')), 'runner source acquired Python bytecode'

# Optional per-architecture proof: the image userland, the installed OpenShift
# CLI, and the hash-locked dependency set must match the build target. The ELF
# header identifies the binary even where the host machine is not the target.
EXPECTED_ARCH = os.environ.get('SMOKE_EXPECTED_ARCH', '')
if EXPECTED_ARCH:
    machines = {'amd64': ('x86_64', 62), 'arm64': ('aarch64', 183)}
    assert EXPECTED_ARCH in machines, 'unknown SMOKE_EXPECTED_ARCH'
    machine, elf_machine = machines[EXPECTED_ARCH]
    assert platform.machine() == machine, f'image userland is {platform.machine()}, expected {machine}'
    with Path('/usr/local/bin/oc').open('rb') as stream:
        header = stream.read(20)
    assert header[4] == 2 and header[5] == 1 and int.from_bytes(header[18:20], 'little') == elf_machine, \
        'bundled oc was built for another architecture'
    assert subprocess.run(['oc', 'version', '--client'], capture_output=True).returncode == 0, \
        'bundled oc cannot run in this image userland'


def run(request, inputs, output, manifest, tool):
    """Execute the real Forge flow against one synthetic serving deployment."""
    state, actions = {}, []

    def forge_oc(*args, **kwargs):
        actions.append(args)
        if args[0] == 'create':
            document = json.loads(kwargs['input_text'])
            assert document['kind'] == manifest['kind']
            assert document['metadata']['name'] == manifest['metadata']['name']
            assert document['metadata']['namespace'] == 'benchmark'
            assert '--raw=/apis/apps/v1/namespaces/benchmark/deployments' in args
            document['metadata']['uid'] = 'fixture-deployment-uid'
            state['deployment'] = document
            return SimpleNamespace(returncode=0, stdout=json.dumps(document), stderr='')
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
        unrelated = json.loads(json.dumps(pod))
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
    os.environ['KUBECONFIG'] = '/tmp/nonexistent-test-kubeconfig'
    code = runner.execute(request, inputs, output, 'benchmark', 'fixture', FORGE_ROOT, 60)
    if code:
        for path in (output / 'private-diagnostics').glob('*-error.txt'):
            print(path.read_text(), file=sys.stderr)
    assert code == 0, 'genuine Forge CLI/postprocessing/collection failed'
    progress = json_load((output / 'progress.json').read_bytes())
    assert progress['stage'] == 'finished' and progress['outcome'] == 'succeeded'
    assert progress['serving_ready_containers'] == 1
    assert any(action[0] == 'delete' for action in actions), 'Forge finalizers did not run'
    assert state['deleted'] and not any(action[0] == 'apply' for action in actions)
    return output / 'evidence.tar.gz'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, default=Path('/inputs/request.json'))
    parser.add_argument('--inputs', type=Path, default=Path('/inputs/playbooks'))
    parser.add_argument('--output', type=Path, default=Path('/outputs'))
    args = parser.parse_args()
    request = json_load(args.request.read_bytes())
    assert request['forge_revision'] == FORGE_REVISION, 'request predates the image Forge pin'
    assert request['workload'] in {'guidellm-smoke', 'aiperf-smoke'}, 'only smoke harnesses are supported here'
    tool = WORKLOADS[request['workload']]['harness']
    manifest = yaml.safe_load((args.inputs / request['manifest_path']).read_text())
    assert manifest['kind'] == 'Deployment', 'this proof covers the single-role Deployment contract'

    archive = run(request, args.inputs, args.output, manifest, tool)
    data = archive.read_bytes()
    digest = checksum(data)
    envelope, files = read_archive(archive, digest)
    assert envelope['request'] == request, 'archive must attribute the prepared request unchanged'
    assert envelope['outcome'] == 'succeeded'
    assert 'progress.json' not in files, 'local progress must not enter evidence payloads'
    assert {name for name in files if name.startswith('inputs/')} == {'inputs/' + name for name in request['inputs']}
    assert any(name.startswith('raw/') for name in files), 'native harness exports missing'
    assert json_load(files['result.json'])['completeness'] == 'smoke'
    assert json_load(files['runtime.json'])['pods_before']['items'][0]['spec']['containers'][0]['name'] == 'vllm'
    assert b'${ENDPOINT}' not in files['kpis.json']

    from projects.caliper.engine.kpi.html_generator import generate_kpi_html_from_file
    with tempfile.TemporaryDirectory(prefix='kpi-html-') as temporary:
        kpi_path = Path(temporary) / 'kpis.json'
        kpi_path.write_bytes(files['kpis.json'])
        html = generate_kpi_html_from_file(kpi_path).read_text()
        assert kpi_path.read_bytes() == files['kpis.json'], 'HTML generation changed authoritative KPIs'
        if tool == 'aiperf':
            document = json_load(files['kpis.json'])
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
            fixture_path = Path(temporary) / 'synthetic-sweep.html'
            original_fixture = copy.deepcopy(sweep_fixture)
            HTMLGenerator().generate_kpi_html(sweep_fixture, fixture_path)
            assert sweep_fixture == original_fixture
            assert 'const rawData = [[1.0, 200.0], [4.0, 800.0]];' in fixture_path.read_text()
            assert kpi_path.read_bytes() == files['kpis.json']

    # The sidecar is the integrity value the host importer is given; it never
    # substitutes for server-side validation of the archive itself.
    (args.output / 'evidence.sha256').write_text(digest + '\n')
    print(json.dumps({'workload': request['workload'], 'checksum': digest, 'tool': tool}))


if __name__ == '__main__':
    main()
