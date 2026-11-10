#!/usr/bin/env python3
"""Prepare a private evidence review, then explicitly publish its exact reviewed diff."""
from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from urllib.parse import quote

import jsonschema

from contributor_bundle import checksum, json_bytes, json_load, read_archive
from import_contributor import import_bundle, plan_import, write_import_plan

TOOLS = Path(__file__).resolve().parent


def command(argv, *, cwd=None):
    result = subprocess.run([str(arg) for arg in argv], cwd=cwd, capture_output=True, timeout=300)
    if result.returncode:
        # Neither Git remote credentials nor arbitrary validator output belongs
        # in public reports. Keep failures local and name only the failed step.
        step = argv[3] if Path(str(argv[0])).name == 'git' and argv[1] == '-C' else Path(str(argv[1])).name
        raise ValueError(f'{Path(str(argv[0])).name} {step} failed (exit {result.returncode})')
    return result.stdout


def git(repo, *args):
    return command(['git', '-C', repo, *args])


def revision(repo, ref='HEAD'):
    return git(repo, 'rev-parse', '--verify', '--end-of-options', ref + '^{commit}').decode().strip()


def names(data):
    return set(filter(None, data.decode().split('\0')))


def origin_repository(repo):
    remote = git(repo, 'remote', 'get-url', '--push', '--all', 'origin').decode().strip()
    for prefix in ('git@github.com:', 'https://github.com/'):
        if remote.startswith(prefix):
            name = remote[len(prefix):].removesuffix('.git')
            if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', name):
                return name
    raise ValueError('origin must have one credential-free github.com SSH/HTTPS push URL')


def review_directory(worktree):
    path = Path(git(worktree, 'rev-parse', '--git-path', 'contributor-review').decode().strip())
    return path if path.is_absolute() else worktree / path


def save_receipt(directory, receipt):
    temporary = directory / 'receipt.tmp'
    temporary.write_bytes(json_bytes(receipt))
    temporary.chmod(0o600)
    temporary.replace(directory / 'receipt.json')


def validate_preview(worktree, recipe):
    # Always execute maintainer-controlled tools, not tools from a selected
    # source branch or from the returned bundle. The base must still be trusted.
    for tool, flags in (
        ('validate.py', ['--current']),
        ('check_manifests.py', [str(Path(recipe).parent)]),
        ('catalog.py', ['--out', str(worktree / '.build/site/catalog.json')]),
    ):
        command([sys.executable, TOOLS / tool, '--repo', worktree, *flags], cwd=worktree)


def planned_files(worktree, plan):
    return {
        str(plan.recipe.relative_to(worktree)): plan.updated.encode(),
        **{str((plan.directory / name).relative_to(worktree)): data for name, data in plan.files.items()},
    }


def summary(worktree, receipt):
    private = review_directory(worktree)
    return {
        'worktree': str(worktree), 'branch': receipt['branch'],
        'run_id': receipt['run_id'], 'checksum': receipt['checksum'],
        'recipe': receipt['recipe'], 'tested_revision': receipt['request']['tested_revision'],
        'platform': receipt['request']['platform'], 'workload': receipt['request']['workload'],
        'runner_image': receipt['request']['runner_image'], 'harness_image': receipt['request']['harness_image'],
        'completeness': receipt['completeness'], 'maturity_changed': False,
        'review_native_exports': str(private / 'native'),
        'review_diff': f'git -C {shlex.quote(str(worktree))} diff --cached',
        's3_upload_pending': bool(receipt['s3_prefix'] and not receipt['commit']),
        's3_prefix': receipt['s3_prefix'],
        'commit': receipt['commit'], 'pr_url': receipt['pr_url'],
    }


