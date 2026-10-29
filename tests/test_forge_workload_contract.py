"""Sweep intent comes from producer profiles/Jobs, never observed KPI points."""
import copy
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from forge_workload_contract import invocation_levels, replay_expectations, workload_expectations
from normalize_kpis import normalize


@pytest.fixture
def planning(monkeypatch):
    profiles = {'guide': {'rates': [1, 4, 16, 32]},
                'ai': {'tool': 'aiperf', 'concurrency': [4, 8, 16, 32]},
                'other': {'rates': [2, 6, 10]}}
    monkeypatch.setattr('forge_workload_contract.fetch_profiles', lambda sha: (
        f'https://raw.githubusercontent.com/openshift-psap/forge/{sha}/workloads.yaml',
        yaml.safe_dump(profiles).encode()))
    artifacts, steps, tests = {}, {}, []
    for index, (tool, key, point) in enumerate([('guidellm', 'guide', None),
                                               *(('aiperf', 'ai', c) for c in [4, 8, 16, 32])], 1):
        run_id = f'001__inference_playbooks_test_dir/{index:03d}__benchmark_{key}'
        directory = '01__test/' + run_id
        step = directory + f'/001__run_{tool}_benchmark'
        steps[directory] = step
        labels = {'benchmark_tool': tool, 'benchmark_key': key}
        if point is not None:
            labels['concurrency'] = str(point)
        artifacts[directory + '/__caliper_test_metadata__.yaml'] = {
            'labels': {'phase': 'benchmark'}, 'kpi_labels': labels}
        if tool == 'guidellm':
            container = {'command': ['/bin/sh', '-c'], 'args': [
                "set -eux\n/opt/app-root/bin/guidellm run '--profile={\"kind\":\"concurrent\",\"streams\":[1,4,16,32]}'\n"]}
            kpi = {'kpi_id': 'playbooks_output_tok_per_sec', 'is_curve': True,
                   'values': [[c, 100] for c in [1, 4, 16, 32]]}
        else:
            container = {'command': ['aiperf', 'profile', '--concurrency', str(point)]}
            kpi = {'kpi_id': 'aiperf_output_token_throughput_avg', 'is_curve': False, 'value': 100}
        artifacts[step + f'/src/{tool}-job.yaml'] = {
            'kind': 'Job', 'spec': {'template': {'spec': {'containers': [container]}}}}
        tests.append({'run_id': run_id, 'labels': copy.deepcopy(labels), 'kpis': [kpi]})

    class Client:
        def request(self, endpoint, body=None, raw=False):
            query = parse_qs(urlsplit(endpoint).query)
            assert query['run_id'] == ['a' * 32]
            path = query['path'][0]
            if endpoint.startswith('/get-artifact?'):
                assert raw
                return yaml.safe_dump(artifacts[path]).encode()
            return {'files': [{'path': steps[path], 'is_dir': True}]}

    return Client(), {'tests': tests}, artifacts, profiles


def derive(planning, selected='guide,ai'):
    client, document, _, _ = planning
    return workload_expectations(client, 'a' * 32, 'b' * 40, selected, document)


def results(document, expected):
    return normalize(document, run_id='fixture', deployment_scope='single-node',
                     accelerator_key='nvidia-h200-x8', expected_concurrencies=expected)


def test_profile_and_job_derived_full_sweeps(planning):
    snapshot, expected = derive(planning)
    assert expected == {'guidellm': [1, 4, 16, 32], 'aiperf': [4, 8, 16, 32]}
    assert all(result['completeness'] == 'full-sweep' for result in results(planning[1], expected).values())
    assert replay_expectations(json.loads(json.dumps(snapshot)), planning[1]) == expected
    assert snapshot['forge_revision'] == 'b' * 40
    assert snapshot['profile_source']['checksum'].startswith('sha256:')
    assert all(i['checksum'].startswith('sha256:') for w in snapshot['workloads'] for i in w['invocations'])


@pytest.mark.parametrize('selected', [' guide , ai, ', [' guide ', 'ai', '']])
def test_effective_workload_selection_whitespace_matches_producer(planning, selected):
    _, expected = derive(planning, selected)
    assert expected == {'guidellm': [1, 4, 16, 32], 'aiperf': [4, 8, 16, 32]}


