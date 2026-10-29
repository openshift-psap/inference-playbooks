"""Expected sweep points from pinned profiles and recorded benchmark invocations."""
import hashlib
import json
import re
import shlex
from functools import lru_cache
from urllib.parse import urlencode
from urllib.request import build_opener

from forge_bundle_contract import yaml_data


PROFILE_PATH = 'projects/rhaiis/orchestration/config.d/workloads.yaml'


@lru_cache(maxsize=16)
def fetch_profiles(revision):
    from fetch_forge_evidence import NoRedirect
    if not isinstance(revision, str) or not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('workload profiles require the recorded Forge revision')
    url = f'https://raw.githubusercontent.com/openshift-psap/forge/{revision}/{PROFILE_PATH}'
    # Never send MLflow credentials to the producer source repository.
    with build_opener(NoRedirect()).open(url, timeout=30) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError('Forge workload profile exceeds intake limit')
    return url, raw


def levels(value):
    values = value if isinstance(value, list) else [value]
    if not values or any(type(v) is not int or v < 1 for v in values) or len(set(values)) != len(values):
        raise ValueError('expected workload concurrency must contain unique positive integers')
    return sorted(values)


def selection(value):
    values = value.split(',') if isinstance(value, str) else value
    if not isinstance(values, list) or any(not isinstance(key, str) for key in values):
        raise ValueError('ambiguous recorded workload selection')
    keys = [key.strip() for key in values if key.strip()]
    if not keys or len(set(keys)) != len(keys):
        raise ValueError('ambiguous recorded workload selection')
    return keys


def invocation_levels(job, harness):
    """Parse the generated Job as data. Never execute its shell or copy endpoints."""
    if job.get('kind') != 'Job':
        raise ValueError('benchmark invocation artifact must be a Job')
    commands = []
    for container in job['spec']['template']['spec']['containers']:
        command, args = container.get('command', []), container.get('args', [])
        if command and command[0].rsplit('/', 1)[-1] == harness:
            commands.append([*command, *args])
        elif command and command[0] in {'/bin/sh', '/bin/bash', 'sh', 'bash'}:
            for script in args:
                for line in script.splitlines():
                    if re.match(r'^\s*(?:/[^\s]+/)?' + harness + r'\s+(?:run|profile)\s', line):
                        commands.append(shlex.split(line))
    if len(commands) != 1:
        raise ValueError('benchmark invocation is missing or ambiguous')
    argv = commands[0]
    if any(arg == '--config' or arg.startswith('--config=') for arg in argv):
        raise ValueError('benchmark config-file overrides require a resolved execution profile')
    flag = '--profile' if harness == 'guidellm' else '--concurrency'
    aliases = {flag, '-c'} if harness == 'aiperf' else {flag}
    values = []
    for index, arg in enumerate(argv):
        if arg.startswith(flag + '='):
            values.append(arg.partition('=')[2])
        elif arg in aliases and index + 1 < len(argv):
            values.append(argv[index + 1])
    if len(values) != 1:
        raise ValueError('benchmark concurrency override is missing or ambiguous')
    if harness == 'aiperf':
        if not re.fullmatch('[0-9]+', values[0]):
            raise ValueError('unsupported AIPerf concurrency argument')
        return levels(int(values[0]))
    value = values[0]
    if value.startswith('{'):
        profile = json.loads(value)
    else:
        pairs = [part.split('=', 1) for part in value.split(',')]
        if any(len(pair) != 2 for pair in pairs) or len({p[0] for p in pairs}) != len(pairs):
            raise ValueError('ambiguous GuideLLM profile argument')
        profile = dict(pairs)
        if re.fullmatch('[0-9]+', profile.get('streams', '')):
            profile['streams'] = int(profile['streams'])
    if profile.get('kind') != 'concurrent':
        raise ValueError('GuideLLM workload is not a concurrency profile')
    return levels(profile.get('streams'))