def prepare_review(repo, bundle, digest, worktree, *, base, github_repo, pr_base='main', s3_prefix=None):
    repo, bundle = repo.resolve(), bundle.resolve()
    worktree = worktree.absolute()
    if worktree.exists() or worktree.is_symlink():
        raise ValueError('review worktree already exists; inspect it or resume with publish')
    if git(repo, 'status', '--porcelain', '--untracked-files=all').strip():
        raise ValueError('source checkout must be clean; use a trusted clean checkout')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', github_repo):
        raise ValueError('explicit GitHub OWNER/REPO required')
    if origin_repository(repo).lower() != github_repo.lower():
        raise ValueError('origin push repository differs from --github-repo; fork publication is unsupported')
    git(repo, 'check-ref-format', 'refs/heads/' + pr_base)
    base_sha = revision(repo, base)
    envelope, files = read_archive(bundle, digest)
    if envelope['outcome'] != 'succeeded':
        raise ValueError('only successful bundles can enter evidence review')
    branch = 'evidence/' + envelope['request']['run_id']
    git(repo, 'worktree', 'add', '-b', branch, worktree, base_sha)
    private = review_directory(worktree)
    private.mkdir(mode=0o700)
    archive = private / 'evidence.tar.gz'
    shutil.copyfile(bundle, archive)
    archive.chmod(0o600)
    plan = plan_import(worktree, archive, digest, s3_prefix=s3_prefix)
    expected = planned_files(worktree, plan)
    # This is an unreviewed, local preview only. No --reviewed claim, upload,
    # commit, or PR is made by materializing the importer's validated plan.
    write_import_plan(plan)
    for name, data in files.items():
        if name.startswith('raw/'):
            target = private / 'native' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o600)
    git(worktree, 'add', '--', *sorted(expected))
    changed = names(git(worktree, 'diff', '--cached', '--name-only', '-z'))
    if not changed:
        raise ValueError('bundle is already imported in the selected base; no evidence PR is needed')
    if not changed.issubset(expected):
        raise ValueError('preview changed files outside the import plan')
    receipt = {
        'schema_version': 1, 'base_commit': base_sha, 'branch': branch,
        'github_repo': github_repo, 'pr_base': pr_base, 'checksum': digest,
        'run_id': plan.report['run_id'], 'completeness': plan.report['completeness'],
        'request': envelope['request'],
        'recipe': envelope['request']['recipe_path'], 's3_prefix': s3_prefix,
        'expected': {name: checksum(data) for name, data in expected.items()},
        'changed': sorted(changed), 'commit': None, 'pr_url': None,
    }
    save_receipt(private, receipt)
    verify_review(worktree, receipt)
    # Preserve a resumable review if local validation prerequisites fail.
    # Publication always reruns these checks before any commit or upload.
    validate_preview(worktree, envelope['request']['recipe_path'])
    verify_review(worktree, receipt)
    return summary(worktree, receipt)


def verify_review(worktree, receipt):
    if not isinstance(receipt, dict) or type(receipt.get('schema_version')) is not int or receipt['schema_version'] != 1:
        raise ValueError('unsupported local review receipt')
    if git(worktree, 'branch', '--show-current').decode().strip() != receipt['branch']:
        raise ValueError('review branch changed')
    if origin_repository(worktree).lower() != receipt['github_repo'].lower():
        raise ValueError('origin push repository changed since preparation')
    head = revision(worktree)
    if head != (receipt['commit'] or receipt['base_commit']):
        raise ValueError('review HEAD changed; prepare a fresh review rather than mixing commits')
    if receipt['commit']:
        if revision(worktree, 'HEAD^') != receipt['base_commit']:
            raise ValueError('publication commit does not have the selected review base')
        if names(git(worktree, 'diff', '--name-only', '-z', receipt['base_commit'], 'HEAD')) != set(receipt['changed']):
            raise ValueError('publication commit contains files outside the reviewed diff')
    if git(worktree, 'diff', '--name-only', '-z') or git(worktree, 'ls-files', '--others', '--exclude-standard', '-z'):
        raise ValueError('review worktree has unstaged or untracked changes')
    staged = names(git(worktree, 'diff', '--cached', '--name-only', '-z'))
    if staged != (set() if receipt['commit'] else set(receipt['changed'])):
        raise ValueError('staged files differ from the prepared review')
    entries = git(worktree, 'ls-files', '--stage', '-z', '--', *sorted(receipt['expected'])).decode().split('\0')
    for entry in filter(None, entries):
        metadata, _ = entry.split('\t', 1)
        mode, _, stage = metadata.split()
        if mode != '100644' or stage != '0':
            raise ValueError('reviewed files must remain regular, non-executable Git files')
    for name, digest in receipt['expected'].items():
        path = worktree / name
        if path.is_symlink() or checksum(path.read_bytes()) != digest:
            raise ValueError('review files changed; prepare a fresh review')
        data = git(worktree, 'show', f'HEAD:{name}' if receipt['commit'] else f':{name}')
        if checksum(data) != digest:
            raise ValueError('reviewed Git bytes differ from the prepared files')
    plan = plan_import(worktree, review_directory(worktree) / 'evidence.tar.gz',
                       receipt['checksum'], s3_prefix=receipt['s3_prefix'])
    current = {name: checksum(data) for name, data in planned_files(worktree, plan).items()}
    if current != receipt['expected']:
        raise ValueError('replayed import plan differs from the prepared review')


