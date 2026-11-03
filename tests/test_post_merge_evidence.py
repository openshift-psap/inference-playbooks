"""Append-only post-merge evidence preparation and publication."""
import base64
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tools'))
import post_merge_evidence as post

REPOSITORY = 'openshift-psap/inference-playbooks'
MAIN = 'a' * 40
MAINTAINER = 'b' * 40
BRANCH = f'automation/forge-evidence-{MAIN}-92'
RECIPE = 'models/demo/recipes/demo/recipe.yaml'
RESULTS = 'models/demo/recipes/demo/results/forge-' + 'c' * 32 + '-guidellm'


def git(repo, *args):
    return subprocess.run(
        ['git', '-c', 'user.name=test', '-c', 'user.email=test@example.com',
         '-C', str(repo), *args], check=True, capture_output=True, text=True)


def head(repo):
    return git(repo, 'rev-parse', 'HEAD').stdout.strip()


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(post, 'has_closed_pr', lambda *_: False)
    repo = tmp_path / 'checkout'
    repo.mkdir()
    subprocess.run(['git', '-C', str(repo), 'init', '-q'], check=True)
    (repo / 'models/demo/recipes/demo').mkdir(parents=True)
    (repo / RECIPE).write_text('recipe_id: demo\nbenchmark_runs: []\n')
    (repo / '.gitignore').write_text('.build/\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'base')
    return repo


def write_evidence(repo, name='run.yaml', text='evidence\n'):
    directory = repo / RESULTS
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text)
    return f'{RESULTS}/{name}'


def make_plan(repo, existing=True, base=MAIN, revision=None):
    return {'version': 1, 'repository': REPOSITORY, 'branch': BRANCH, 'existing': existing,
            'base': base, 'head': revision or head(repo), 'candidate': str(repo)}


def test_prepare_bases_candidate_on_existing_branch(monkeypatch, tmp_path):
    monkeypatch.delenv('GITHUB_OUTPUT', raising=False)
    seen = {}
    monkeypatch.setattr(post, 'branch_head', lambda repository, branch: MAINTAINER)
    monkeypatch.setattr(post, 'checkout_candidate',
                        lambda repository, branch, main_sha, target, repo: seen.update(
                            target=target, main=main_sha, branch=branch))
    plan = post.prepare(REPOSITORY, BRANCH, MAIN, tmp_path / 'out')
    assert set(plan) == post.PLAN_KEYS
    assert plan['existing'] is True
    assert plan['base'] == MAIN
    assert plan['head'] == MAINTAINER
    assert seen == {'target': MAINTAINER, 'main': MAIN, 'branch': BRANCH}


def test_prepare_new_branch_uses_trusted_main(monkeypatch, tmp_path):
    monkeypatch.delenv('GITHUB_OUTPUT', raising=False)
    seen = {}
    monkeypatch.setattr(post, 'branch_head', lambda repository, branch: None)
    monkeypatch.setattr(post, 'checkout_candidate',
                        lambda repository, branch, main_sha, target, repo: seen.update(target=target))
    plan = post.prepare(REPOSITORY, BRANCH, MAIN, tmp_path / 'out')
    assert plan['existing'] is False
    assert plan['head'] == MAIN
    assert seen == {'target': MAIN}


def test_prepare_rejects_nondeterministic_branch(monkeypatch, tmp_path):
    monkeypatch.setattr(post, 'branch_head', lambda repository, branch: None)
    with pytest.raises(ValueError, match='deterministic evidence branch'):
        post.prepare(REPOSITORY, 'main', MAIN, tmp_path / 'out')


def test_noop_reuses_existing_pr_without_a_commit(monkeypatch, candidate):
    plan = make_plan(candidate)
    monkeypatch.setattr(post, 'branch_head', lambda repository, branch: plan['head'])
    monkeypatch.setattr(post, 'find_pr', lambda repository, branch: 99)
    forbidden = []
    monkeypatch.setattr(post, 'graphql', lambda payload: forbidden.append(payload))
    monkeypatch.setattr(post, 'create_ref', lambda *args: forbidden.append(args))
    assert post.publish(plan, candidate) == (99, None)
    assert forbidden == []


