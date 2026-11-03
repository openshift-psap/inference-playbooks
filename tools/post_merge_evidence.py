#!/usr/bin/env python3
"""Prepare and append Forge evidence on the deterministic post-merge branch.

The candidate checkout is based on the current automation branch when it exists,
so maintainer commits remain in history. Publication only appends one commit
through GitHub's ``createCommitOnBranch`` mutation with an ``expectedHeadOid``
guard; nothing is reset, rebased, or force-pushed.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode

from open_pr_evidence import (MAX_PUBLISH_BYTES, MAX_PUBLISH_FILES,
                              collect_changes as collect_evidence_changes,
                              git, repository_name)


SHA = re.compile(r'[0-9a-f]{40}')
BRANCH = re.compile(r'automation/forge-evidence-[0-9a-f]{40}-(?:push|[1-9][0-9]*)')
PLAN_KEYS = {'version', 'repository', 'branch', 'existing', 'base', 'head', 'candidate'}
TITLE = 'Import attributed Forge benchmark evidence'
BODY = ('Automated Forge KPI intake. Raw artifacts and SHA-256 checksums are included.\n'
        'Attribution was checked against the tested PR head and merged deployment inputs.\n'
        'This import does not promote recipe maturity or platform verification.\n')
COMMIT = 'feat(evidence): import attributed Forge benchmark results'
# Validation must never execute or inherit code from the candidate branch.
RUNTIME_INPUTS = ('tools', 'schema', 'templates')


class NotFound(Exception):
    """A GitHub resource does not exist."""


def api(endpoint, method='GET', fields=None):
    """Call one GitHub REST endpoint through gh; fail closed with its detail."""
    command = ['gh', 'api', endpoint, '--method', method]
    for key, value in (fields or {}).items():
        command.extend(['-f', f'{key}={value}'])
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or 'no error output'
        if 'HTTP 404' in detail or 'Not Found' in detail:
            raise NotFound(detail)
        raise ValueError(f'GitHub API request failed ({endpoint}): {detail}')
    return json.loads(result.stdout) if result.stdout.strip() else None


def graphql(payload):
    result = subprocess.run(['gh', 'api', 'graphql', '--input', '-'],
                            input=json.dumps(payload), capture_output=True, text=True)
    if result.returncode:
        raise ValueError('GitHub rejected the guarded evidence commit; the branch may have advanced. Retry preparation.')
    value = json.loads(result.stdout)
    if value.get('errors'):
        raise ValueError('GitHub rejected the guarded evidence commit; the branch may have advanced. Retry preparation.')
    return value['data']


def revision(repo, ref='HEAD'):
    return git(repo, 'rev-parse', ref).stdout.decode().strip()


def branch_head(repository, branch):
    """Return the current commit SHA of the deterministic branch, or None."""
    try:
        value = api(f'repos/{repository}/git/ref/heads/{branch}')
    except NotFound:
        return None
    target = value.get('object') if isinstance(value, dict) else None
    sha = target.get('sha') if isinstance(target, dict) else None
    if not isinstance(target, dict) or target.get('type') != 'commit' or not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ValueError('branch ref response is malformed')
    return sha


def create_ref(repository, branch, sha):
    api(f'repos/{repository}/git/refs', method='POST',
        fields={'ref': f'refs/heads/{branch}', 'sha': sha})


def find_pr(repository, branch, state='open'):
    """Find a PR for this repository branch; forks are unsupported."""
    owner = repository.split('/', 1)[0]
    query = urlencode({'state': state, 'per_page': 100, 'head': f'{owner}:{branch}'})
    pulls = api(f'repos/{repository}/pulls?{query}')
    if not isinstance(pulls, list):
        raise ValueError('pull request list response is malformed')
    numbers = []
    for pull in pulls:
        if not isinstance(pull, dict):
            raise ValueError('pull request list contains a non-object entry')
        head = pull.get('head') if isinstance(pull.get('head'), dict) else {}
        base = pull.get('base') if isinstance(pull.get('base'), dict) else {}
        if head.get('ref') != branch:
            continue
        if (head.get('repo') or {}).get('full_name') != repository or base.get('ref') != 'main':
            raise ValueError('evidence branch PR belongs to another repository; forks are unsupported')
        number = pull.get('number')
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError('pull request number is malformed')
        numbers.append(number)
    if len(numbers) > 1:
        raise ValueError('multiple open pull requests use the evidence branch')
    return numbers[0] if numbers else None


def has_closed_pr(repository, branch):
    return find_pr(repository, branch, state='closed') is not None


def create_pr(repository, branch):
    pull = api(f'repos/{repository}/pulls', method='POST',
               fields={'title': TITLE, 'head': branch, 'base': 'main', 'body': BODY})
    number = pull.get('number') if isinstance(pull, dict) else None
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError('created pull request response is malformed')
    return number


def ensure_pr(repository, branch):
    """Reuse the open PR or create it; tolerate a create/lookup race."""
    number = find_pr(repository, branch)
    if number is not None:
        return number
    try:
        return create_pr(repository, branch)
    except (NotFound, ValueError):
        number = find_pr(repository, branch)
        if number is None:
            raise
        return number


def has_commits_after_base(repo, current, base):
    """True when the branch head carries commits beyond its merge base."""
    return current != git(repo, 'merge-base', current, base).stdout.decode().strip()


def select_candidate_base(existing, main_sha):
    """Use the current automation branch if present, else trusted main."""
    if existing is not None and (not isinstance(existing, str) or not SHA.fullmatch(existing)):
        raise ValueError('existing evidence branch head is not a commit SHA')
    if not isinstance(main_sha, str) or not SHA.fullmatch(main_sha):
        raise ValueError('trusted main revision is not a commit SHA')
    return main_sha if existing is None else existing


def checkout_candidate(repository, branch, main_sha, target, repo):
    """Create a detached candidate checkout at target; never run candidate code."""
    repo.mkdir()
    git(repo, 'init', '--quiet')
    git(repo, 'remote', 'add', 'origin', f'https://github.com/{repository}.git')
    git(repo, 'fetch', '--quiet', '--no-tags', 'origin', 'refs/heads/main')
    if revision(repo, 'FETCH_HEAD') != main_sha:
        raise ValueError('trusted main advanced while preparing; rerun the workflow')
    if target != main_sha:
        git(repo, 'fetch', '--quiet', '--no-tags', 'origin', f'refs/heads/{branch}')
        if revision(repo, 'FETCH_HEAD') != target:
            raise ValueError('evidence branch advanced while preparing; retry against the new head')
    # Reject links/submodules before any candidate file is read or validated.
    for entry in git(repo, 'ls-tree', '-r', '-z', target).stdout.split(b'\0'):
        if entry and entry.split(b' ', 1)[0] not in {b'100644', b'100755'}:
            raise ValueError('evidence checkout requires regular files; symlinks/submodules are unsupported')
    git(repo, 'checkout', '--quiet', '--detach', target)


def prepare(repository, branch, main_sha, output):
    repository_name(repository)
    if not isinstance(branch, str) or not BRANCH.fullmatch(branch):
        raise ValueError('invalid deterministic evidence branch')
    existing = branch_head(repository, branch)
    target = select_candidate_base(existing, main_sha)
    if output.exists() and any(output.iterdir()):
        raise ValueError('prepare output directory must be empty')
    output.mkdir(parents=True, exist_ok=True)
    repo = output / 'checkout'
    checkout_candidate(repository, branch, main_sha, target, repo)
    plan = {'version': 1, 'repository': repository, 'branch': branch,
            'existing': existing is not None, 'base': main_sha, 'head': target,
            'candidate': str(repo)}
    (output / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            stream.write(f'candidate={repo}\nhead={target}\nexisting={str(existing is not None).lower()}\n')
    print(f"Prepared candidate at {target} on branch {branch} "
          f"({'existing branch' if existing is not None else 'trusted main'})")
    return plan


def stage(candidate, trusted, snapshot):
    """Copy candidate data with trusted tools/schema/templates for validation.

    Trusted ``validate.py``/``build-site.py`` still read schemas and Jinja
    templates from the repository they validate. Overlaying the trusted copies
    keeps candidate branch content out of the validation runtime.
    """
    candidate = Path(candidate).resolve()
    trusted = Path(trusted).resolve()
    snapshot = Path(snapshot)
    if not candidate.is_dir() or not trusted.is_dir():
        raise ValueError('candidate and trusted checkouts are required')
    missing = [name for name in RUNTIME_INPUTS if not (trusted / name).is_dir()]
    if missing:
        raise ValueError(f'trusted validation inputs are missing: {", ".join(missing)}')
    if snapshot.exists() and any(snapshot.iterdir()):
        raise ValueError('staging output directory must be empty')
    if snapshot.resolve().is_relative_to(candidate):
        raise ValueError('staging snapshot must be outside the candidate checkout')
    snapshot.mkdir(parents=True, exist_ok=True)

    def ignore(directory, names):
        if Path(directory).resolve() == candidate:
            return [name for name in names if name in RUNTIME_INPUTS or name == '.build']
        return []

    shutil.copytree(candidate, snapshot, dirs_exist_ok=True, ignore=ignore)
    for name in RUNTIME_INPUTS:
        shutil.copytree(trusted / name, snapshot / name)
    print(f'Staged trusted validation snapshot at {snapshot}')
    return snapshot


def collect_changes(repo):
    """Reuse open-PR allowlisting and completeness checks for additive intake.

    Post-merge ingestion adds executions; it does not create recipes or remove
    prior evidence. Only open-PR selected-execution replacement supplies an
    explicit, validated deletion set to the shared collector.
    """
    files, deletions, _ = collect_evidence_changes(repo)
    if len(files) + len(deletions) > MAX_PUBLISH_FILES:
        raise ValueError('evidence exceeds atomic publication file limits')
    if sum(len(item['contents']) for item in files) > MAX_PUBLISH_BYTES:
        raise ValueError('evidence exceeds atomic publication byte limits')
    return files, deletions


def validate_plan(plan, candidate):
    if not isinstance(plan, dict) or set(plan) != PLAN_KEYS or plan.get('version') != 1:
        raise ValueError('invalid prepared evidence plan')
    repository = repository_name(plan['repository'])
    if not isinstance(plan['branch'], str) or not BRANCH.fullmatch(plan['branch']):
        raise ValueError('invalid deterministic evidence branch in plan')
    for key in ('base', 'head'):
        if not isinstance(plan[key], str) or not SHA.fullmatch(plan[key]):
            raise ValueError(f'invalid {key} revision in plan')
    if type(plan['existing']) is not bool:
        raise ValueError('invalid existing-branch flag in plan')
    if not isinstance(plan['candidate'], str):
        raise ValueError('invalid candidate path in plan')
    if not plan['existing'] and plan['head'] != plan['base']:
        raise ValueError('a new evidence branch must start at the trusted main revision')
    repo = Path(plan['candidate'])
    if repo.resolve() != Path(candidate).resolve():
        raise ValueError('prepared candidate path is inconsistent')
    if not repo.is_dir():
        raise ValueError('prepared candidate checkout is missing')
    return repository


def publish(plan, candidate):
    """Append prepared changes with CAS; return PR number and commit SHA."""
    repository = validate_plan(plan, candidate)
    repo = Path(candidate)
    if revision(repo) != plan['head']:
        raise ValueError('candidate checkout does not match the prepared head')
    files, deletions = collect_changes(repo)
    current = branch_head(repository, plan['branch'])
    if plan['existing']:
        if current != plan['head']:
            raise ValueError('evidence branch advanced after preparation; no commit was made')
    elif current is not None:
        raise ValueError('evidence branch appeared after preparation; no commit was made')
    number = find_pr(repository, plan['branch'])
    if plan['existing'] and number is None and has_closed_pr(repository, plan['branch']):
        if files or deletions:
            raise ValueError('evidence PR is closed; dispatch from current main for a new import branch')
        # A rerun after merge must not resurrect the old PR, including when its
        # commits were squash-merged and are not ancestors of main.
        return None, None
    if not files and not deletions:
        if number is None and plan['existing'] and has_commits_after_base(repo, current, plan['base']):
            # A prior attempt may have committed evidence and then failed to
            # open the PR. Retry must expose that branch instead of orphaning it.
            number = ensure_pr(repository, plan['branch'])
        return number, None
    changes = {'additions': files}
    if deletions:
        changes['deletions'] = deletions
    expected = plan['head']
    if not plan['existing']:
        expected = plan['base']
        # The ref exists before the mutation only so createCommitOnBranch has
        # a branch. It is created only when there is evidence to publish.
        create_ref(repository, plan['branch'], expected)
    data = graphql({'query': '''mutation($input: CreateCommitOnBranchInput!) {
      createCommitOnBranch(input: $input) { commit { oid url } }
    }''', 'variables': {'input': {
        'branch': {'repositoryNameWithOwner': repository, 'branchName': plan['branch']},
        'expectedHeadOid': expected,
        'message': {'headline': COMMIT},
        'fileChanges': changes}}})
    commit = data['createCommitOnBranch']['commit']
    number = ensure_pr(repository, plan['branch'])
    return number, commit['oid']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    preparation = commands.add_parser('prepare')
    preparation.add_argument('--repository', required=True)
    preparation.add_argument('--branch', required=True)
    preparation.add_argument('--main-sha', required=True)
    preparation.add_argument('--output', required=True, type=Path)
    publication = commands.add_parser('publish')
    publication.add_argument('--plan', required=True, type=Path)
    staging = commands.add_parser('stage')
    staging.add_argument('--candidate', required=True, type=Path)
    staging.add_argument('--trusted', required=True, type=Path)
    staging.add_argument('--snapshot', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        output = args.output.resolve()
    elif args.command == 'publish':
        output = args.plan.resolve().parent
    else:
        output = args.snapshot.resolve().parent
    try:
        if args.command == 'prepare':
            prepare(args.repository, args.branch, args.main_sha, output)
        elif args.command == 'stage':
            stage(args.candidate, args.trusted, args.snapshot)
        else:
            plan_path = args.plan.resolve()
            plan = json.loads(plan_path.read_text())
            number, commit = publish(plan, plan_path.parent / 'checkout')
            if os.environ.get('GITHUB_OUTPUT') and number is not None:
                with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
                    stream.write(f'pull-request-number={number}\n')
                    if commit:
                        stream.write(f'commit-sha={commit}\n')
            if number is None:
                print('No evidence change and no open evidence PR to report')
            elif commit:
                print(f'Published {commit} to PR #{number}')
            else:
                print(f'No evidence change; PR #{number} is unchanged')
        return 0
    except Exception as error:
        message = str(error)
        for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'MLFLOW_USERNAME', 'MLFLOW_PASSWORD'):
            if os.environ.get(key):
                message = message.replace(os.environ[key], '[redacted]')
        message = f'Post-merge evidence {args.command} failed: {type(error).__name__}: {message}'
        print(message, file=sys.stderr)
        output.mkdir(parents=True, exist_ok=True)
        with (output / 'summary.md').open('a') as stream:
            stream.write('\n' + message + '\n')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