def publish_review(worktree, *, reviewed=False):
    if not reviewed:
        raise ValueError('publish requires --reviewed after privacy/provenance and staged-diff review')
    worktree = worktree.resolve()
    private = review_directory(worktree)
    receipt = json_load((private / 'receipt.json').read_bytes())
    verify_review(worktree, receipt)
    validate_preview(worktree, receipt['recipe'])
    verify_review(worktree, receipt)
    if receipt['pr_url']:
        return summary(worktree, receipt)
    # A PR against an unrelated/older base could include source commits the
    # staged evidence review never showed. Permit the selected base or a later
    # descendant, but never silently broaden the reviewed diff.
    ahead = command([
        'gh', 'api', f'repos/{receipt["github_repo"]}/compare/{quote(receipt["pr_base"], safe="")}...{receipt["base_commit"]}',
        '--jq', '.ahead_by',
    ], cwd=worktree).decode().strip()
    if ahead != '0':
        raise ValueError('GitHub PR base does not contain the reviewed base; no source commits were implicitly approved')
    if not receipt['commit']:
        # Upload, if requested, only after explicit review and every local check.
        # The existing importer owns conditional upload, checksum, and retry rules.
        import_bundle(worktree, private / 'evidence.tar.gz', receipt['checksum'],
                      apply=True, reviewed=True, s3_prefix=receipt['s3_prefix'])
        verify_review(worktree, receipt)
        git(worktree, 'commit', '-m', f'feat(evidence): import {receipt["run_id"]}')
        receipt['commit'] = revision(worktree)
        save_receipt(private, receipt)
    verify_review(worktree, receipt)
    git(worktree, 'push', 'origin', f'{receipt["commit"]}:refs/heads/{receipt["branch"]}')
    existing = json_load(command([
        'gh', 'pr', 'list', '--repo', receipt['github_repo'], '--head', receipt['branch'],
        '--base', receipt['pr_base'], '--state', 'all', '--json', 'url,headRefOid',
    ], cwd=worktree))
    if existing:
        if len(existing) != 1 or existing[0]['headRefOid'] != receipt['commit']:
            raise ValueError('existing PR differs from the reviewed commit; no duplicate PR was created')
        url = existing[0]['url']
    else:
        body = (
            f'## Reviewed contributor evidence\n\nRun: `{receipt["run_id"]}`\n\n'
            f'Recipe: `{receipt["recipe"]}`\n\nBundle: `{receipt["checksum"]}`\n\n'
            f'Completeness: `{receipt["completeness"]}`. Privacy/provenance and staged diff reviewed; '
            'offline replay, manifest drift, and catalog validation passed.\n\n'
            'Contributor-controlled execution, not independent validation. '
            'No recipe maturity change or automatic merge.\n'
        )
        url = command([
            'gh', 'pr', 'create', '--repo', receipt['github_repo'], '--head', receipt['branch'],
            '--base', receipt['pr_base'], '--title', f'Import contributor evidence: {receipt["run_id"]}',
            '--body', body,
        ], cwd=worktree).decode().strip()
    if not re.fullmatch(r'https://github\.com/' + re.escape(receipt['github_repo']) + r'/pull/[0-9]+', url):
        raise ValueError('GitHub returned an unexpected PR URL; inspect before retrying')
    receipt['pr_url'] = url
    save_receipt(private, receipt)
    return summary(worktree, receipt)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    prepare = actions.add_parser('prepare', help='create a private, staged review; no upload or publication')
    prepare.add_argument('bundle', type=Path)
    digest = prepare.add_mutually_exclusive_group(required=True)
    digest.add_argument('--checksum')
    digest.add_argument('--checksum-file', type=Path)
    prepare.add_argument('--repo', type=Path, default=Path.cwd())
    prepare.add_argument('--base', default='origin/main', help='already available, maintainer-trusted ref; never auto-fetched')
    prepare.add_argument('--worktree', required=True, type=Path)
    prepare.add_argument('--github-repo', required=True, help='OWNER/REPO; must match origin push URL')
    prepare.add_argument('--pr-base', default='main')
    prepare.add_argument('--s3-prefix', help='optional durable archive destination; upload deferred until reviewed publication')
    publish = actions.add_parser('publish', help='revalidate, commit, push, and open/reuse a PR')
    publish.add_argument('worktree', type=Path)
    publish.add_argument('--reviewed', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.action == 'prepare':
            digest = args.checksum_file.read_text().strip() if args.checksum_file else args.checksum
            report = prepare_review(args.repo, args.bundle, digest, args.worktree, base=args.base,
                                    github_repo=args.github_repo, pr_base=args.pr_base, s3_prefix=args.s3_prefix)
        else:
            report = publish_review(args.worktree, reviewed=args.reviewed)
        print(json.dumps(report, indent=2))
        return 0
    except (ValueError, KeyError, TypeError, OSError, EOFError, RecursionError, tarfile.TarError,
            jsonschema.ValidationError, subprocess.SubprocessError) as error:
        print(f'Maintainer flow stopped: {error}. Local review files are preserved; nothing was force-reset.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