def test_existing_branch_publication_appends_with_cas(monkeypatch, candidate):
    path = write_evidence(candidate)
    plan = make_plan(candidate)
    monkeypatch.setattr(post, 'branch_head', lambda repository, branch: plan['head'])
    monkeypatch.setattr(post, 'find_pr', lambda repository, branch: 7)
    monkeypatch.setattr(post, 'create_ref',
                        lambda *args: pytest.fail('existing branch must not be created'))
    monkeypatch.setattr(post, 'create_pr',
                        lambda *args: pytest.fail('existing PR must be reused'))
    captured = {}

    def graphql(payload):
        captured['input'] = payload['variables']['input']
        return {'createCommitOnBranch': {'commit': {'oid': 'd' * 40, 'url': 'https://example.test'}}}

    monkeypatch.setattr(post, 'graphql', graphql)
    assert post.publish(plan, candidate) == (7, 'd' * 40)
    publication = captured['input']
    assert publication['expectedHeadOid'] == plan['head']
    assert publication['branch'] == {'repositoryNameWithOwner': REPOSITORY, 'branchName': BRANCH}
    assert [item['path'] for item in publication['fileChanges']['additions']] == [path]


def test_import_after_maintainer_fix_preserves_history_and_repeat_is_noop(monkeypatch, candidate):
    base = head(candidate)
    mutations = []
    monkeypatch.setattr(post, 'branch_head', lambda *_: head(candidate))
    monkeypatch.setattr(post, 'find_pr', lambda *_: 99)
    monkeypatch.setattr(post, 'create_ref', lambda *_: pytest.fail('must append to existing branch'))

    def append(payload):
        mutation = payload['variables']['input']
        assert mutation['expectedHeadOid'] == head(candidate)
        mutations.append(mutation)
        for item in mutation['fileChanges']['additions']:
            destination = candidate / item['path']
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(base64.b64decode(item['contents']))
        git(candidate, 'add', '.')
        git(candidate, 'commit', '-qm', 'evidence update')
        return {'createCommitOnBranch': {'commit': {'oid': head(candidate), 'url': 'https://example.test'}}}

    monkeypatch.setattr(post, 'graphql', append)
    first_path = write_evidence(candidate)
    _, first = post.publish(make_plan(candidate, base=base), candidate)

    # A maintainer change outside evidence, plus an unrelated recipe edit.
    (candidate / 'maintainer-fix.txt').write_text('keep this fix\n')
    recipe = candidate / RECIPE
    recipe.write_text(recipe.read_text() + 'notes: maintainer-note.yaml\n')
    git(candidate, 'add', '.')
    git(candidate, 'commit', '-qm', 'maintainer fix')
    maintainer = head(candidate)
    second_path = first_path.replace('c' * 32, 'd' * 32)
    (candidate / second_path).parent.mkdir(parents=True)
    (candidate / second_path).write_text('new execution\n')
    _, second = post.publish(make_plan(candidate, base=base), candidate)

    assert git(candidate, 'rev-parse', f'{second}^').stdout.strip() == maintainer
    assert git(candidate, 'rev-parse', f'{maintainer}^').stdout.strip() == first
    assert (candidate / 'maintainer-fix.txt').read_text() == 'keep this fix\n'
    assert 'notes: maintainer-note.yaml' in recipe.read_text()
    assert (candidate / first_path).read_text() == 'evidence\n'
    assert post.publish(make_plan(candidate, base=base), candidate) == (99, None)
    assert len(mutations) == 2
    assert head(candidate) == second


def test_advanced_branch_fails_without_publication(monkeypatch, candidate):
    write_evidence(candidate)
    plan = make_plan(candidate)
    monkeypatch.setattr(post, 'branch_head', lambda repository, branch: 'e' * 40)
    monkeypatch.setattr(post, 'graphql', lambda payload: pytest.fail('must not publish on a moved branch'))
    monkeypatch.setattr(post, 'create_ref', lambda *args: pytest.fail('must not create a ref'))
    with pytest.raises(ValueError, match='advanced after preparation'):
        post.publish(plan, candidate)


