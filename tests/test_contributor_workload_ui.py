"""Recorded workloads, not recipe labels, control contributor evidence filtering."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

REPO = Path(__file__).resolve().parents[1]
PLATFORM = {'stack': 'vllm', 'version': 'v1'}


def groups(run):
    if not shutil.which('node'):
        pytest.skip('Node.js is required to exercise catalog workload filtering')
    source = (REPO / 'site/app.js').read_text()
    functions = source[source.index('function workloadGroup('):source.index('function dimension(')]
    entry = {'platform': PLATFORM, 'workload_profile': 'unclassified', 'evidence': [{'run': run}]}
    script = functions + '\nconst entry = ' + json.dumps(entry) + ''';
console.log(JSON.stringify({groups: evidenceWorkloads(entry.evidence[0], entry),
  discover8k: matchesWorkload(entry, '8k1k'), discoverAgentic: matchesWorkload(entry, 'agentic')}));
'''
    result = subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def contributor(key='IP-8k/1k', harness='guidellm'):
    return {'evidence_source': 'contributor-forge', 'harness': harness, 'platform': PLATFORM,
            'workload': {'key': key, 'harness': harness, 'definition': {}}}


def test_contributor_8k_evidence_is_discoverable_and_visible_under_filter():
    assert groups(contributor()) == {'groups': ['8k1k'], 'discover8k': True, 'discoverAgentic': False}


@pytest.mark.parametrize('key,harness', [('ci-quick', 'guidellm'), ('aiperf-smoke', 'aiperf'),
                                       ('unknown', 'guidellm')])
def test_smoke_and_unknown_contributor_workloads_do_not_confer_coverage(key, harness):
    assert groups(contributor(key, harness)) == {'groups': [], 'discover8k': False, 'discoverAgentic': False}


@pytest.mark.parametrize('change', ['stack', 'version', 'harness', 'source', 'missing-workload'])
def test_contributor_filter_requires_matching_recorded_attribution(change):
    run = contributor()
    if change in {'stack', 'version'}:
        run['platform'] = {**PLATFORM, change: 'different'}
    elif change == 'harness':
        run['workload']['harness'] = 'aiperf'
    elif change == 'source':
        run['evidence_source'] = 'local-guidellm'
    else:
        run.pop('workload')
    assert groups(run)['groups'] == []


@pytest.mark.parametrize('harness,expected', [('guidellm', ['8k1k']), ('aiperf', ['agentic'])])
def test_forge_expectation_snapshot_still_filters_by_its_own_harness(harness, expected):
    run = {'evidence_source': 'forge-kpis', 'harness': harness, 'platform': PLATFORM,
           'workload': {'expectations': {'workloads': [
               {'harness': 'guidellm', 'key': 'IP-8k/1k'},
               {'harness': 'aiperf', 'key': 'aiperf-agentx-128k'}]}}}
    assert groups(run)['groups'] == expected
