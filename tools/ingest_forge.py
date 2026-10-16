#!/usr/bin/env python3
"""Discover merged PRs and trusted Forge comments, then import their evidence."""
import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from fetch_forge_evidence import Client, import_parent


def github(endpoint, paginated=False):
    command = ['gh', 'api', endpoint]
    if paginated:
        command.extend(['--paginate', '--slurp'])
    value = json.loads(subprocess.run(command, check=True, capture_output=True, text=True).stdout)
    return [item for page in value for item in page] if paginated else value


def comment_runs(comments, tracking_uri):
    """Only explicit success comments from the configured Forge bot are accepted."""
    runs = {}
    for comment in comments:
        if comment.get('user', {}).get('login') != 'psap-forge-bot[bot]':
            continue
        body = comment.get('body', '')
        if not re.search(r'\b(success|successful|succeeded|passed)\b', body, re.I):
            continue
        if re.search(r'\b(failed|failure|error|unsuccessful)\b', body, re.I):
            continue
        if re.search(r'\b(?:not|never)\s+(?:successful|succeeded|passed)\b', body, re.I):
            continue
        for url in re.findall(r'https://[^\s<>\)\]]+', body):
            parsed = urlparse(url)
            if (parsed.scheme, parsed.netloc) != (urlparse(tracking_uri).scheme, urlparse(tracking_uri).netloc):
                continue
            match = re.search(r'/runs/([a-f0-9]{32})(?:[/?#]|$)', url)
            if match:
                runs.setdefault(match[1], comment['id'])
    return runs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pr-number', type=int)
    parser.add_argument('--before')
    parser.add_argument('--head', default='HEAD')
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--insecure', action='store_true')
    args = parser.parse_args()
    repository = os.environ['GITHUB_REPOSITORY']
    if args.pr_number:
        numbers = {args.pr_number}
    else:
        if not args.before or set(args.before) == {'0'}:
            raise ValueError('initial push needs explicit PR backfill')
        commits = subprocess.run(['git', 'rev-list', f'{args.before}..{args.head}'], check=True,
                                 capture_output=True, text=True).stdout.splitlines()
        numbers = {p['number'] for commit in commits
                   for p in github(f'repos/{repository}/commits/{commit}/pulls', True)
                   if p.get('merged_at') and p['base']['ref'] == 'main'}
    client = Client(os.environ['MLFLOW_TRACKING_URI'], os.environ.get('MLFLOW_WORKSPACE', 'forge-sandbox'),
                    os.environ['MLFLOW_USERNAME'], os.environ['MLFLOW_PASSWORD'], args.insecure)
    for number in sorted(numbers):
        pr = github(f'repos/{repository}/pulls/{number}')
        if not pr.get('merged_at') or pr['base']['ref'] != 'main':
            raise ValueError(f'PR {number} is not merged to main')
        # Evidence PRs must not ingest themselves and create a recursive loop.
        if pr['head']['ref'].startswith('automation/forge-evidence'):
            continue
        comments = github(f'repos/{repository}/issues/{number}/comments', True)
        runs = comment_runs(comments, client.uri)
        if not runs:
            print(f'PR {number}: no trusted successful Forge comment; no import')
            continue
        revision = pr['head']['sha']
        subprocess.run(['git', 'fetch', 'origin', f'refs/pull/{number}/head'], check=True)
        for run_id, comment_id in runs.items():
            import_parent(client, args.repo.resolve(), run_id, revision, number, comment_id)


if __name__ == '__main__':
    main()