def test_new_branch_creates_ref_only_when_there_are_changes(monkeypatch, candidate):
    write_evidence(candidate)
    plan = make_plan(candidate, existing=False, base=head(candidate))
    refs = []
    monkeypatch.setattr(post, 'branch_head', lambda repository, branch: None)
    monkeypatch.setattr(post, 'create_ref', lambda repository, branch, sha: refs.append((repository, branch, sha)))
    monkeypatch.setattr(post, 'find_pr', lambda repository, branch: None)
    monkeypatch.setattr(post, 'create_pr', lambda repository, branch: 12)
    captured = {}

    def graphql(payload):
        captured['expected'] = payload['variables']['input']['expectedHeadOid']
        return {'createCommitOnBranch': {'commit': {'oid': 'f' * 40, 'url': 'https://example.test'}}}

    monkeypatch.setattr(post, 'graphql', graphql)
    assert post.publish(plan, candidate) == (12, 'f' * 40)
    assert refs == [(REPOSITORY, BRANCH, plan['base'])]
    assert captured['expected'] == plan['base']


def test_new_branch_without_changes_creates_nothing(monkeypatch, candidate):
    plan = make_plan(candidate, existing=False, base=head(candidate))
    monkeypatch.setattr(post, 'branch_head', lambda repository, branch: None)
    monkeypatch.setattr(post, 'find_pr', lambda repository, branch: None)
    monkeypatch.setattr(post, 'create_ref', lambda *args: pytest.fail('no-op must not create a ref'))
    monkeypatch.setattr(post, 'graphql', lambda payload: pytest.fail('no-op must not commit'))
    monkeypatch.setattr(post, 'create_pr', lambda *args: pytest.fail('no-op must not create a PR'))
    assert post.publish(plan, candidate) == (None, None)


def test_pr_is_created_only_when_missing(monkeypatch, candidate):
    write_evidence(candidate)
    plan = make_plan(candidate)
    monkeypatch.setattr(post, 'branch_head', lambda repository, branch: plan['head'])
    monkeypatch.setattr(post, 'find_pr', lambda repository, branch: None)
    monkeypatch.setattr(post, 'create_pr', lambda repository, branch: 42)
    monkeypatch.setattr(post, 'graphql', lambda payload: {
        'createCommitOnBranch': {'commit': {'oid': '1' * 40, 'url': 'https://example.test'}}})
    assert post.publish(plan, candidate) == (42, '1' * 40)


def test_paths_outside_the_evidence_allowlist_are_rejected(candidate):
    (candidate / 'docs').mkdir()
    (candidate / 'docs/notes.md').write_text('not evidence\n')
    with pytest.raises(ValueError, match='evidence allowlist'):
        post.collect_changes(candidate)


def test_symlinks_are_rejected(candidate):
    target = candidate / 'outside.txt'
    target.write_text('outside\n')
    directory = candidate / RESULTS
    directory.mkdir(parents=True)
    (directory / 'run.yaml').symlink_to(target)
    with pytest.raises(ValueError, match='canonical regular file'):
        post.collect_changes(candidate)


def test_additive_post_merge_intake_cannot_delete_evidence(candidate):
    path = write_evidence(candidate)
    git(candidate, 'add', '.')
    git(candidate, 'commit', '-qm', 'evidence')
    (candidate / path).unlink()
    with pytest.raises(ValueError, match='unexpected deletion'):
        post.collect_changes(candidate)


def test_existing_result_files_are_immutable(candidate):
    path = write_evidence(candidate)
    git(candidate, 'add', '.')
    git(candidate, 'commit', '-qm', 'evidence')
    (candidate / path).write_text('changed\n')
    with pytest.raises(ValueError, match='immutable'):
        post.collect_changes(candidate)


def test_recipe_edits_may_only_change_evidence_references(candidate):
    (candidate / RECIPE).write_text('recipe_id: demo\nbenchmark_runs: []\nimage: unexpected\n')
    with pytest.raises(ValueError, match='changed recipe inputs'):
        post.collect_changes(candidate)


