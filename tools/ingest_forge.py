#!/usr/bin/env python3
"""Discover merged PRs and trusted Forge comments, then import their evidence."""
import argparse
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fetch_forge_evidence import Client, import_parent
from forge_admission import admitted_execution, load_admissions, origin


def github(endpoint, paginated=False):
    command = ['gh', 'api', endpoint]
    if paginated:
        command.extend(['--paginate', '--slurp'])
    value = json.loads(subprocess.run(command, check=True, capture_output=True, text=True).stdout)
    return [item for page in value for item in page] if paginated else value


def selected_run(comments, tracking_uri, workspace):
    """Select the latest trusted success report before interpreting its primary link."""
    reports = []
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
        reports.append(comment)
    if not reports:
        return None
    report = max(reports, key=lambda c: (datetime.fromisoformat(c['created_at'].replace('Z', '+00:00')), c['id']))
    links = re.findall(r'\[(?:mlflow results|mlflow test results)\]\(([^)\n]*)\)', report['body'], re.I)
    if not links:
        raise ValueError('latest successful Forge report has no primary result link')
    runs = set()
    for url in links:
        parsed = urlparse(url)
        match = re.fullmatch(r'/experiments/[0-9]+/runs/([a-f0-9]{32})(?:/artifacts)?/?(?:\?[^#]*)?',
                             parsed.fragment)
        if (origin(url) != origin(tracking_uri) or parsed.query or
                parsed.path.rstrip('/') != urlparse(tracking_uri).path.rstrip('/') or not match):
            raise ValueError('latest successful Forge report has an invalid primary result link')
        query = parse_qs(parsed.fragment.partition('?')[2], keep_blank_values=True)
        if 'workspace' in query and query['workspace'] != [workspace]:
            raise ValueError('selected Forge report workspace disagrees with configured workspace')
        runs.add(match[1])
    if len(runs) != 1:
        raise ValueError('latest successful Forge report has ambiguous primary result links')
    return runs.pop(), report['id']


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
    admissions, checked_inputs = None, {}
    for number in sorted(numbers):
        pr = github(f'repos/{repository}/pulls/{number}')
        if not pr.get('merged_at') or pr['base']['ref'] != 'main':
            raise ValueError(f'PR {number} is not merged to main')
        # Evidence PRs must not ingest themselves and create a recursive loop.
        if pr['head']['ref'].startswith('automation/forge-evidence'):
            continue
        comments = github(f'repos/{repository}/issues/{number}/comments', True)
        selected = selected_run(comments, client.uri, client.headers['X-MLflow-Workspace'])
        if selected is None:
            print(f'PR {number}: no trusted successful Forge comment; no import')
            continue
        revision = pr['head']['sha']
        subprocess.run(['git', 'fetch', 'origin', f'refs/pull/{number}/head'], check=True)
        repo = args.repo.resolve()
        if admissions is None:
            admissions = load_admissions(repo)
        run_id, comment_id = selected
        if admitted_execution(repo, admissions, client, run_id, revision, number, checked_inputs):
            print(f'PR {number}: selected execution already indexed; imported=0 indexed=1')
            continue
        import_parent(client, repo, run_id, revision, number, comment_id,
                      require_parent=True, admissions=admissions)
        print(f'PR {number}: selected execution imported; imported=1 indexed=0')


if __name__ == '__main__':
    main()
