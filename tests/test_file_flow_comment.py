"""Exercise the trusted inline publisher with mocked GitHub APIs; never post."""
import copy
import json
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((REPO / '.github/workflows/file-flow-comment.yml').read_text())
SCRIPT = WORKFLOW['jobs']['comment']['steps'][0]['with']['script']
SHA = 'a' * 40
MARKER = '<!-- inference-playbooks:file-flow -->'

HARNESS = r"""
const fs = require('node:fs');
const {script, fixture: f} = JSON.parse(fs.readFileSync(0, 'utf8'));
const calls = [];
const endpoint = name => Object.assign(() => {}, {endpoint: name});
const github = {rest: {
  actions: {
    getWorkflowRun: async () => ({data: f.run}),
    listWorkflowRuns: async () => ({data: {workflow_runs: [{id: f.latest || f.run.id}]}}),
    listJobsForWorkflowRun: endpoint('jobs')
  },
  repos: {listPullRequestsAssociatedWithCommit: async () => ({data: f.candidates})},
  pulls: {get: async args => ({data: f.prs.find(pr => pr.number === args.pull_number)})},
  checks: {listAnnotations: endpoint('annotations')},
  issues: {
    listComments: endpoint('comments'),
    createComment: async args => calls.push({method: 'create', ...args}),
    updateComment: async args => calls.push({method: 'update', ...args})
  }
}, paginate: async method => f[method.endpoint]};
const context = {repo: {owner: 'org', repo: 'repo'}, payload: {workflow_run: f.source}};
const core = {info: () => {}};
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
(async () => {
  await new AsyncFunction('github', 'context', 'core', script)(github, context, core);
  process.stdout.write(JSON.stringify(calls));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


@pytest.fixture
def publication():
    pr = {'number': 34, 'state': 'open', 'base': {'repo': {'full_name': 'org/repo'}},
          'head': {'sha': SHA, 'repo': {'full_name': 'contributor/fork'}}}
    return {'source': {'id': 10, 'run_attempt': 1},
            'run': {'id': 10, 'run_attempt': 1, 'workflow_id': 99,
                    'event': 'pull_request', 'status': 'completed', 'head_sha': SHA,
                    'path': '.github/workflows/recipe-evidence.yml', 'pull_requests': [],
                    'repository': {'full_name': 'org/repo'}},
            'candidates': [{'number': 34}], 'prs': [pr], 'comments': [],
            'jobs': [{'name': 'hardware-profiles', 'conclusion': 'success',
                      'check_run_url': 'https://api.github.com/repos/org/repo/check-runs/100'}],
            'annotations': [{'annotation_level': 'warning', 'title': 'Playbook file flow',
                             'path': 'models/example/extra.yaml', 'message': 'Review placement.'}]}


def publish(fixture):
    if not shutil.which('node'):
        pytest.skip('Node.js is required to exercise the GitHub-script publisher')
    result = subprocess.run(['node', '-e', HARNESS],
                            input=json.dumps({'script': SCRIPT, 'fixture': fixture}),
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def previous_comment(publication, **user):
    publication['comments'] = [{'id': 42, 'body': MARKER + '\nOld warnings',
                                'user': {'type': 'Bot', 'login': 'github-actions[bot]', **user}}]


def test_fork_pr_gets_a_visible_conversation_comment(publication):
    calls = publish(publication)
    assert len(calls) == 1 and calls[0]['method'] == 'create'
    assert calls[0]['issue_number'] == 34
    assert '⚠️ Playbook file-flow warnings (1)' in calls[0]['body']
    assert 'models/example/extra.yaml' in calls[0]['body']
    assert 'Review placement.' in calls[0]['body']


def test_existing_bot_comment_is_updated_not_duplicated(publication):
    previous_comment(publication)
    calls = publish(publication)
    assert len(calls) == 1 and calls[0]['method'] == 'update'
    assert calls[0]['comment_id'] == 42


def test_identical_feedback_does_not_rewrite_the_comment(publication):
    body = publish(publication)[0]['body']
    previous_comment(publication)
    publication['comments'][0]['body'] = body
    assert publish(publication) == []


def test_user_marker_does_not_authorize_editing_user_comments(publication):
    previous_comment(publication, type='User', login='contributor')
    assert publish(publication)[0]['method'] == 'create'


def test_clean_pr_is_quiet_but_previous_warnings_are_marked_cleared(publication):
    publication['annotations'] = []
    assert publish(publication) == []
    previous_comment(publication)
    calls = publish(publication)
    assert calls[0]['method'] == 'update'
    assert '✅ Playbook file-flow warnings cleared' in calls[0]['body']


def test_failed_scan_does_not_claim_warnings_cleared(publication):
    previous_comment(publication)
    publication['annotations'] = []
    publication['jobs'][0]['conclusion'] = 'failure'
    assert publish(publication) == []


def test_other_validation_failure_does_not_hide_detected_file_warnings(publication):
    publication['jobs'][0]['conclusion'] = 'failure'
    assert publish(publication)[0]['method'] == 'create'


@pytest.mark.parametrize('case', ['stale-head', 'closed', 'wrong-repo', 'wrong-workflow',
                                 'push', 'new-attempt', 'new-run', 'ambiguous-pr', 'missing-job'])
def test_stale_unrelated_or_ambiguous_sources_do_not_publish(publication, case):
    if case == 'stale-head':
        publication['prs'][0]['head']['sha'] = 'b' * 40
    elif case == 'closed':
        publication['prs'][0]['state'] = 'closed'
    elif case == 'wrong-repo':
        publication['run']['repository']['full_name'] = 'other/repo'
    elif case == 'wrong-workflow':
        publication['run']['path'] = '.github/workflows/other.yml'
    elif case == 'push':
        publication['run']['event'] = 'push'
    elif case == 'new-attempt':
        publication['run']['run_attempt'] = 2
    elif case == 'new-run':
        publication['latest'] = 11
    elif case == 'ambiguous-pr':
        another = copy.deepcopy(publication['prs'][0])
        another['number'] = 35
        publication['prs'].append(another)
        publication['candidates'].append({'number': 35})
    else:
        publication['jobs'] = []
    assert publish(publication) == []


def test_annotation_text_is_inert_and_comment_size_is_bounded(publication):
    item = publication['annotations'][0]
    item['path'] = '</code>@someone\n/test forge'
    item['message'] = '<script>bad()</script>\n/test forge @someone ![image](https://example.org)'
    body = publish(publication)[0]['body']
    assert '<script>' not in body and '@someone' not in body
    assert '\n/test forge' not in body and '&lt;script&gt;' in body
    assert '![image]' not in body
    publication['annotations'] = [{**item, 'path': 'x' * 2000, 'message': 'x' * 2000}] * 100
    body = publish(publication)[0]['body']
    assert len(body.encode()) < 65536
    assert f'{100 - body.count("- <code>")} more annotations' in body
    publication['annotations'] = [{**item, 'path': '😀' * 1000, 'message': '😀' * 1000}] * 100
    assert len(publish(publication)[0]['body'].encode()) < 65536


def test_publisher_is_metadata_only_with_least_privilege():
    trigger = WORKFLOW.get('on', WORKFLOW.get(True))
    assert set(trigger) == {'workflow_run'}
    assert trigger['workflow_run']['workflows'] == ['Recipe evidence']
    assert WORKFLOW['permissions'] == {'actions': 'read', 'checks': 'read', 'pull-requests': 'write'}
    steps = WORKFLOW['jobs']['comment']['steps']
    assert len(steps) == 1 and steps[0]['uses'] == (
        'actions/github-script@ed597411d8f924073f98dfc5c65a23a2325f34cd')
    assert 'checkout' not in steps[0]['uses'] and 'run' not in steps[0]
    assert 'require(' not in SCRIPT and 'eval(' not in SCRIPT