def test_recipe_cannot_reference_unpublished_evidence(candidate):
    path = f'{RESULTS}/run.yaml'
    (candidate / RECIPE).write_text(
        f'recipe_id: demo\nbenchmark_runs: ["results/{Path(path).parent.name}/run.yaml"]\n')
    with pytest.raises(ValueError, match='not collectable'):
        post.collect_changes(candidate)


def test_import_cannot_create_a_recipe_even_with_evidence(candidate):
    result = 'models/new/recipes/new/results/forge-' + 'c' * 32 + '-guidellm/run.yaml'
    (candidate / result).parent.mkdir(parents=True)
    (candidate / result).write_text('evidence\n')
    new_recipe = 'models/new/recipes/new/recipe.yaml'
    (candidate / new_recipe).write_text(
        'recipe_id: new\nbenchmark_runs: ["results/forge-' + 'c' * 32 + '-guidellm/run.yaml"]\n')
    with pytest.raises(ValueError, match='cannot create a recipe'):
        post.collect_changes(candidate)


def test_new_recipe_without_references_is_rejected(candidate):
    new_recipe = 'models/new/recipes/new/recipe.yaml'
    (candidate / new_recipe).parent.mkdir(parents=True)
    (candidate / new_recipe).write_text('recipe_id: new\nbenchmark_runs: []\n')
    with pytest.raises(ValueError, match='cannot create a recipe'):
        post.collect_changes(candidate)


def test_stage_replaces_runtime_inputs_with_trusted(tmp_path):
    candidate = tmp_path / 'candidate'
    trusted = tmp_path / 'trusted'
    for root in (candidate, trusted):
        for name in post.RUNTIME_INPUTS:
            (root / name).mkdir(parents=True)
        (root / 'models').mkdir()
    (candidate / 'tools/validate.py').write_text('candidate code\n')
    (candidate / 'tools/candidate_only.py').write_text('candidate only\n')
    (trusted / 'tools/validate.py').write_text('trusted code\n')
    (candidate / 'schema/x.schema.json').write_text('candidate schema\n')
    (trusted / 'schema/x.schema.json').write_text('trusted schema\n')
    (candidate / 'templates/x.j2').write_text('candidate template\n')
    (trusted / 'templates/x.j2').write_text('trusted template\n')
    (candidate / 'models/demo.yaml').write_text('candidate data\n')
    snapshot = post.stage(candidate, trusted, tmp_path / 'snapshot')
    assert (snapshot / 'tools/validate.py').read_text() == 'trusted code\n'
    assert not (snapshot / 'tools/candidate_only.py').exists()
    assert (snapshot / 'schema/x.schema.json').read_text() == 'trusted schema\n'
    assert (snapshot / 'templates/x.j2').read_text() == 'trusted template\n'
    assert (snapshot / 'models/demo.yaml').read_text() == 'candidate data\n'


def test_stage_requires_empty_output(tmp_path):
    candidate = tmp_path / 'candidate'
    trusted = tmp_path / 'trusted'
    for root in (candidate, trusted):
        for name in post.RUNTIME_INPUTS:
            (root / name).mkdir(parents=True)
    snapshot = tmp_path / 'snapshot'
    snapshot.mkdir()
    (snapshot / 'stale').write_text('stale\n')
    with pytest.raises(ValueError, match='must be empty'):
        post.stage(candidate, trusted, snapshot)


def test_noop_committed_branch_opens_missing_pr(monkeypatch, candidate):
    base = head(candidate)
    write_evidence(candidate)
    git(candidate, 'add', '.')
    git(candidate, 'commit', '-qm', 'committed evidence')
    plan = make_plan(candidate, existing=True, base=base)
    monkeypatch.setattr(post, 'branch_head', lambda repository, branch: plan['head'])
    monkeypatch.setattr(post, 'find_pr', lambda repository, branch: None)
    monkeypatch.setattr(post, 'create_pr', lambda repository, branch: 77)
    assert post.publish(plan, candidate) == (77, None)


