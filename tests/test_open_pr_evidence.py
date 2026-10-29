"""Open-PR intake, maintainer iterations and atomic publication boundaries."""
import base64
import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from test_forge_evidence import (REPO, commit_evidence, forge_report, intake, kpis,
                                 parent_intake, record_launch, variant_intake)
import open_pr_evidence as workflow


@pytest.fixture
def open_intake(parent_intake, monkeypatch, tmp_path):
    source, path, revision, client, parent, artifacts, raw = parent_intake
    repository = 'openshift-psap/inference-playbooks'
    comment = {**forge_report(), 'issue_url': f'https://api.github.com/repos/{repository}/issues/34'}
    pr = {'state': 'open', 'merged_at': None,
          'base': {'ref': 'main', 'repo': {'full_name': repository}},
          'head': {'ref': 'contributor/recipe', 'sha': revision, 'repo': {'full_name': repository}}}
    state = {'source': source, 'pr': pr, 'permission': 'write', 'comments': {123: comment},
             'api_calls': [], 'mutations': [], 'validation': []}

    def github(endpoint):
        state['api_calls'].append(endpoint)
        if endpoint.endswith('/permission'):
            return {'permission': state['permission']}
        if endpoint.endswith('/pulls/34'):
            return copy.deepcopy(state['pr'])
        if '/issues/comments/' in endpoint:
            return copy.deepcopy(state['comments'][int(endpoint.rsplit('/', 1)[1])])
        pytest.fail('unexpected GitHub request: ' + endpoint)

    def checkout(repository, number, head, repo):
        subprocess.run(['git', 'clone', '--quiet', '--no-hardlinks', str(state['source']), str(repo)], check=True)
        assert workflow.git(repo, 'rev-parse', 'HEAD').stdout.decode().strip() == head

    monkeypatch.setattr(workflow, 'github', github)
    monkeypatch.setattr(workflow, 'checkout_candidate', checkout)
    monkeypatch.setattr(workflow, 'Client', lambda *args, **kwargs: client)
    monkeypatch.setattr(workflow, 'TRUSTED', source)
    monkeypatch.setattr(workflow, 'validate_candidate', lambda repo, trusted: state['validation'].append(repo))

    def graphql(payload):
        state['mutations'].append(payload)
        return {'createCommitOnBranch': {'commit': {'oid': 'c' * 40, 'url': 'https://github.com/example/commit/' + 'c' * 40}}}

    monkeypatch.setattr(workflow, 'graphql', graphql)
    for key, value in {'MLFLOW_TRACKING_URI': client.uri, 'MLFLOW_WORKSPACE': 'forge-sandbox',
                       'MLFLOW_USERNAME': 'test-user', 'MLFLOW_PASSWORD': 'test-password'}.items():
        monkeypatch.setenv(key, value)
    return state, repository, source, path, revision, client, parent, artifacts


def prepare(fixture, output='prepared', comment_id=123):
    state, repository, source, *_ = fixture
    destination = source / output
    return workflow.prepare(repository, 34, comment_id, destination, 'maintainer'), destination


def test_explicit_report_import_and_atomic_existing_branch_publication(open_intake):
    state, repository, source, path, revision, *_ = open_intake
    plan, output = prepare(open_intake)
    assert plan['mode'] == 'publish'
    assert plan['tested_revision'] == plan['head_sha'] == revision
    assert state['validation'] == [output / 'checkout']
    assert not any(endpoint.endswith('/comments') for endpoint in state['api_calls'])
    assert (output / 'evidence.patch').read_text().startswith('diff --git ')
    assert not (path.parent / 'results').exists()  # source branch remains untouched
    commit = workflow.publish(plan, 'maintainer')
    assert commit['oid'] == 'c' * 40
    mutation = state['mutations'][0]['variables']['input']
    assert mutation['expectedHeadOid'] == revision
    assert mutation['branch'] == {'repositoryNameWithOwner': repository, 'branchName': 'contributor/recipe'}
    assert set(mutation['fileChanges']) == {'additions'}
    assert all(workflow.evidence_path(item['path']) for item in mutation['fileChanges']['additions'])
    additions = {item['path']: item for item in mutation['fileChanges']['additions']}
    recipe_path = path.relative_to(source).as_posix()
    recipe = yaml.safe_load(base64.b64decode(additions[recipe_path]['contents']))
    for reference in recipe['benchmark_runs']:
        run_dir = Path(recipe_path).parent / Path(reference).parent
        assert {str(run_dir / name) for name in workflow.FILES} <= additions.keys()
    assert any(name.endswith('/' + workflow.RECEIPT) for name in additions)


