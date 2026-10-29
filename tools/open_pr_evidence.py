#!/usr/bin/env python3
"""Prepare trusted Forge evidence for an open PR, then publish with a head-SHA guard."""
import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode

from fetch_forge_evidence import Client, import_parent
from forge_admission import FILES, RECEIPT, admitted_execution, load_admissions, origin
from ingest_forge import github, selected_run
from recipe_evidence import load_unique_yaml


TRUSTED = Path(__file__).resolve().parents[1]
MAX_PUBLISH_BYTES = 8 * 1024 * 1024
MAX_PUBLISH_FILES = 100
SHA = r'[a-f0-9]{40}'
RECIPE = r'models/[a-z0-9][a-z0-9._-]*/recipes/[a-z0-9][a-z0-9._-]*'


def repository_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', value):
        raise ValueError('invalid GitHub repository name')
    return value


def git(repo, *args, check=True):
    # Candidate .gitattributes must not activate machine-level filters or helpers.
    env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull}
    return subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull, '-c', 'core.fsmonitor=false',
                           '-C', str(repo), *args], env=env, check=check, capture_output=True)


def maintainer(repository, actor):
    if not isinstance(actor, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?', actor):
        raise ValueError('a GitHub maintainer actor is required')
    permission = github(f'repos/{repository}/collaborators/{actor}/permission')['permission']
    if permission not in {'admin', 'maintain', 'write'}:
        raise ValueError('only repository maintainers may request an evidence import')


def pr_snapshot(repository, number):
    pr = github(f'repos/{repository}/pulls/{number}')
    if pr.get('state') != 'open' or pr.get('merged_at') or pr['base']['ref'] != 'main':
        raise ValueError('target must be an open PR against main')
    if pr['base']['repo']['full_name'] != repository or not pr['head'].get('repo'):
        raise ValueError('PR repository attribution is missing or inconsistent')
    head_repository = repository_name(pr['head']['repo']['full_name'])
    branch, head = pr['head']['ref'], pr['head']['sha']
    if not isinstance(branch, str) or not branch or not re.fullmatch(SHA, head):
        raise ValueError('invalid PR head identity')
    if head_repository == repository and branch == 'main':
        raise ValueError('evidence publication must never target main')
    return {'head_repository': head_repository, 'head_branch': branch, 'head_sha': head}


def report(repository, number, comment_id, tracking_uri, workspace):
    comment = github(f'repos/{repository}/issues/comments/{comment_id}')
    if comment.get('issue_url') != f'https://api.github.com/repos/{repository}/issues/{number}':
        raise ValueError('selected Forge report does not belong to the target PR')
    selected = selected_run([comment], tracking_uri, workspace)
    if selected is None:
        raise ValueError('selected comment is not a trusted successful Forge report; select a successful attempt')
    run_id, selected_comment, selected_workspace = selected
    if selected_comment != comment_id:
        raise ValueError('selected Forge comment identity mismatch')
    return {'run_id': run_id, 'comment_id': comment_id, 'workspace': selected_workspace,
            'report_sha256': hashlib.sha256(comment['body'].encode()).hexdigest()}


def checkout_candidate(repository, number, head, repo):
    repo.mkdir()
    git(repo, 'init', '--quiet')
    git(repo, 'remote', 'add', 'origin', f'https://github.com/{repository}.git')
    git(repo, 'fetch', '--quiet', '--no-tags', 'origin', f'refs/pull/{number}/head')
    if git(repo, 'rev-parse', 'FETCH_HEAD').stdout.decode().strip() != head:
        raise ValueError('PR advanced while fetching; retry against the new head')
    # Reject links/submodules before reading candidate files or running validators.
    for entry in git(repo, 'ls-tree', '-r', '-z', head).stdout.split(b'\0'):
        if entry and entry.split(b' ', 1)[0] not in {b'100644', b'100755'}:
            raise ValueError('evidence checkout requires regular files; symlinks/submodules are unsupported')
    git(repo, 'checkout', '--quiet', '--detach', head)


def trusted_inputs(repo, trusted):
    # Validators read schemas, and Jinja executes templates. Neither may come
    # from an unreviewed PR. Candidate tools are never imported or executed.
    for directory in ('schema', 'templates'):
        def inventory(root):
            files = {}
            for path in (root / directory).rglob('*'):
                if path.is_symlink():
                    raise ValueError('trusted validation inputs cannot be symlinks')
                if path.is_file():
                    files[path.relative_to(root).as_posix()] = path.read_bytes()
            return files
        if inventory(repo) != inventory(trusted):
            raise ValueError(f'candidate {directory}/ differs from trusted main; merge the reviewed tooling first')


def recorded_revision(client, run_id, number):
    run = client.run(run_id)
    if run['info']['run_id'] != run_id or run['info']['status'] != 'FINISHED':
        raise ValueError('selected Forge execution is not successful; fix the recipe and rerun Forge')
    if any(t['key'] == 'mlflow.parentRunId' for t in run.get('data', {}).get('tags', [])):
        raise ValueError('select the primary Forge report, not a benchmark child run')
    raw = client.request('/get-artifact?' + urlencode({
        'run_id': run_id, 'path': '01__test/000__ci_metadata/fournos_fjob.yaml'}), raw=True)
    env = load_unique_yaml(raw.decode())['spec']['env']
    if str(env.get('FORGE_FOREIGN_TESTING_PULL_NUMBER')) != str(number):
        raise ValueError('recorded Forge execution belongs to another PR')
    revision = env.get('FORGE_FOREIGN_TESTING_PULL_PULL_SHA')
    if not isinstance(revision, str) or not re.fullmatch(SHA, revision):
        raise ValueError('Forge execution does not record an exact tested recipe SHA')
    return revision


def validate_candidate(repo, trusted):
    environment = {key: value for key, value in os.environ.items()
                   if key not in {'MLFLOW_USERNAME', 'MLFLOW_PASSWORD', 'GH_TOKEN', 'GITHUB_TOKEN'}}
    for tool, flags in [('validate.py', ['--current', '--require-converted-raw']),
                        ('check_manifests.py', ['--all'])]:
        subprocess.run([sys.executable, str(trusted / 'tools' / tool), '--repo', str(repo), *flags],
                       cwd=trusted, env=environment, check=True)
    from catalog import build_catalog
    build_catalog(repo, {'source_sha': git(repo, 'rev-parse', 'HEAD').stdout.decode().strip(), 'dirty': True})


def evidence_path(path):
    if re.fullmatch(RECIPE + r'/recipe\.yaml', path):
        return 'recipe'
    match = re.fullmatch(RECIPE + r'/results/forge-[a-f0-9]{32}-(?:guidellm|aiperf)/([^/]+)', path)
    if match and match[1] in FILES | {RECEIPT}:
        return 'result'
    raise ValueError('publication contains a path outside the evidence allowlist')


def collect_changes(repo):
    files = []
    for entry in git(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all').stdout.split(b'\0'):
        if not entry:
            continue
        status, path = entry[:2].decode(), entry[3:].decode()
        if status not in {' M', '??'}:
            raise ValueError('evidence import produced an unexpected deletion, rename or staged change')
        kind = evidence_path(path)
        destination = repo / path
        if destination.is_symlink() or destination.resolve() != destination or not destination.is_file():
            raise ValueError('evidence output must be a canonical regular file')
        before = git(repo, 'show', f'HEAD:{path}', check=False)
        raw = destination.read_bytes()
        if kind == 'recipe':
            if before.returncode:
                raise ValueError('evidence import cannot create a recipe')
            old, new = load_unique_yaml(before.stdout.decode()), load_unique_yaml(raw.decode())
            old_refs, new_refs = old.pop('benchmark_runs', []), new.pop('benchmark_runs', [])
            if old != new or not set(old_refs).issubset(new_refs):
                raise ValueError('evidence import changed recipe inputs or removed historical references')
            for reference in new_refs:
                if reference not in old_refs:
                    if evidence_path(str(Path(path).parent / reference)) != 'result' or not reference.endswith('/run.yaml'):
                        raise ValueError('recipe contains an invalid new evidence reference')
        elif before.returncode == 0:
            raise ValueError('historical evidence is immutable; refusing to replace a run')
        files.append({'path': path, 'contents': base64.b64encode(raw).decode()})
    files.sort(key=lambda item: item['path'])
    if files:
        git(repo, 'add', '--', *(item['path'] for item in files))
    patch = git(repo, 'diff', '--cached', '--binary', '--no-ext-diff', '--no-textconv', 'HEAD', '--').stdout
    return files, patch


def prepare(repository, number, comment_id, output, actor, insecure=False):
    repository_name(repository)
    if number < 1 or comment_id < 1:
        raise ValueError('PR number and report comment ID must be positive')
    maintainer(repository, actor)
    snapshot = pr_snapshot(repository, number)
    uri = os.environ['MLFLOW_TRACKING_URI']
    selected = report(repository, number, comment_id, uri, os.environ.get('MLFLOW_WORKSPACE', 'forge-sandbox'))
    if output.exists() and any(output.iterdir()):
        raise ValueError('prepare output directory must be empty')
    output.mkdir(parents=True, exist_ok=True)
    repo = output / 'checkout'
    checkout_candidate(repository, number, snapshot['head_sha'], repo)
    trusted_inputs(repo, TRUSTED)
    client = Client(uri, selected['workspace'], os.environ['MLFLOW_USERNAME'], os.environ['MLFLOW_PASSWORD'], insecure)
    admissions = load_admissions(repo)
    admission = admissions.get((origin(uri), selected['workspace'], selected['run_id']))
    revision = admission['receipt']['tested_revision'] if admission else recorded_revision(client, selected['run_id'], number)
    if git(repo, 'merge-base', '--is-ancestor', revision, snapshot['head_sha'], check=False).returncode:
        raise ValueError('tested recipe SHA is not in this PR head history; select a matching run or rerun Forge')
    print(f"PR #{number}: selected run {selected['run_id']}; tested {revision}; current head {snapshot['head_sha']}")
    if not admitted_execution(repo, admissions, client, selected['run_id'], revision, number, {}):
        import_parent(client, repo, selected['run_id'], revision, number, comment_id,
                      require_parent=True, admissions=admissions)
    validate_candidate(repo, TRUSTED)
    files, patch = collect_changes(repo)
    small = len(files) <= MAX_PUBLISH_FILES and sum(len(item['contents']) for item in files) <= MAX_PUBLISH_BYTES
    mode = 'unchanged' if not files else ('publish' if snapshot['head_repository'] == repository and small else 'patch')
    plan = {'version': 1, 'repository': repository, 'pr_number': number, **snapshot, **selected,
            'tracking_uri': uri, 'tested_revision': revision, 'mode': mode, 'files': files}
    (output / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    (output / 'evidence.patch').write_bytes(patch)
    summary = (f"## Forge evidence for PR #{number}\n\n"
               f"- Selected run: `{selected['run_id']}`\n- Tested recipe: `{revision}`\n"
               f"- Prepared against PR head: `{snapshot['head_sha']}`\n- Files: {len(files)}\n"
               f"- Delivery: **{mode}**\n\n")
    if mode == 'patch':
        summary += ('Download the evidence artifact and apply it to the exact prepared head:\n\n'
                    f'```bash\ntest "$(git rev-parse HEAD)" = "{snapshot["head_sha"]}"\n'
                    'git apply --check evidence.patch && git apply evidence.patch\n```\n'
                    'Commit the evidence to the existing PR, then run normal PR validation.\n')
    elif mode == 'unchanged':
        summary += 'This execution is already indexed and remains applicable. No commit is needed.\n'
    (output / 'summary.md').write_text(summary)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            stream.write(f'publishable={str(mode == "publish").lower()}\n')
    print(summary)
    return plan


def graphql(payload):
    response = subprocess.run(['gh', 'api', 'graphql', '--input', '-'],
                              input=json.dumps(payload), capture_output=True, text=True, check=True)
    result = json.loads(response.stdout)
    if result.get('errors'):
        raise ValueError('GitHub rejected the guarded evidence commit; the branch may have advanced. Retry preparation.')
    return result['data']


def publish(plan, actor):
    if plan.get('version') != 1 or plan.get('mode') not in {'publish', 'patch', 'unchanged'}:
        raise ValueError('invalid prepared evidence plan')
    if plan['mode'] != 'publish':
        return None
    repository = repository_name(plan['repository'])
    maintainer(repository, actor)
    current = pr_snapshot(repository, plan['pr_number'])
    if current != {key: plan[key] for key in current}:
        raise ValueError('PR changed after preparation; no commit was made. Retry against the new head.')
    if current['head_repository'] != repository:
        raise ValueError('fork evidence must be delivered as a patch')
    selected = report(repository, plan['pr_number'], plan['comment_id'], plan['tracking_uri'], plan['workspace'])
    if selected != {key: plan[key] for key in selected}:
        raise ValueError('Forge report changed after preparation; revalidate the selected execution')
    files = plan['files']
    if not files or len(files) > MAX_PUBLISH_FILES or sum(len(item['contents']) for item in files) > MAX_PUBLISH_BYTES:
        raise ValueError('evidence exceeds atomic publication limits; use the prepared patch')
    paths = set()
    for item in files:
        evidence_path(item['path'])
        if item['path'] in paths:
            raise ValueError('duplicate evidence publication path')
        paths.add(item['path'])
        base64.b64decode(item['contents'], validate=True)
    # GitHub signs commits it creates. expectedHeadOid is an atomic compare-and-
    # swap, so a push racing this request cannot be overwritten or force-pushed.
    data = graphql({'query': '''mutation($input: CreateCommitOnBranchInput!) {
      createCommitOnBranch(input: $input) { commit { oid url } }
    }''', 'variables': {'input': {
        'branch': {'repositoryNameWithOwner': repository, 'branchName': current['head_branch']},
        'expectedHeadOid': current['head_sha'],
        'message': {'headline': f"feat(evidence): import Forge results for PR #{plan['pr_number']}",
                    'body': f"Forge run: {plan['run_id']}\nTested recipe: {plan['tested_revision']}"},
        'fileChanges': {'additions': files}}}})
    return data['createCommitOnBranch']['commit']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    preparation = commands.add_parser('prepare')
    preparation.add_argument('--repository', required=True)
    preparation.add_argument('--pr-number', required=True, type=int)
    preparation.add_argument('--comment-id', required=True, type=int)
    preparation.add_argument('--output', required=True, type=Path)
    preparation.add_argument('--actor', required=True)
    preparation.add_argument('--insecure', action='store_true')
    publication = commands.add_parser('publish')
    publication.add_argument('--plan', required=True, type=Path)
    publication.add_argument('--actor', required=True)
    args = parser.parse_args()
    output = args.output.resolve() if args.command == 'prepare' else args.plan.resolve().parent
    try:
        if args.command == 'prepare':
            prepare(args.repository, args.pr_number, args.comment_id, output, args.actor, args.insecure)
        else:
            commit = publish(json.loads(args.plan.read_text()), args.actor)
            if commit:
                message = f"Published evidence commit: {commit['url']}\n"
                with (output / 'summary.md').open('a') as stream:
                    stream.write('\n' + message)
                print(message)
        return 0
    except Exception as error:
        message = str(error)
        for key in ('MLFLOW_USERNAME', 'MLFLOW_PASSWORD', 'GH_TOKEN', 'GITHUB_TOKEN'):
            if os.environ.get(key):
                message = message.replace(os.environ[key], '[redacted]')
        message = f'Evidence {args.command} failed: {type(error).__name__}: {message}'
        print(message, file=sys.stderr)
        output.mkdir(parents=True, exist_ok=True)
        with (output / 'summary.md').open('a') as stream:
            stream.write('\n' + message + '\n\nFix the reported mismatch or select a matching successful Forge run, then retry.\n')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