def test_noop_empty_branch_does_not_open_pr(monkeypatch, candidate):
    plan = make_plan(candidate, existing=True, base=head(candidate))
    monkeypatch.setattr(post, 'branch_head', lambda repository, branch: plan['head'])
    monkeypatch.setattr(post, 'find_pr', lambda repository, branch: None)
    monkeypatch.setattr(post, 'create_pr', lambda *args: pytest.fail('empty branch must not open a PR'))
    assert post.publish(plan, candidate) == (None, None)


def test_noop_after_squash_merge_does_not_resurrect_pr(monkeypatch, candidate):
    base = head(candidate)
    write_evidence(candidate)
    git(candidate, 'add', '.')
    git(candidate, 'commit', '-qm', 'already squash-merged evidence')
    plan = make_plan(candidate, base=base)
    monkeypatch.setattr(post, 'branch_head', lambda *_: plan['head'])
    monkeypatch.setattr(post, 'find_pr', lambda *_: None)
    monkeypatch.setattr(post, 'has_closed_pr', lambda *_: True)
    monkeypatch.setattr(post, 'create_pr', lambda *_: pytest.fail('must not resurrect closed PR'))
    monkeypatch.setattr(post, 'graphql', lambda *_: pytest.fail('must not publish'))
    assert post.publish(plan, candidate) == (None, None)


def test_new_evidence_on_closed_pr_requires_new_dispatch(monkeypatch, candidate):
    write_evidence(candidate)
    plan = make_plan(candidate)
    monkeypatch.setattr(post, 'branch_head', lambda *_: plan['head'])
    monkeypatch.setattr(post, 'find_pr', lambda *_: None)
    monkeypatch.setattr(post, 'has_closed_pr', lambda *_: True)
    monkeypatch.setattr(post, 'graphql', lambda *_: pytest.fail('must not publish'))
    with pytest.raises(ValueError, match='PR is closed'):
        post.publish(plan, candidate)


def test_retry_after_pr_create_failure_reuses_existing_pr(monkeypatch, candidate):
    write_evidence(candidate)
    plan = make_plan(candidate)
    monkeypatch.setattr(post, 'branch_head', lambda repository, branch: plan['head'])
    lookups = iter([None, None, 88])

    def find_pr(repository, branch):
        return next(lookups, 88)

    monkeypatch.setattr(post, 'find_pr', find_pr)

    def fail_create(repository, branch):
        raise ValueError('temporary failure')

    monkeypatch.setattr(post, 'create_pr', fail_create)
    monkeypatch.setattr(post, 'graphql', lambda payload: {
        'createCommitOnBranch': {'commit': {'oid': '3' * 40, 'url': 'https://example.test'}}})
    assert post.publish(plan, candidate) == (88, '3' * 40)


def test_publication_never_forces_or_resets_git(candidate, monkeypatch):
    write_evidence(candidate)
    plan = make_plan(candidate)
    calls = []
    real_git = post.git

    def recording(repo, *args, check=True):
        calls.append(args)
        return real_git(repo, *args, check=check)

    monkeypatch.setattr(post, 'git', recording)
    monkeypatch.setattr(post, 'branch_head', lambda repository, branch: plan['head'])
    monkeypatch.setattr(post, 'find_pr', lambda repository, branch: 3)
    monkeypatch.setattr(post, 'graphql', lambda payload: {
        'createCommitOnBranch': {'commit': {'oid': '2' * 40, 'url': 'https://example.test'}}})
    post.publish(plan, candidate)
    forbidden = {'push', 'reset', 'rebase', 'update-ref'}
    assert not [args for args in calls if forbidden & {str(item) for item in args}]


def test_publication_payload_is_bounded(candidate, monkeypatch):
    write_evidence(candidate, 'run.yaml')
    write_evidence(candidate, 'result.json')
    monkeypatch.setattr(post, 'MAX_PUBLISH_FILES', 1)
    with pytest.raises(ValueError, match='file limits'):
        post.collect_changes(candidate)
