"""Maintainer workflow proof: real Git/worktrees, synthetic evidence, mocked publication."""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_contributor import commit, evidence, source  # noqa: F401 -- shared synthetic fixtures

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import import_contributor as importer
import maintain_contributor as flow
from contributor_bundle import json_bytes, json_load, write_archive


@pytest.fixture
def review(evidence, tmp_path, monkeypatch):
    repo, _, request, _, bundle, digest = evidence
    for key, value in [('user.name', 'Test'), ('user.email', 'test@example.org'), ('commit.gpgsign', 'false')]:
        flow.git(repo, 'config', key, value)
    flow.git(repo, 'remote', 'add', 'origin', 'git@github.com:example/playbooks.git')
    state = SimpleNamespace(events=[], commands=[], fail_push=False, fail_pr=False,
                            validation_fail=False, extra_commit_file=False, existing_pr=None, base_ahead=0)
    original = flow.command

    def command(argv, *, cwd=None):
        argv = [str(arg) for arg in argv]
        state.commands.append(argv)
        if argv[0] == sys.executable:
            state.events.append('validate')
            assert Path(argv[1]).parent == flow.TOOLS
            assert '--repo' in argv
            if state.validation_fail:
                raise ValueError('synthetic validation failed')
            return b''
        if argv[0] == 'git' and argv[3] == 'push':
            state.events.append('push')
            assert '--force' not in argv and ':refs/heads/evidence/' in argv[-1]
            if state.fail_push:
                raise ValueError('synthetic push failed')
            return b''
        if argv[0] == 'git' and argv[3] == 'commit':
            state.events.append('commit')
            if state.extra_commit_file:
                worktree = Path(argv[2])
                (worktree / 'unexpected.txt').write_text('synthetic hook change')
                flow.git(worktree, 'add', 'unexpected.txt')
        if argv[:3] == ['gh', 'pr', 'list']:
            state.events.append('list')
            return json_bytes([state.existing_pr] if state.existing_pr else [])
        if argv[:2] == ['gh', 'api']:
            state.events.append('base')
            assert '/compare/' in argv[2] and argv[-1] == '.ahead_by'
            return str(state.base_ahead).encode()
        if argv[:3] == ['gh', 'pr', 'create']:
            state.events.append('pr')
            state.existing_pr = {'url': 'https://github.com/example/playbooks/pull/1',
                                 'headRefOid': flow.revision(cwd)}
            if state.fail_pr:
                raise ValueError('synthetic lost PR response')
            return state.existing_pr['url'].encode()
        return original(argv, cwd=cwd)

    monkeypatch.setattr(flow, 'command', command)
    worktree = tmp_path / 'review'

    def prepare(**kwargs):
        return flow.prepare_review(repo, bundle, digest, worktree, base='HEAD',
                                   github_repo='example/playbooks', **kwargs)

    state.repo, state.worktree, state.bundle, state.digest = repo, worktree, bundle, digest
    state.request, state.prepare = request, prepare
    return state


def test_prepare_creates_private_staged_review_without_publication(review):
    report = review.prepare()
    assert report['commit'] is None and report['pr_url'] is None
    assert not report['maturity_changed']
    assert report['tested_revision'] == review.request['tested_revision']
    assert 'push' not in review.events and 'commit' not in review.events
    assert not any(argv[0] == 'gh' for argv in review.commands)
    assert not flow.git(review.repo, 'status', '--porcelain').strip()
    private = flow.review_directory(review.worktree)
    assert not private.is_relative_to(review.worktree)
    assert (private.stat().st_mode & 0o777) == 0o700
    assert (private / 'evidence.tar.gz').read_bytes() == review.bundle.read_bytes()
    assert (private / 'native/raw/harness-0.json').is_file()
    assert not flow.git(review.worktree, 'ls-files', '*receipt*', '*.tar.gz')
    receipt = json_load((private / 'receipt.json').read_bytes())
    assert len(receipt['expected']) == 7
    assert set(receipt['changed']) == set(receipt['expected'])
    assert len([event for event in review.events if event == 'validate']) == 3


def test_publish_requires_review_before_any_action(review):
    review.prepare()
    review.events.clear()
    with pytest.raises(ValueError, match='--reviewed'):
        flow.publish_review(review.worktree)
    assert review.events == []