@pytest.mark.parametrize('pattern', ['**/results/', '**/result.json', '**/forge-import.json'])
def test_ignored_evidence_rejected_before_publication(open_intake, pattern):
    state, _, source, *_ = open_intake
    (source / '.gitignore').write_text(pattern + '\n')
    commit_evidence(source)
    state['pr']['head']['sha'] = workflow.git(source, 'rev-parse', 'HEAD').stdout.decode().strip()
    with pytest.raises(ValueError, match='imported evidence is not collectable'):
        prepare(open_intake)
    assert not (source / 'prepared/plan.json').exists()
    assert not state['mutations']


def test_tooling_merge_does_not_require_another_benchmark(open_intake):
    state, _, source, _, revision, *_ = open_intake
    (source / 'tooling-change.txt').write_text('new importer\n')
    commit_evidence(source)
    state['pr']['head']['sha'] = workflow.git(source, 'rev-parse', 'HEAD').stdout.decode().strip()
    plan, _ = prepare(open_intake)
    assert plan['head_sha'] != revision
    assert plan['tested_revision'] == revision
    assert plan['mode'] == 'publish'


@pytest.mark.parametrize('failure', ['closed', 'merged', 'main-head', 'no-permission', 'wrong-pr', 'untrusted', 'failed-report', 'failed-run', 'unrelated-sha'])
def test_invalid_requests_do_not_publish(open_intake, failure):
    state, repository, source, _, revision, client, parent, artifacts = open_intake
    if failure == 'closed':
        state['pr']['state'] = 'closed'
    elif failure == 'merged':
        state['pr']['merged_at'] = '2026-10-29T00:00:00Z'
    elif failure == 'main-head':
        state['pr']['head']['ref'] = 'main'
    elif failure == 'no-permission':
        state['permission'] = 'read'
    elif failure == 'wrong-pr':
        state['comments'][123]['issue_url'] = f'https://api.github.com/repos/{repository}/issues/35'
    elif failure == 'untrusted':
        state['comments'][123]['user']['login'] = 'contributor'
    elif failure == 'failed-report':
        state['comments'][123]['body'] = 'Failed\n' + state['comments'][123]['body']
    elif failure == 'failed-run':
        parent['info']['status'] = 'FAILED'
    else:
        key = '01__test/000__ci_metadata/fournos_fjob.yaml'
        document = yaml.safe_load(artifacts[key])
        document['spec']['env']['FORGE_FOREIGN_TESTING_PULL_PULL_SHA'] = 'f' * 40
        artifacts[key] = yaml.safe_dump(document)
    with pytest.raises(ValueError):
        prepare(open_intake)
    assert not state['mutations']
    assert not (source / 'prepared/plan.json').exists()


@pytest.mark.parametrize('change', ['head', 'branch', 'closed', 'report'])
def test_publication_rechecks_live_pr_and_report(open_intake, change):
    state, *_ = open_intake
    plan, _ = prepare(open_intake)
    if change == 'head':
        state['pr']['head']['sha'] = 'e' * 40
    elif change == 'branch':
        state['pr']['head']['ref'] = 'another-branch'
    elif change == 'closed':
        state['pr']['state'] = 'closed'
    else:
        state['comments'][123]['body'] += '\nEdited report'
    with pytest.raises(ValueError):
        workflow.publish(plan, 'maintainer')
    assert not state['mutations']


def test_race_after_recheck_is_not_retried_or_force_pushed(open_intake, monkeypatch):
    state, *_ = open_intake
    plan, output = prepare(open_intake)

    def raced(payload):
        state['mutations'].append(payload)
        assert payload['variables']['input']['expectedHeadOid'] == plan['head_sha']
        raise ValueError('expectedHeadOid no longer matches')

    monkeypatch.setattr(workflow, 'graphql', raced)
    with pytest.raises(ValueError, match='expectedHeadOid'):
        workflow.publish(plan, 'maintainer')
    assert len(state['mutations']) == 1
    assert (output / 'evidence.patch').is_file()