def test_missing_kpi_point_does_not_shrink_expected_sweep(planning):
    planning[1]['tests'].pop()  # Last AIPerf point never produced a KPI.
    _, expected = derive(planning)
    assert expected['aiperf'] == [4, 8, 16, 32]
    assert results(planning[1], expected)['aiperf']['completeness'] == 'partial'


def test_effective_guidellm_job_override_wins_over_default_profile(planning):
    _, document, artifacts, _ = planning
    job = next(v for p, v in artifacts.items() if p.endswith('/guidellm-job.yaml'))
    job['spec']['template']['spec']['containers'][0]['args'][0] = (
        'guidellm run --profile=kind=concurrent,streams=7\n')
    snapshot, expected = derive(planning)
    assert snapshot['workloads'][0]['profile_concurrencies'] == [1, 4, 16, 32]
    assert expected['guidellm'] == [7]
    # We do not declare the old observed points complete against the new command.
    assert results(document, expected)['guidellm']['completeness'] == 'partial'


def test_effective_comment_workload_selection_uses_selected_profile(planning):
    _, document, artifacts, _ = planning
    guide = document['tests'][0]
    guide['labels']['benchmark_key'] = 'other'
    directory = '01__test/' + guide['run_id']
    artifacts[directory + '/__caliper_test_metadata__.yaml']['kpi_labels']['benchmark_key'] = 'other'
    job = next(v for p, v in artifacts.items() if p.endswith('/guidellm-job.yaml'))
    job['spec']['template']['spec']['containers'][0]['args'][0] = (
        'guidellm run \'--profile={"kind":"concurrent","streams":[2,6,10]}\'\n')
    guide['kpis'][0]['values'] = [[c, 100] for c in [2, 6, 10]]
    snapshot, expected = derive(planning, ['other', 'ai'])
    assert snapshot['workloads'][0]['key'] == 'other'
    assert expected['guidellm'] == [2, 6, 10]
    assert results(document, expected)['guidellm']['completeness'] == 'full-sweep'


def test_replay_does_not_read_todays_profile_defaults(planning, monkeypatch):
    snapshot, expected = derive(planning)
    monkeypatch.setattr('forge_workload_contract.fetch_profiles', lambda _: pytest.fail('offline replay contacted GitHub'))
    planning[3]['ai']['concurrency'] = [999]
    assert replay_expectations(snapshot, planning[1]) == expected


@pytest.mark.parametrize('failure', ['missing-profile', 'missing-harness', 'wrong-key', 'axis',
                                    'duplicate-flag', 'short-override', 'config-file', 'ai-override', 'traversal'])
def test_ambiguous_or_unattributed_expectations_fail(planning, failure):
    _, document, artifacts, profiles = planning
    if failure == 'missing-profile':
        del profiles['ai']
    elif failure == 'missing-harness':
        document['tests'] = document['tests'][:1]
    elif failure == 'wrong-key':
        document['tests'][0]['labels']['benchmark_key'] = 'other'
    elif failure == 'axis':
        job = next(v for p, v in artifacts.items() if p.endswith('/guidellm-job.yaml'))
        job['spec']['template']['spec']['containers'][0]['args'][0] = 'guidellm run --profile=kind=constant,rate=4'
    elif failure in {'duplicate-flag', 'short-override', 'config-file', 'ai-override'}:
        job = next(v for p, v in artifacts.items() if p.endswith('/aiperf-job.yaml'))
        command = job['spec']['template']['spec']['containers'][0]['command']
        if failure == 'duplicate-flag':
            command += ['--concurrency=99']
        elif failure == 'short-override':
            command += ['-c', '99']
        elif failure == 'config-file':
            command += ['--config=unresolved.yaml']
        else:
            command[-1] = '99'
    else:
        document['tests'][0]['run_id'] = '../outside'
    with pytest.raises(ValueError):
        derive(planning)


@pytest.mark.parametrize('value', [0, -1, True, [], [1, 1], [1.5], None])
def test_invalid_profile_levels_are_not_rounded_into_a_plan(planning, value):
    planning[3]['ai']['concurrency'] = value
    with pytest.raises(ValueError):
        derive(planning)