def test_publish_and_retry_preserve_exact_bytes_and_reuse_pr(review):
    review.prepare()
    report = flow.publish_review(review.worktree, reviewed=True)
    assert report['pr_url'] == 'https://github.com/example/playbooks/pull/1'
    assert flow.revision(review.worktree) == report['commit']
    assert not flow.git(review.worktree, 'status', '--porcelain')
    assert review.events.index('commit') < review.events.index('push') < review.events.index('pr')
    before = list(review.events)
    assert flow.publish_review(review.worktree, reviewed=True) == report
    assert review.events.count('commit') == before.count('commit') == 1
    assert review.events.count('push') == before.count('push') == 1
    assert review.events.count('pr') == before.count('pr') == 1


@pytest.mark.parametrize('failure', ['push', 'pr'])
def test_publication_retry_does_not_recommit_or_duplicate_pr(review, failure):
    review.prepare()
    setattr(review, 'fail_' + failure, True)
    with pytest.raises(ValueError, match='synthetic'):
        flow.publish_review(review.worktree, reviewed=True)
    committed = flow.revision(review.worktree)
    setattr(review, 'fail_' + failure, False)
    report = flow.publish_review(review.worktree, reviewed=True)
    assert report['commit'] == committed
    assert review.events.count('commit') == 1
    assert review.events.count('pr') == 1


@pytest.mark.parametrize('change', ['unstaged', 'staged', 'mode', 'extra', 'head', 'branch', 'origin', 'archive'])
def test_changed_review_refuses_publication_without_reset(review, change):
    review.prepare()
    receipt = json_load((flow.review_directory(review.worktree) / 'receipt.json').read_bytes())
    path = review.worktree / next(name for name in receipt['expected'] if name.endswith('result.json'))
    if change in {'unstaged', 'staged'}:
        path.write_bytes(path.read_bytes() + b'\n')
        if change == 'staged':
            flow.git(review.worktree, 'add', '--', str(path.relative_to(review.worktree)))
    elif change == 'mode':
        path.chmod(0o755)
        flow.git(review.worktree, 'add', '--', str(path.relative_to(review.worktree)))
    elif change == 'extra':
        (review.worktree / 'extra.txt').write_text('unrelated')
        flow.git(review.worktree, 'add', 'extra.txt')
    elif change == 'head':
        flow.git(review.worktree, 'commit', '-m', 'unrelated operator commit')
    elif change == 'branch':
        flow.git(review.worktree, 'switch', '-c', 'other-review')
    elif change == 'origin':
        flow.git(review.worktree, 'remote', 'set-url', 'origin', 'git@github.com:other/repo.git')
    else:
        (flow.review_directory(review.worktree) / 'evidence.tar.gz').write_bytes(b'changed')
    review.events.clear()
    with pytest.raises(ValueError):
        flow.publish_review(review.worktree, reviewed=True)
    assert 'push' not in review.events and 'pr' not in review.events
    assert 'commit' not in review.events


def test_commit_hook_cannot_add_unreviewed_files_to_publication(review):
    review.prepare()
    review.extra_commit_file = True
    with pytest.raises(ValueError, match='outside the reviewed diff'):
        flow.publish_review(review.worktree, reviewed=True)
    assert 'push' not in review.events
    assert (review.worktree / 'unexpected.txt').exists()


def test_wrong_github_pr_base_cannot_publish_unreviewed_source_commits(review):
    review.prepare()
    review.base_ahead = 1
    with pytest.raises(ValueError, match='PR base does not contain'):
        flow.publish_review(review.worktree, reviewed=True)
    assert 'commit' not in review.events and 'push' not in review.events and 'pr' not in review.events


def test_s3_upload_is_deferred_until_review_and_precedes_commit(review, monkeypatch):
    calls = []

    def upload(path, prefix, digest):
        review.events.append('upload')
        calls.append((path, prefix, digest))
        return importer.archive_destination(prefix, digest)[2]

    monkeypatch.setattr(importer, 'upload_archive', upload)
    report = review.prepare(s3_prefix='s3://accepted/contributor')
    assert report['s3_upload_pending'] and not calls
    assert not list(review.worktree.glob('models/*/recipes/*/results/*/raw-harness*'))
    report = flow.publish_review(review.worktree, reviewed=True)
    assert not report['s3_upload_pending'] and len(calls) == 1
    assert review.events.index('upload') < review.events.index('commit') < review.events.index('push')