def test_fork_delivery_is_a_patch_applicable_to_the_prepared_head(open_intake):
    state, _, source, *_ = open_intake
    state['pr']['head'].update(repo={'full_name': 'contributor/inference-playbooks'}, ref='main')
    plan, output = prepare(open_intake)
    assert plan['mode'] == 'patch'
    assert workflow.publish(plan, 'maintainer') is None
    assert not state['mutations']
    workflow.git(source, 'apply', '--check', str(output / 'evidence.patch'))
    assert plan['head_sha'] in (output / 'summary.md').read_text()


def test_size_limit_falls_back_to_patch(open_intake, monkeypatch):
    monkeypatch.setattr(workflow, 'MAX_PUBLISH_BYTES', 1)
    plan, _ = prepare(open_intake)
    assert plan['mode'] == 'patch'


def test_repeat_import_after_evidence_commit_is_offline_and_noop(open_intake):
    state, _, _, _, revision, client, *_ = open_intake
    plan, output = prepare(open_intake)
    repo = output / 'checkout'
    commit_evidence(repo)
    state['source'] = repo
    state['pr']['head']['sha'] = workflow.git(repo, 'rev-parse', 'HEAD').stdout.decode().strip()
    client.request = client.run = client.kpis = lambda *a, **kw: pytest.fail('repeat admission contacted MLflow')
    repeated, repeated_output = prepare(open_intake, 'repeated')
    assert repeated['mode'] == 'unchanged'
    assert repeated['tested_revision'] == revision
    assert repeated['files'] == []
    assert repeated_output.joinpath('evidence.patch').read_bytes() == b''
    assert workflow.publish(repeated, 'maintainer') is None
    assert not state['mutations']


def test_fix_rerun_import_preserves_history_and_rejects_old_run(open_intake):
    state, repository, _, path, revision, client, parent, artifacts = open_intake
    _, output = prepare(open_intake)
    repo = output / 'checkout'
    commit_evidence(repo)
    recipe_path = repo / path.relative_to(open_intake[2])
    historical = {p.relative_to(repo): p.read_bytes() for p in recipe_path.parent.glob('results/**/*') if p.is_file()}
    recipe = yaml.safe_load(recipe_path.read_text())
    recipe['serving']['args'] = [{'flag': '--max-num-seqs', 'value': '8'}]
    recipe_path.write_text(yaml.safe_dump(recipe))
    source = next(key for key in artifacts if key.endswith('/server-deployment.yaml'))
    launch = yaml.safe_load(artifacts[source])
    launch['spec']['template']['spec']['containers'][0]['args'] += ['--max-num-seqs', '8']
    artifacts[source] = yaml.safe_dump(launch)
    new_revision = record_launch(repo, artifacts, source)
    state['source'] = repo
    state['pr']['head']['sha'] = new_revision
    with pytest.raises(ValueError, match='differs from tested revision'):
        prepare(open_intake, 'stale')
    assert not (open_intake[2] / 'stale/plan.json').exists()

    parent['info']['run_id'] = 'e' * 32
    original = client.request
    client.request = lambda endpoint, *a, **kw: original(endpoint.replace('e' * 32, 'd' * 32), *a, **kw)
    state['comments'][124] = {**forge_report('e' * 32, comment_id=124),
                              'issue_url': f'https://api.github.com/repos/{repository}/issues/34'}
    new_plan, new_output = prepare(open_intake, 'rerun', 124)
    assert new_plan['tested_revision'] == new_revision != revision
    new_repo = new_output / 'checkout'
    assert all((new_repo / p).read_bytes() == raw for p, raw in historical.items())
    updated = yaml.safe_load((new_repo / recipe_path.relative_to(repo)).read_text())
    assert len(updated['benchmark_runs']) == 4


@pytest.mark.parametrize('path', ['.github/workflows/evil.yml', 'tools/evil.py',
                                 'models/test/recipes/recipe/../../outside',
                                 'models/test/recipes/recipe/results/forge-' + 'a' * 32 + '-aiperf/evil.py'])
