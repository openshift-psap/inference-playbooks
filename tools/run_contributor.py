#!/usr/bin/env python3
"""Container entry point: run pinned Forge locally and return data, never upload it."""
from __future__ import annotations

import argparse
import copy
from contextlib import chdir
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from urllib.parse import urlparse

import yaml

from contributor_bundle import (FORGE_REVISION, WORKLOADS, checksum, json_bytes, json_load, relative,
                                replay, shareable, validate_request, write_archive)
from recipe_evidence import load_unique_yaml, load_unique_yaml_all
from contributor_security import UIDAdmissionRejected, admit_manifest, check_runtime_uids


def oc(*args):
    # Credentials/cluster responses are never logged or added to the shareable bundle.
    return subprocess.run(['oc', *args], check=True, capture_output=True, timeout=60).stdout


def admission_probe(pod):
    result = subprocess.run(['oc', 'create', '--dry-run=server', '-n', pod['metadata']['namespace'],
                             '-f', '-', '-o', 'json'], input=json_bytes(pod), capture_output=True, timeout=60)
    if result.returncode:
        message = result.stderr.decode(errors='replace')
        if 'runAsUser' in message and re.search(r'Forbidden|Invalid|denied', message):
            raise UIDAdmissionRejected(message)
        raise ValueError('Pod server-side admission probe failed: ' + message)
    return json_load(result.stdout)


def standalone_overrides(request, namespace):
    return {
        'inference_playbooks.recipe': Path(request['recipe_path']).parent.name,
        'inference_playbooks.manifest_path': request['manifest_path'],
        'inference_playbooks.namespace': namespace,
        'inference_playbooks.workload': WORKLOADS[request['workload']]['key'],
        'caliper.export.backend.mlflow.enabled': False,
        'caliper.export.notifications.enabled': False,
        'caliper.postprocess.s3.import.enabled': False,
        'caliper.postprocess.s3.export.enabled': False,
        'caliper.postprocess.analyze.enabled': False,
        'caliper.postprocess.visualize.enabled': False,
        'caliper.postprocess.kpi.dashboard_csv.enabled': False,
        'caliper.postprocess.kpi.artifacts_to_ai_data.enabled': False,
        'agentic.enabled': False,
        # Prevent cluster-name discovery/preset application from affecting execution.
        'ci_job.cluster': 'portable-contributor',
    }


