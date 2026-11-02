#!/usr/bin/env python3
"""Verify that every commit in a pull request has a GitHub-verified signature.

Uses only the GitHub REST API through the authenticated ``gh`` CLI. The pull
request commits endpoint reports at most 250 commits, so a larger history is
rejected instead of being silently truncated.
"""
import argparse
import json
import re
import subprocess
import sys

PAGE_SIZE = 100
MAX_PR_COMMITS = 250
SHA = re.compile(r'[0-9a-f]{40}')
REPOSITORY = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9_.-]*')


class VerificationError(Exception):
    """A PR cannot be proven to contain only GitHub-verified commits."""


class APIError(VerificationError):
    """GitHub could not be queried reliably."""


def gh_json(endpoint):
    """Return decoded JSON from one GitHub REST request, or fail closed."""
    try:
        result = subprocess.run(['gh', 'api', endpoint], capture_output=True, text=True)
    except OSError as error:
        raise APIError(f'could not run gh: {error}') from error
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or 'no error output'
        raise APIError(f'GitHub API request failed ({endpoint}): {detail}')
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise APIError(f'GitHub API returned malformed JSON ({endpoint})') from error


def commit_verification(commit):
    """Validate one PR commit entry and return (sha, author, verified, reason)."""
    if not isinstance(commit, dict):
        raise VerificationError('commit list contains a non-object entry')
    sha = commit.get('sha')
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise VerificationError('commit list contains an invalid or missing SHA')
    details = commit.get('commit')
    verification = details.get('verification') if isinstance(details, dict) else None
    if not isinstance(verification, dict):
        raise VerificationError(f'{sha}: GitHub verification metadata is missing')
    verified = verification.get('verified')
    reason = verification.get('reason')
    if not isinstance(verified, bool):
        raise VerificationError(f'{sha}: GitHub verification status is malformed')
    if not isinstance(reason, str) or not reason.strip():
        raise VerificationError(f'{sha}: GitHub verification reason is missing')
    author = 'unknown'
    for identity in (commit.get('author'), details.get('author')):
        if isinstance(identity, dict):
            label = identity.get('login') or identity.get('name')
            if isinstance(label, str) and label.strip():
                # Commit author names are user-controlled; keep CI logs single-line.
                author = re.sub(r'[^A-Za-z0-9_.@+\[\] ()-]', '_', label.strip())[:100] or 'unknown'
                break
    return sha, author, verified, reason.strip()


def fetch_commits(api, repository, number, expected):
    """Fetch every commit page; the caller must reject a truncated result."""
    commits = []
    page = 1
    while len(commits) < expected:
        endpoint = (f'repos/{repository}/pulls/{number}/commits'
                    f'?per_page={PAGE_SIZE}&page={page}')
        batch = api(endpoint)
        if not isinstance(batch, list):
            raise VerificationError('GitHub PR commits response is not a list')
        if not batch:
            break
        commits.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        page += 1
    return commits


def verify(repository, number, api=None, expected_head=None):
    """Print the verification reason for every commit, then fail on any gap."""
    api = api or gh_json
    if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        raise VerificationError('invalid GitHub repository name')
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise VerificationError('PR number must be a positive integer')

    endpoint = f'repos/{repository}/pulls/{number}'
    snapshot = api(endpoint)
    if not isinstance(snapshot, dict):
        raise VerificationError('GitHub pull request response is not an object')
    head = snapshot.get('head', {}).get('sha') if isinstance(snapshot.get('head'), dict) else None
    if not isinstance(head, str) or not SHA.fullmatch(head):
        raise VerificationError('PR head SHA is missing or malformed')
    if expected_head is not None and head != expected_head:
        raise VerificationError('PR head differs from the triggering event; rerun for the current head')
    expected = snapshot.get('commits')
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
        raise VerificationError('PR commit count is missing or malformed')
    if expected > MAX_PR_COMMITS:
        raise VerificationError(
            f'PR reports {expected} commits, but the GitHub PR commits endpoint reports '
            f'at most {MAX_PR_COMMITS}; refusing to verify a truncated commit list')

    commits = fetch_commits(api, repository, number, expected)
    if len(commits) != expected:
        raise VerificationError(
            f'PR reports {expected} commits but the API returned {len(commits)}; '
            'refusing to verify a truncated commit list')

    shas, failures = set(), []
    for commit in commits:
        sha, author, verified, reason = commit_verification(commit)
        if sha in shas:
            raise VerificationError(f'{sha}: duplicate commit in PR history')
        shas.add(sha)
        print(f'{sha}: {"verified" if verified else "unverified"} reason={reason} author={author}',
              flush=True)
        if not verified or reason != 'valid':
            failures.append(f'{sha} ({reason}, {author})')

    last = commits[-1].get('sha') if commits else None
    if last != head:
        raise VerificationError(
            f'commit list ends at {last}, not the PR head {head}; '
            'refusing to verify a truncated or reordered list')

    current = api(endpoint)
    current_head = (current.get('head', {}).get('sha')
                    if isinstance(current, dict) and isinstance(current.get('head'), dict) else None)
    if current_head != head:
        raise VerificationError(
            f'PR head changed during verification ({head} -> {current_head}); rerun against the new head')
    before_base, after_base = snapshot.get('base') or {}, current.get('base') or {}
    if (current.get('commits') != expected or
            any(before_base.get(key) != after_base.get(key) for key in ('sha', 'ref'))):
        raise VerificationError('PR base or commit count changed during verification; rerun')

    if failures:
        raise VerificationError('unverified PR commits: ' + ', '.join(failures))
    print(f'PR #{number}: all {len(commits)} commits verified at head {head}')
    return len(commits)


def positive_int(value):
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError('must be a positive integer') from error
    if number < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True, help='owner/repository')
    parser.add_argument('--pr-number', required=True, type=positive_int)
    parser.add_argument('--expected-head', help='reject an outdated PR event head')
    args = parser.parse_args(argv)
    try:
        verify(args.repository, args.pr_number, expected_head=args.expected_head)
    except VerificationError as error:
        print(f'PR signature verification failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