def test_upload_failure_preserves_staged_review_without_publication(review, monkeypatch):
    review.prepare(s3_prefix='s3://accepted/contributor')
    head = flow.revision(review.worktree)

    def fail(*args):
        raise ValueError('synthetic upload failed')

    monkeypatch.setattr(importer, 'upload_archive', fail)
    with pytest.raises(ValueError, match='upload failed'):
        flow.publish_review(review.worktree, reviewed=True)
    assert flow.revision(review.worktree) == head
    assert 'push' not in review.events and 'commit' not in review.events
    assert flow.git(review.worktree, 'diff', '--cached', '--name-only')


def test_validation_failure_cannot_publish(review):
    review.prepare()
    review.validation_fail = True
    with pytest.raises(ValueError, match='validation failed'):
        flow.publish_review(review.worktree, reviewed=True)
    assert 'push' not in review.events and 'commit' not in review.events


def test_prepare_validation_failure_keeps_a_resumable_review(review):
    review.validation_fail = True
    with pytest.raises(ValueError, match='validation failed'):
        review.prepare()
    assert (flow.review_directory(review.worktree) / 'receipt.json').is_file()
    assert 'push' not in review.events and 'commit' not in review.events
    review.validation_fail = False
    assert flow.publish_review(review.worktree, reviewed=True)['pr_url']


def test_dirty_source_and_existing_worktree_are_not_overwritten(review):
    extra = review.repo / 'user-work.txt'
    extra.write_text('retain me')
    with pytest.raises(ValueError, match='clean'):
        review.prepare()
    assert not review.worktree.exists() and extra.read_text() == 'retain me'
    # This file was created only by this test; make the fixture clean without a reset.
    extra.unlink()
    review.prepare()
    with pytest.raises(ValueError, match='already exists'):
        review.prepare()
    assert (flow.review_directory(review.worktree) / 'receipt.json').is_file()


def test_failed_bundle_is_rejected_before_creating_worktree(review, evidence):
    _, _, request, files, _, _ = evidence
    review.bundle.unlink()
    review.digest = write_archive(review.bundle, request, 'failed', files)
    with pytest.raises(ValueError, match='successful'):
        flow.prepare_review(review.repo, review.bundle, review.digest, review.worktree,
                            base='HEAD', github_repo='example/playbooks')
    assert not review.worktree.exists()


def test_bad_checksum_is_rejected_before_creating_worktree(review):
    with pytest.raises(ValueError, match='checksum mismatch'):
        flow.prepare_review(review.repo, review.bundle, 'sha256:' + '0' * 64, review.worktree,
                            base='HEAD', github_repo='example/playbooks')
    assert not review.worktree.exists()


def test_mismatched_base_never_materializes_or_publishes_evidence(review):
    model = review.repo / 'models/test/model.yaml'
    model.write_text(model.read_text() + 'name: changed checkpoint metadata\n')
    commit(review.repo)
    with pytest.raises(ValueError):
        review.prepare()
    assert not list(review.worktree.glob('models/*/recipes/*/results/*'))
    assert 'push' not in review.events and 'pr' not in review.events
    assert not (flow.review_directory(review.worktree) / 'receipt.json').exists()


def test_already_imported_bundle_does_not_create_an_empty_pr(review):
    importer.import_bundle(review.repo, review.bundle, review.digest, apply=True, reviewed=True)
    commit(review.repo)
    with pytest.raises(ValueError, match='already imported'):
        review.prepare()
    assert 'push' not in review.events and 'pr' not in review.events


def test_cli_checksum_file_and_explicit_review(review):
    sidecar = review.bundle.with_suffix('.checksum')
    sidecar.write_text(review.digest + '\n')
    assert flow.main(['prepare', str(review.bundle), '--checksum-file', str(sidecar),
                      '--repo', str(review.repo), '--base', 'HEAD', '--worktree', str(review.worktree),
                      '--github-repo', 'example/playbooks']) == 0
    assert flow.main(['publish', str(review.worktree)]) == 1
    assert flow.main(['publish', str(review.worktree), '--reviewed']) == 0


def test_command_failure_never_echoes_private_output(monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(
        returncode=1, stdout=b'private credential', stderr=b'private credential'))
    with pytest.raises(ValueError) as error:
        flow.command(['gh', 'pr', 'list'])
    assert 'private credential' not in str(error.value)