def preflight(request, inputs: Path, namespace: str, context: str, private: Path):
    if not re.fullmatch(r'[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?', namespace) or not context:
        raise ValueError('explicit Kubernetes namespace required')
    config = load_unique_yaml(oc('config', 'view', '--raw', '--flatten', '--minify', '--context', context).decode())
    if any(user.get('user', {}).get('exec') for user in config.get('users', [])):
        raise ValueError('use a self-contained kubeconfig without external exec credential plugins')
    config['contexts'][0]['context']['namespace'] = namespace
    target = private / 'kubeconfig'
    target.write_text(yaml.safe_dump(config))
    target.chmod(0o600)
    os.environ['KUBECONFIG'] = str(target)
    oc('get', 'namespace', namespace, '-o', 'name')
    manifest = [d for d in load_unique_yaml_all((inputs / request['manifest_path']).read_text()) if d]
    primary = next(d for d in manifest if d['kind'] in {'Deployment', 'LeaderWorkerSet', 'LLMInferenceService'})
    kind = primary['kind']
    resource = {'Deployment': 'deployments.apps', 'LeaderWorkerSet': 'leaderworkersets.leaderworkerset.x-k8s.io',
                'LLMInferenceService': 'llminferenceservices.serving.kserve.io'}[kind]
    oc('api-resources', '--api-group=' + resource.split('.', 1)[1], '-o', 'name')
    for item in (resource, 'services', 'pods', 'jobs.batch', 'persistentvolumeclaims'):
        for verb in ('get', 'list', 'create', 'delete'):
            if oc('auth', 'can-i', verb, item, '-n', namespace).strip() != b'yes':
                raise ValueError('missing required namespace permission')
    nodes = json_load(oc('get', 'nodes', '-o', 'json'))
    capacities = [int(node.get('status', {}).get('allocatable', {}).get('nvidia.com/gpu', 0))
                  for node in nodes['items'] if not node.get('spec', {}).get('unschedulable')]
    if not any(capacities):
        raise ValueError('no visible NVIDIA GPU capacity; custom DRA allocation is not yet supported')
    spec = primary['spec']
    if kind == 'Deployment':
        roles = [spec['template']['spec']] * spec.get('replicas', 1)
    elif kind == 'LeaderWorkerSet':
        templates = spec['leaderWorkerTemplate']
        roles = ([templates['leaderTemplate']['spec']] +
                 [templates['workerTemplate']['spec']] * (templates['size'] - 1)) * spec.get('replicas', 1)
    else:
        if 'prefill' in spec:
            raise ValueError('LLMInferenceService P/D is not supported')
        roles = ([spec['template']] + [spec.get('worker', spec['template'])] *
                 (spec.get('parallelism', {}).get('pipeline', 1) - 1)) * spec.get('replicas', 1)
    requirements = [sum(int(c.get('resources', {}).get('limits', {}).get('nvidia.com/gpu', 0))
                        for c in role.get('containers', [])) for role in roles]
    if not requirements or min(requirements) < 1:
        raise ValueError('explicit NVIDIA GPU limits required for every serving role')
    for required in sorted(requirements, reverse=True):
        index = next((i for i, capacity in enumerate(capacities) if capacity >= required), None)
        if index is None:
            raise ValueError('visible schedulable-node GPU capacity cannot fit the declared serving roles')
        capacities[index] -= required
    def references(value):
        if isinstance(value, dict):
            if 'persistentVolumeClaim' in value:
                claim = value['persistentVolumeClaim']['claimName']
                pvc = json_load(oc('get', 'pvc', claim, '-n', namespace, '-o', 'json'))
                if pvc.get('status', {}).get('phase') != 'Bound':
                    raise ValueError('required model PVC is not Bound')
            if 'secretKeyRef' in value:
                oc('get', 'secret', value['secretKeyRef']['name'], '-n', namespace, '-o', 'name')
            for child in value.values():
                references(child)
        elif isinstance(value, list):
            for child in value:
                references(child)
    references(primary)
    facts = []
    for node in nodes['items']:
        labels = node.get('metadata', {}).get('labels', {})
        facts.append({'gpu_count': node.get('status', {}).get('allocatable', {}).get('nvidia.com/gpu'),
                      'labels': {k: v for k, v in labels.items() if k in {
                          'nvidia.com/gpu.product', 'nvidia.com/gpu.memory', 'nvidia.com/cuda.driver.major',
                          'nvidia.com/cuda.runtime.major', 'kubernetes.io/arch'}},
                      'node_info': {k: v for k, v in node.get('status', {}).get('nodeInfo', {}).items()
                                    if k in {'kernelVersion', 'osImage', 'containerRuntimeVersion', 'kubeletVersion'}}})
    return {'visible_nodes': facts, 'qualification': 'Observed capacity, not exclusive reservation or topology proof.'}