def test_publication_path_allowlist(path):
    with pytest.raises(ValueError, match='allowlist'):
        workflow.evidence_path(path)


@pytest.mark.parametrize('directory', ['templates', 'schema'])
def test_unreviewed_executable_or_validation_inputs_rejected(tmp_path, directory):
    trusted, candidate = tmp_path / 'trusted', tmp_path / 'candidate'
    trusted.mkdir()
    (candidate / directory).mkdir(parents=True)
    (candidate / directory / 'injected').write_text('unreviewed')
    with pytest.raises(ValueError, match='differs from trusted main'):
        workflow.trusted_inputs(candidate, trusted)


@pytest.mark.parametrize('mode', ['120000', '160000'])
def test_nonregular_git_inputs_rejected_before_checkout(tmp_path, monkeypatch, mode):
    calls = []
    head = 'a' * 40
    def git(repo, *args, **kwargs):
        calls.append(args)
        output = (head + '\n').encode() if args[0] == 'rev-parse' else b''
        if args[0] == 'ls-tree':
            output = f'{mode} blob {head}\tlink\0'.encode()
        return SimpleNamespace(stdout=output, returncode=0)
    monkeypatch.setattr(workflow, 'git', git)
    with pytest.raises(ValueError, match='regular files'):
        workflow.checkout_candidate('owner/repository', 1, head, tmp_path / 'candidate')
    assert not any(args[0] == 'checkout' for args in calls)


def test_validation_uses_trusted_scripts_without_mlflow_credentials(tmp_path, monkeypatch):
    calls = []
    trusted, candidate = tmp_path / 'trusted', tmp_path / 'candidate'
    monkeypatch.setenv('MLFLOW_PASSWORD', 'must-not-reach-validator')
    monkeypatch.setenv('GH_TOKEN', 'must-not-reach-validator')
    monkeypatch.setattr(workflow.subprocess, 'run', lambda args, **kw: calls.append((args, kw)))
    monkeypatch.setattr(workflow, 'git', lambda *a, **kw: SimpleNamespace(stdout=b'a' * 40))
    monkeypatch.setattr('catalog.build_catalog', lambda *a: None)
    workflow.validate_candidate(candidate, trusted)
    assert len(calls) == 2
    for args, kw in calls:
        assert Path(args[1]).parent == trusted / 'tools'
        assert kw['cwd'] == trusted
        assert 'MLFLOW_PASSWORD' not in kw['env'] and 'GH_TOKEN' not in kw['env']


def test_open_pr_workflow_trust_and_publication_boundaries():
    workflow_yaml = yaml.safe_load((REPO / '.github/workflows/benchmark-import-pr.yml').read_text())
    events = workflow_yaml.get('on', workflow_yaml.get(True))
    assert set(events) == {'workflow_dispatch'}
    assert set(events['workflow_dispatch']['inputs']) == {'pr_number', 'report_comment_id'}
    job = workflow_yaml['jobs']['import']
    assert job['if'] == "${{ github.ref == 'refs/heads/main' }}"
    assert job['environment'] == 'forge-evidence'
    steps = job['steps']
    assert steps[0]['with'] == {'ref': '${{ github.sha }}', 'path': 'trusted', 'persist-credentials': False}
    prepare_step = next(step for step in steps if step.get('id') == 'prepare')
    assert prepare_step['env']['ACTOR'] == '${{ github.triggering_actor }}'
    app = next(step for step in steps if step.get('id') == 'app-token')
    assert app['if'] == "${{ steps.prepare.outputs.publishable == 'true' }}"
    assert steps.index(app) > steps.index(prepare_step)
    publisher = next(step for step in steps if step.get('name', '').startswith('Publish'))
    assert 'MLFLOW_PASSWORD' not in publisher['env']
    assert 'trusted/tools/open_pr_evidence.py publish' in publisher['run']
    upload = next(step for step in steps if 'upload-artifact@' in step.get('uses', ''))
    assert steps.index(upload) < steps.index(publisher)
    assert 'checkout' not in upload['with']['path']
    assert workflow_yaml['concurrency']['cancel-in-progress'] is False