def replay_expectations(snapshot, document):
    """Offline derivation from the persisted, sanitized execution-plan snapshot."""
    if not isinstance(snapshot, dict) or type(snapshot.get('version')) is not int or snapshot['version'] != 1:
        raise ValueError('unsupported workload expectation snapshot')
    if not isinstance(snapshot.get('workloads'), list) or not snapshot['workloads']:
        raise ValueError('workload expectation snapshot requires selected workloads')
    expected, seen = {}, set()
    supported = [test for test in document['tests']
                 if test.get('labels', {}).get('benchmark_tool') in {'guidellm', 'aiperf'}]
    tests = {test['run_id']: test for test in supported}
    if len(tests) != len(supported):
        raise ValueError('duplicate KPI test identities')
    for workload in snapshot['workloads']:
        harness = workload['harness']
        if harness not in {'guidellm', 'aiperf'} or harness in expected:
            raise ValueError('multiple workloads per harness are unsupported')
        planned = levels(workload['profile_concurrencies'])
        invocations = workload['invocations']
        if not invocations:
            raise ValueError('selected workload has no benchmark evidence')
        if harness == 'guidellm':
            if len(invocations) != 1:
                raise ValueError('GuideLLM sweep invocation is missing or ambiguous')
            # The generated Job contains the full effective profile after overrides.
            planned = levels(invocations[0]['concurrencies'])
        for invocation in invocations:
            run_id = invocation['test_run_id']
            if run_id in seen or run_id not in tests:
                raise ValueError('workload invocation does not identify a unique KPI test')
            seen.add(run_id)
            labels = tests[run_id]['labels']
            if (labels['benchmark_tool'] != harness or
                    labels.get('benchmark_key', workload['key']) != workload['key']):
                raise ValueError('workload profile disagrees with KPI attribution')
            actual = levels(invocation['concurrencies'])
            if harness == 'aiperf':
                if len(actual) != 1 or str(actual[0]) != str(labels.get('concurrency')):
                    raise ValueError('AIPerf invocation concurrency disagrees with KPI attribution')
                if actual[0] not in planned:
                    raise ValueError('AIPerf invocation differs from the recorded workload profile')
        expected[harness] = planned
    if seen != set(tests):
        raise ValueError('KPI tests are missing workload attribution')
    return expected


def workload_expectations(client, parent_id, revision, selected, document):
    """Use the effective selection, immutable producer defaults and actual Jobs."""
    url, raw = fetch_profiles(revision)
    profiles = yaml_data(raw.decode())
    workloads = []
    for key in selection(selected):
        if key not in profiles:
            raise ValueError('recorded workload is absent from the pinned Forge profiles')
        profile = profiles[key]
        harness = profile.get('tool', 'guidellm')
        if harness not in {'guidellm', 'aiperf'}:
            raise ValueError('unsupported workload harness')
        workloads.append({'key': key, 'harness': harness,
                          'profile_concurrencies': levels(profile.get('rates' if harness == 'guidellm' else 'concurrency')),
                          'invocations': []})
    if len({w['harness'] for w in workloads}) != len(workloads):
        raise ValueError('multiple workloads per harness are unsupported')

    def artifact(path):
        return client.request('/get-artifact?' + urlencode({'run_id': parent_id, 'path': path}), raw=True)

    for test in document['tests']:
        harness = test.get('labels', {}).get('benchmark_tool')
        if harness not in {'guidellm', 'aiperf'}:
            continue
        run_id = test.get('run_id')
        if not isinstance(run_id, str) or not re.fullmatch(
                r'001__inference_playbooks_test_dir/\d+__benchmark_[A-Za-z0-9_.-]+', run_id):
            raise ValueError('KPI test needs a canonical benchmark artifact path')
        directory = '01__test/' + run_id
        metadata = yaml_data(artifact(directory + '/__caliper_test_metadata__.yaml').decode())
        labels = {**metadata.get('kpi_labels', {}), **metadata.get('labels', {})}
        matches = [w for w in workloads if w['harness'] == harness and w['key'] == labels.get('benchmark_key')]
        if labels.get('phase') != 'benchmark' or len(matches) != 1:
            raise ValueError('benchmark metadata disagrees with the selected workload')
        listing = client.request('/api/2.0/mlflow/artifacts/list?' + urlencode({'run_id': parent_id, 'path': directory}))
        steps = [item['path'] for item in listing.get('files', []) if item.get('is_dir') and
                 re.fullmatch(re.escape(directory) + r'/\d+__run_' + harness + '_benchmark', item['path'])]
        if len(steps) != 1:
            raise ValueError('benchmark Job artifact is missing or ambiguous')
        job_path = steps[0] + f'/src/{harness}-job.yaml'
        job_raw = artifact(job_path)
        matches[0]['invocations'].append({'test_run_id': run_id, 'artifact_path': job_path,
                                         'checksum': 'sha256:' + hashlib.sha256(job_raw).hexdigest(),
                                         'concurrencies': invocation_levels(yaml_data(job_raw.decode()), harness)})
    snapshot = {'version': 1, 'forge_revision': revision,
                'profile_source': {'uri': url, 'checksum': 'sha256:' + hashlib.sha256(raw).hexdigest()},
                'workloads': workloads}
    return snapshot, replay_expectations(snapshot, document)