def instrument(phase, request, private: Path, runtime: dict):
    """Observe the existing Forge flow; retain its deployment/benchmark/finalizers."""
    original_write, original_workload = phase._write_launch_manifest, phase._run_workload
    original_metadata = phase.create_test_metadata
    original_strip = phase._strip_hardcoded_uid

    def preserve_uid(spec):
        # Keep Forge's HOME/USER compatibility handling, not its unconditional UID removal.
        context = spec.get('securityContext', {})
        missing = object()
        uid = context.pop('runAsUser', missing)
        try:
            original_strip(spec)
        finally:
            if uid is not missing:
                context['runAsUser'] = uid
    phase._strip_hardcoded_uid = preserve_uid

    def metadata(directory, labels, **kwargs):
        # PR-322's AIPerf parser consumes labels, not kpi_labels. Preserve its
        # existing declaration labels at source so both parsers retain identity.
        kpi_labels = kwargs.get('kpi_labels', {})
        if kpi_labels.get('benchmark_tool') == 'aiperf':
            labels = {**kpi_labels, **labels}
        return original_metadata(directory, labels, **kwargs)

    phase.create_test_metadata = metadata

    def hf_secret():
        found = set()
        def visit(value):
            if isinstance(value, dict):
                if value.get('name') == 'HF_TOKEN':
                    reference = value.get('valueFrom', {}).get('secretKeyRef', {})
                    if reference.get('key') == 'HF_TOKEN' and reference.get('name'):
                        found.add(reference['name'])
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(runtime.get('launch', {}))
        if len(found) > 1:
            raise ValueError('ambiguous HF token Secret reference')
        return next(iter(found), '')

    original_guidellm = phase.run_guidellm_benchmark.run
    def guidellm(**kwargs):
        kwargs['hf_token_secret'] = hf_secret()
        runtime['harness_invocation'] = {key: value for key, value in kwargs.items()
                                         if key not in {'endpoint_url', 'namespace', 'name'}}
        return original_guidellm(**kwargs)
    phase.run_guidellm_benchmark.run = guidellm

    from projects.aiperf.toolbox.run_aiperf_benchmark import main as aiperf
    original_aiperf, original_job = aiperf.run, aiperf.build_job_manifest
    def aiperf_run(**kwargs):
        runtime['harness_invocation'] = {key: value for key, value in kwargs.items()
                                         if key not in {'endpoint_url', 'namespace', 'name'}}
        return original_aiperf(**kwargs)
    def aiperf_job(**kwargs):
        job = original_job(**kwargs)
        secret = hf_secret()
        if secret:
            job['spec']['template']['spec']['containers'][0].setdefault('env', []).append({
                'name': 'HF_TOKEN', 'valueFrom': {'secretKeyRef': {'name': secret, 'key': 'HF_TOKEN'}}})
        return job
    aiperf.run, aiperf.build_job_manifest = aiperf_run, aiperf_job

    def write(name, manifest):
        if manifest['kind'] in {'Deployment', 'LeaderWorkerSet', 'LLMInferenceService'}:
            runtime['uid_admission'] = admit_manifest(manifest, manifest['metadata']['namespace'], admission_probe)
            runtime['launch'] = copy.deepcopy(manifest)
            # Namespaces and provider/controller metadata do not prove performance.
            runtime['launch']['metadata'] = {'name': manifest['metadata']['name']}
        return original_write(name, manifest)

    def snapshot(namespace):
        launch = runtime['launch']
        selector = ('app.kubernetes.io/name=' if launch['kind'] == 'LLMInferenceService'
                    else 'forge.openshift.io/run=') + launch['metadata']['name']
        pods = json_load(oc('get', 'pods', '-n', namespace, '-l', selector, '-o', 'json'))
        sanitized = []
        for pod in pods['items']:
            containers = [c for c in pod['spec']['containers'] if c['name'] in {'vllm', 'vllm-leader', 'vllm-worker', 'main'}]
            names = {c['name'] for c in containers}
            sanitized.append({'kind': 'Pod', 'spec': {'containers': containers,
                               'volumes': pod['spec'].get('volumes', []),
                               'securityContext': pod['spec'].get('securityContext', {}),
                               'serviceAccountName': pod['spec'].get('serviceAccountName', 'default')},
                              'status': {'containerStatuses': [
                                  {k: s[k] for k in ('name', 'ready', 'imageID') if k in s}
                                  for s in pod.get('status', {}).get('containerStatuses', []) if s['name'] in names]}})
        return {'kind': 'List', 'items': sanitized}

    def workload(*args, **kwargs):
        namespace = args[1] if args else kwargs['namespace']
        endpoint = args[0] if args else kwargs['endpoint_url']
        runtime['_private_endpoints'] = [endpoint, urlparse(endpoint).hostname]
        runtime['pods_before'] = snapshot(namespace)
        check_runtime_uids(runtime['pods_before'], runtime['uid_admission'])
        original_workload(*args, **kwargs)
        runtime['pods_after'] = snapshot(namespace)
        check_runtime_uids(runtime['pods_after'], runtime['uid_admission'])

    phase._write_launch_manifest, phase._run_workload = write, workload
    workload = WORKLOADS[request['workload']]
    workloads = private / 'workloads.yaml'
    workloads.write_text(yaml.safe_dump({workload['key']: workload['definition']}))
    phase.RHAIIS_WORKLOADS = workloads
    config = load_unique_yaml(phase.RHAIIS_CONFIG.read_text())
    config['benchmarks'][workload['harness']]['image'] = request['harness_image']
    config_path = private / 'benchmark-config.yaml'
    config_path.write_text(yaml.safe_dump(config))
    phase.RHAIIS_CONFIG = config_path


