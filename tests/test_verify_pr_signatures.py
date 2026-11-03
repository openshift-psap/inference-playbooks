"""Verified-signature checks over paginated PR commit histories."""
import copy
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tools'))
import verify_pr_signatures as signatures

REPOSITORY = 'openshift-psap/inference-playbooks'


def test_recurring_audit_never_executes_pr_or_target_branch_code():
    workflow = yaml.safe_load((REPO / '.github/workflows/commit-signatures.yml').read_text())
    assert workflow['permissions'] == {'contents': 'read', 'pull-requests': 'read'}
    steps = workflow['jobs']['verify']['steps']
    checkout = next(step for step in steps if step.get('uses', '').startswith('actions/checkout@'))
    assert checkout['with']['ref'] == '${{ github.event.repository.default_branch }}'
    assert checkout['with']['persist-credentials'] is False
    audit = next(step for step in steps if 'verify_pr_signatures.py' in step.get('run', ''))
    assert audit['env']['EXPECTED_HEAD'] == '${{ github.event.pull_request.head.sha }}'
    assert '--expected-head "$EXPECTED_HEAD"' in audit['run']


def commit(sha, verified=True, reason='valid', author='psap-playbooks-evidence[bot]'):
    return {'sha': sha, 'commit': {'verification': {'verified': verified, 'reason': reason}},
            'author': {'login': author}}


def pull(head, count):
    return {'head': {'sha': head}, 'commits': count}


class FakeAPI:
    def __init__(self, snapshots, pages):
        self.snapshots = snapshots
        self.pages = pages
        self.calls = []
        self.snapshot_index = 0

    def __call__(self, endpoint):
        self.calls.append(endpoint)
        if '/commits?' in endpoint:
            page = int(endpoint.rsplit('page=', 1)[1])
            return copy.deepcopy(self.pages.get(page, []))
        value = self.snapshots[min(self.snapshot_index, len(self.snapshots) - 1)]
        self.snapshot_index += 1
        return copy.deepcopy(value)


def run(monkeypatch, api, number=7):
    monkeypatch.setattr(signatures, 'gh_json', api)
    return signatures.main(['--repository', REPOSITORY, '--pr-number', str(number)])


def test_all_verified_across_three_pages(monkeypatch, capsys):
    shas = [f'{index:040x}' for index in range(230)]
    api = FakeAPI([pull(shas[-1], len(shas))],
                  {1: [commit(sha) for sha in shas[:100]],
                   2: [commit(sha) for sha in shas[100:200]],
                   3: [commit(sha) for sha in shas[200:]]})
    assert run(monkeypatch, api) == 0
    requested = [call.rsplit('page=', 1)[1] for call in api.calls if '/commits?' in call]
    assert requested == ['1', '2', '3']
    lines = [line for line in capsys.readouterr().out.splitlines() if ': verified reason=valid' in line]
    assert len(lines) == 230
    assert lines[-1].startswith(shas[-1])


def test_mixed_signed_and_unsigned_history_fails_closed(monkeypatch, capsys):
    good, bad = '1' * 40, '2' * 40
    api = FakeAPI([pull(bad, 2)], {1: [commit(good), commit(bad, False, 'unsigned')]})
    assert run(monkeypatch, api) == 1
    captured = capsys.readouterr()
    assert f'{good}: verified reason=valid author=psap-playbooks-evidence[bot]' in captured.out
    assert f'{bad}: unverified reason=unsigned author=psap-playbooks-evidence[bot]' in captured.out
    assert 'unverified PR commits' in captured.err
    assert bad in captured.err


def test_malformed_verification_metadata_fails_closed(monkeypatch, capsys):
    sha = '3' * 40
    api = FakeAPI([pull(sha, 1)], {1: [{'sha': sha, 'commit': {}}]})
    assert run(monkeypatch, api) == 1
    assert 'verification metadata is missing' in capsys.readouterr().err


def test_empty_commit_list_fails_closed(monkeypatch, capsys):
    api = FakeAPI([pull('4' * 40, 1)], {})
    assert run(monkeypatch, api) == 1
    assert 'refusing to verify a truncated commit list' in capsys.readouterr().err


def test_truncated_commit_list_fails_closed(monkeypatch, capsys):
    first, head = '5' * 40, '6' * 40
    api = FakeAPI([pull(head, 2)], {1: [commit(first)]})
    assert run(monkeypatch, api) == 1
    assert 'refusing to verify a truncated commit list' in capsys.readouterr().err


def test_commit_list_must_end_at_the_pr_head(monkeypatch, capsys):
    first, head = 'a' * 40, 'b' * 40
    api = FakeAPI([pull(head, 1)], {1: [commit(first)]})
    assert run(monkeypatch, api) == 1
    assert 'not the PR head' in capsys.readouterr().err


def test_more_than_endpoint_limit_is_rejected_not_truncated(monkeypatch, capsys):
    api = FakeAPI([pull('7' * 40, signatures.MAX_PR_COMMITS + 1)], {})
    assert run(monkeypatch, api) == 1
    assert not [call for call in api.calls if '/commits?' in call]
    assert 'refusing to verify a truncated commit list' in capsys.readouterr().err


def test_head_change_during_verification_fails_closed(monkeypatch, capsys):
    old, new = '8' * 40, '9' * 40
    api = FakeAPI([pull(old, 1), pull(new, 1)], {1: [commit(old)]})
    assert run(monkeypatch, api) == 1
    assert 'PR head changed during verification' in capsys.readouterr().err


def test_outdated_event_cannot_verify_newer_history():
    api = FakeAPI([pull('a' * 40, 1)], {})
    with pytest.raises(signatures.VerificationError, match='triggering event'):
        signatures.verify(REPOSITORY, 7, api=api, expected_head='b' * 40)
    assert len(api.calls) == 1


def test_duplicate_commits_do_not_satisfy_reported_count(monkeypatch, capsys):
    sha = 'a' * 40
    api = FakeAPI([pull(sha, 2)], {1: [commit(sha), commit(sha)]})
    assert run(monkeypatch, api) == 1
    assert 'duplicate commit' in capsys.readouterr().err


def test_unknown_verification_reason_fails_closed(monkeypatch, capsys):
    sha = 'a' * 40
    api = FakeAPI([pull(sha, 1)], {1: [commit(sha, reason='unknown')]})
    assert run(monkeypatch, api) == 1
    assert 'unknown' in capsys.readouterr().err


def test_retargeted_pr_fails_closed(monkeypatch, capsys):
    sha = 'a' * 40
    old = {**pull(sha, 1), 'base': {'sha': 'b' * 40}}
    new = {**pull(sha, 1), 'base': {'sha': 'c' * 40}}
    api = FakeAPI([old, new], {1: [commit(sha)]})
    assert run(monkeypatch, api) == 1
    assert 'PR base or commit count changed' in capsys.readouterr().err


def test_gh_api_failure_and_malformed_json_fail_closed(monkeypatch):
    failure = subprocess.CompletedProcess(['gh'], 1, '', 'HTTP 403: rate limited')
    monkeypatch.setattr(signatures.subprocess, 'run', lambda *args, **kwargs: failure)
    with pytest.raises(signatures.APIError, match='rate limited'):
        signatures.gh_json('repos/x/y/pulls/1')

    malformed = subprocess.CompletedProcess(['gh'], 0, 'not json', '')
    monkeypatch.setattr(signatures.subprocess, 'run', lambda *args, **kwargs: malformed)
    with pytest.raises(signatures.APIError, match='malformed JSON'):
        signatures.gh_json('repos/x/y/pulls/1')