def export_native(value, endpoints, paths=None, prefix=''):
    """Source-side export redaction, never alter numeric metrics or importer inputs.

    Original bytes remain in private diagnostics. Record every redacted field and
    both byte checksums so a sanitized export is never described as unmodified.
    """
    if paths is None:
        paths = []
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            path = prefix + '/' + key
            # GuideLLM emits backend.api_key even when unset. Never export it,
            # including null/empty values; keep the privacy guard unchanged.
            if key in {'hostname', 'ip_address', 'nodeName', 'api_key'}:
                paths.append(path)
                continue
            result[key] = export_native(item, endpoints, paths, path)
        return result
    if isinstance(value, list):
        return [export_native(item, endpoints, paths, prefix + '/' + str(i)) for i, item in enumerate(value)]
    if isinstance(value, str):
        for endpoint in endpoints:
            if endpoint and endpoint in value:
                value = value.replace(endpoint, '${ENDPOINT}')
                paths.append(prefix)
    return value


def execute(request, inputs: Path, output: Path, namespace: str, context: str, forge: Path, timeout: int):
    files = {'inputs/' + relative(name): (inputs / relative(name)).read_bytes() for name in request['inputs']}
    recipe, _, _ = validate_request(request, files)
    if (forge / 'REVISION').read_text().strip() != FORGE_REVISION:
        raise ValueError('runner contains a different Forge revision')
    # Fresh output required: retries are distinct attempts, never overwritten evidence.
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError('output directory must be empty')
    outcome, runtime = 'failed', {}
    artifacts = output / 'private-diagnostics'
    artifacts.mkdir(mode=0o700)
    with tempfile.TemporaryDirectory(prefix='contributor-') as tmp:
        private = Path(tmp)
        try:
            runtime['environment'] = preflight(request, inputs, namespace, context, private)
            overrides = standalone_overrides(request, namespace)
            overrides['inference_playbooks.recipe'] = recipe['recipe_id']
            metadata = artifacts / '000__ci_metadata'
            metadata.mkdir()
            (metadata / 'variable_overrides.yaml').write_text(yaml.safe_dump(overrides))
            os.environ.update(ARTIFACT_DIR=str(artifacts), FORGE_FOREIGN_TESTING_REPO_PATH=str(inputs),
                              PYTHONPATH=str(forge), CLUSTERLESS_MODE='false')
            # Forge registers DSL tasks relative to cwd but looks them up relative
            # to FORGE_HOME. Match its launcher before importing any toolbox.
            with chdir(forge):
                sys.path.insert(0, str(forge))
                sys.path.insert(0, str(forge / 'projects/inference_playbooks/orchestration'))
                spec = importlib.util.spec_from_file_location('portable_forge_cli', forge / 'projects/inference_playbooks/orchestration/cli.py')
                cli = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cli)
                instrument(cli.test_phase, request, private, runtime)
                def deadline(_signal, _frame):
                    raise TimeoutError('benchmark attempt deadline exceeded')
                signal.signal(signal.SIGALRM, deadline)
                signal.alarm(timeout)
                try:
                    cli.main.main(args=['test'], standalone_mode=False)
                    outcome = 'succeeded'
                except SystemExit as error:
                    if error.code not in (None, 0):
                        raise
                    outcome = 'succeeded'
        except KeyboardInterrupt:
            outcome = 'interrupted'
        except SystemExit as error:
            outcome = 'interrupted' if type(error).__name__ == 'SignalInterrupt' else 'failed'
        except Exception:
            # Exception text and Forge logs can contain private endpoints/credentials.
            outcome = 'failed'
            (artifacts / 'execution-error.txt').write_text(traceback.format_exc())
            print('Execution failed; inspect private-diagnostics locally, not for sharing.', file=sys.stderr)
        finally:
            signal.alarm(0)
        if outcome == 'succeeded':
            try:
                kpis = list(artifacts.glob('*__postprocessing/kpis/kpis.json'))
                if len(kpis) != 1:
                    raise ValueError('missing or ambiguous current KPI artifact')
                files['kpis.json'] = kpis[0].read_bytes()
                runtime['command'] = 'python /opt/forge/projects/inference_playbooks/orchestration/cli.py test'
                files['result.json'] = json_bytes(replay(request, files))
                patterns = ('**/*__run_guidellm_benchmark/artifacts/results/benchmarks*.json',
                            '**/*__run_aiperf_benchmark/artifacts/aiperf/profile_export_aiperf.json')
                raw = sorted({p for pattern in patterns for p in artifacts.glob(pattern)})
                if not raw:
                    raise ValueError('native benchmark exports missing')
                endpoints = runtime.pop('_private_endpoints', [])
                runtime['native_exports'] = []
                for index, path in enumerate(raw):
                    original = path.read_bytes()
                    redactions = []
                    value = export_native(json_load(original), endpoints, redactions)
                    shareable(value)
                    name = f'raw/harness-{index}.json'
                    files[name] = json_bytes(value)
                    runtime['native_exports'].append({'path': name, 'original_checksum': checksum(original),
                                                      'export_checksum': checksum(files[name]),
                                                      'redacted_fields': sorted(set(redactions))})
                shareable(runtime)
                files['runtime.json'] = json_bytes(runtime)
            except Exception:
                outcome = 'collection-failed'
                (artifacts / 'collection-error.txt').write_text(traceback.format_exc())
                files = {name: data for name, data in files.items() if name.startswith('inputs/')}
                print('Evidence collection/privacy check failed; private diagnostics remain local.', file=sys.stderr)
        archive = output / 'evidence.tar.gz'
        digest = write_archive(archive, request, outcome, files)
        print(json.dumps({'outcome': outcome, 'bundle': str(archive), 'checksum': digest}))
    return 0 if outcome == 'succeeded' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, default=Path('/inputs/request.json'))
    parser.add_argument('--inputs', type=Path, default=Path('/inputs/playbooks'))
    parser.add_argument('--output', type=Path, default=Path('/outputs'))
    parser.add_argument('--forge', type=Path, default=Path('/opt/forge'))
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--context', required=True)
    parser.add_argument('--timeout', type=int, default=21600)
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error('timeout must be positive')
    try:
        return execute(json_load(args.request.read_bytes()), args.inputs, args.output,
                       args.namespace, args.context, args.forge, args.timeout)
    except (ValueError, KeyError, OSError) as error:
        print(f'Cannot start contributor run: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
