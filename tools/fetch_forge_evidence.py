#!/usr/bin/env python3
"""Fetch Forge evidence with explicit workspace routing and fail-closed attribution."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, HTTPSHandler

import yaml

from normalize_kpis import normalize
from forge_model_identity import validate_model_identity
from recipe_evidence import load_unique_yaml


class NoKpiArtifactsError(ValueError):
    """Successful discovery found no KPI artifacts on eligible runs."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('redirect rejected: credentials must not leave configured origin')


class Client:
    def __init__(self, uri, workspace, username, password, insecure=False):
        if urlparse(uri).scheme != 'https' or urlparse(uri).username:
            raise ValueError('tracking URI must use HTTPS without embedded credentials')
        self.uri = uri.rstrip('/')
        self.headers = {'X-MLflow-Workspace': workspace,
                        'Authorization': 'Basic ' + base64.b64encode(f'{username}:{password}'.encode()).decode()}
        context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=context))

    def request(self, path, body=None, raw=False):
        request = Request(self.uri + path, data=None if body is None else json.dumps(body).encode(),
                          headers={**self.headers, 'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=60) as response:
            data = response.read(10 * 1024 * 1024 + 1)
        if len(data) > 10 * 1024 * 1024:
            raise ValueError('artifact exceeds 10 MiB intake limit')
        return data if raw else json.loads(data)

    def run(self, run_id):
        if not re.fullmatch('[a-f0-9]{32}', run_id):
            raise ValueError('invalid MLflow run ID')
        return self.request('/api/2.0/mlflow/runs/get?' + urlencode({'run_id': run_id}))['run']

    def children(self, parent):
        result, token = [], None
        while True:
            body = {'experiment_ids': [str(parent['info']['experiment_id'])],
                    'filter': f"tags.`mlflow.parentRunId` = '{parent['info']['run_id']}'", 'max_results': 100}
            if token:
                body['page_token'] = token
            page = self.request('/api/2.0/mlflow/runs/search', body)
            result.extend(page.get('runs', []))
            token = page.get('next_page_token')
            if not token:
                return result

    def kpis(self, run_id):
        # Find the artifact rather than trusting a fixed postprocessing ordinal.
        pending, matches = [''], []
        visited = set()
        while pending:
            path = pending.pop()
            if path in visited or len(visited) > 1000:
                raise ValueError('cyclic or oversized artifact listing')
            visited.add(path)
            token = None
            while True:
                query = {'run_id': run_id, 'path': path}
                if token:
                    query['page_token'] = token
                page = self.request('/api/2.0/mlflow/artifacts/list?' + urlencode(query))
                for item in page.get('files', []):
                    if item.get('is_dir'):
                        pending.append(item['path'])
                    elif item['path'].endswith('/kpis.json') or item['path'] == 'kpis.json':
                        matches.append(item['path'])
                token = page.get('next_page_token')
                if not token:
                    break
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError('ambiguous kpis.json artifacts')
        path = matches[0]
        return path, self.request('/get-artifact?' + urlencode({'run_id': run_id, 'path': path}), raw=True)


def tags(run):
    return {item['key']: item['value'] for item in run.get('data', {}).get('tags', [])}


def checked_path(repo, relative):
    path = (repo / relative).resolve()
    if not path.is_relative_to(repo.resolve()):
        raise ValueError('path escapes repository')
    return path


def safe_artifact(value):
    """Reject obvious private data; never rewrite authoritative raw evidence."""
    if isinstance(value, dict):
        for key, item in value.items():
            if re.search(r'(?i)(password|secret|authorization|api[_-]?key|access[_-]?token|hostname|ip_address|^prompts?$)', key):
                raise ValueError('raw KPI artifact contains a sensitive field; sanitize at source')
            if key == 'cluster' and isinstance(item, str) and '.' in item:
                raise ValueError('cluster must be a public alias, not a hostname')
            safe_artifact(item)
    elif isinstance(value, list):
        for item in value:
            safe_artifact(item)
    elif isinstance(value, str):
        if re.search(r'(?i)(bearer\s+|--api-key|password=|secret=|https?://[^/\s]+@|\b(?:\d{1,3}\.){3}\d{1,3}\b)', value):
            raise ValueError('raw KPI artifact may contain credentials or an IP; sanitize at source')


def attribution(repo, values, expected_revision, pr_number):
    required = ('recipe_path', 'tested_revision', 'hardware_profile', 'hardware_profile_revision',
                'model_revision', 'image_digest', 'platform_stack', 'platform_version', 'pr_number', 'command')
    missing = [key for key in required if not values.get(f'playbooks.{key}')]
    if missing:
        raise ValueError('missing Forge attribution tags: ' + ', '.join(missing))
    get = lambda key: values[f'playbooks.{key}']
    if not re.fullmatch('[a-f0-9]{40}', get('tested_revision')) or get('tested_revision') != expected_revision:
        raise ValueError('stale or mismatched tested revision')
    if int(get('pr_number')) != pr_number:
        raise ValueError('PR attribution mismatch')
    relative = get('recipe_path')
    if not re.fullmatch(r'models/[^/]+/recipes/[^/]+/recipe.yaml', relative):
        raise ValueError('recipe_path must be canonical')
    path, recipe = tested_inputs(repo, relative, expected_revision)
    profile = load_unique_yaml(checked_path(repo, recipe['hardware_profile']).read_text())
    if get('hardware_profile') != recipe['hardware_profile'] or int(get('hardware_profile_revision')) != profile['profile_revision']:
        raise ValueError('hardware profile attribution mismatch')
    platform = {'stack': get('platform_stack'), 'version': get('platform_version')}
    if not any(all(p[k] == v for k, v in platform.items()) for p in recipe['platforms']):
        raise ValueError('platform attribution mismatch')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', get('image_digest')):
        raise ValueError('image digest required')
    if not re.fullmatch(r'[^\s\x00-\x1f\x7f]+', get('model_revision')):
        raise ValueError('nonempty model revision identifier without whitespace or control characters required')
    safe_artifact({'model_revision': get('model_revision')})
    safe_artifact({'command': get('command')})
    return path, recipe, profile, platform


def tested_inputs(repo, relative, expected_revision, require_clean=None):
    """Compare deployment inputs with the tested snapshot, also for local admission hits."""
    path = checked_path(repo, relative)
    if require_clean is not None:
        require_clean(relative)
    recipe = load_unique_yaml(path.read_text())
    # Compare all deployment inputs with the exact tested Git snapshot.
    tested = load_unique_yaml(subprocess.run(['git', '-C', str(repo), 'show', f'{expected_revision}:{relative}'],
                              check=True, capture_output=True, text=True).stdout)
    current = dict(recipe)
    tested_references = tested.get('benchmark_runs', [])
    current_references = current.get('benchmark_runs', [])
    if any(reference not in current_references for reference in tested_references):
        raise ValueError('merged recipe removed tested benchmark references')
    for data in (tested, current):
        data.pop('benchmark_runs', None)
    if tested != current:
        raise ValueError('merged recipe differs from tested revision')
    inputs = [recipe['hardware_profile'], str(path.parent.parent.parent.relative_to(repo) / 'model.yaml')]
    inputs.extend(str((path.parent / p['overrides']).relative_to(repo)) for p in recipe['platforms'])
    inputs.extend(str((path.parent / p['pinned_manifest']).relative_to(repo))
                  for p in recipe['platforms'] if p.get('pinned_manifest'))
    inputs.extend(str(checked_path(repo, str(path.parent.relative_to(repo) / p['config'])).relative_to(repo))
                  for p in recipe['platforms'] if p.get('config'))
    if require_clean is not None:
        for input_path in [*inputs, str(path.parent.relative_to(repo) / 'config')]:
            require_clean(input_path)
    diff = subprocess.run(['git', '-C', str(repo), 'diff', expected_revision, '--', *inputs,
                           str(path.parent.relative_to(repo) / 'config')], check=True, capture_output=True, text=True)
    if diff.stdout:
        raise ValueError('merged deployment inputs differ from tested revision')
    untracked = subprocess.run(['git', '-C', str(repo), 'ls-files', '--others', '--', *inputs,
                                str(path.parent.relative_to(repo) / 'config')],
                               check=True, capture_output=True, text=True)
    if untracked.stdout:
        raise ValueError('untracked deployment inputs cannot be attributed')
    return path, recipe


def append_reference(text, reference):
    """Update only the top-level evidence index and verify YAML semantics."""
    recipe = load_unique_yaml(text)
    if reference in recipe.get('benchmark_runs', []):
        return text
    expected = {**recipe, 'benchmark_runs': [*recipe.get('benchmark_runs', []), reference]}
    block = yaml.safe_dump({'benchmark_runs': expected['benchmark_runs']}, sort_keys=False)
    if 'benchmark_runs' in recipe:
        pattern = r'(?m)^benchmark_runs:[^\n]*(?:\n(?:[ \t]+[^\n]*|-[ \t]+[^\n]*|[ \t]*))*(?:\n|$)'
        match = re.search(pattern, text)
        if not match:
            raise ValueError('cannot safely update benchmark_runs')
        updated = text[:match.start()] + block + text[match.end():]
    else:
        updated = text.rstrip() + '\n' + block
    if load_unique_yaml(updated) != expected:
        raise ValueError('evidence reference update changed unrelated recipe fields')
    return updated


def import_parent(client, repo, parent_id, expected_revision, pr_number, comment_id=None,
                  *, require_parent=False, admissions=None):
    parent = client.run(parent_id)
    actual_parent = tags(parent).get('mlflow.parentRunId')
    if actual_parent:
        if require_parent:
            raise ValueError('selected primary result link is not a parent run')
        parent_id = actual_parent
        parent = client.run(parent_id)
    if parent['info']['status'] != 'FINISHED':
        raise ValueError('parent run is not successful')
    if require_parent:
        if parent['info']['run_id'] != parent_id:
            raise ValueError('selected execution identity mismatch')
        if tags(parent).get('playbooks.tested_revision', expected_revision) != expected_revision:
            raise ValueError('selected execution tested revision mismatch')
    candidates = [parent, *client.children(parent)]
    if require_parent and any(run['info']['status'] not in {'FINISHED', 'FAILED', 'KILLED'}
                              for run in candidates):
        raise ValueError('selected execution has nonterminal direct children')
    prepared = []
    artifact_count = 0
    for child in candidates:
        if child['info']['status'] != 'FINISHED':
            continue
        child_id = child['info']['run_id']
        artifact = client.kpis(child_id)
        if artifact is None:
            continue
        artifact_count += 1
        values = {**tags(parent), **tags(child)}
        path, recipe, profile, platform = attribution(repo, values, expected_revision, pr_number)
        artifact_path, raw = artifact
        document = json.loads(raw)
        safe_artifact(document)
        model = load_unique_yaml((path.parent.parent.parent / 'model.yaml').read_text())
        validate_model_identity(document, recipe, model)
        for test in document['tests']:
            labels = test.get('labels', {})
            if platform['stack'] == 'vllm' and labels.get('product_version') and labels['product_version'].lstrip('v') != platform['version'].lstrip('v'):
                raise ValueError('KPI platform version disagrees with run attribution')
        results = normalize(document, run_id=f'forge-{child_id}', deployment_scope=recipe['deployment']['scope'],
                            accelerator_key=profile['accelerator_key'],
                            expected_concurrencies=json.loads(values['playbooks.expected_concurrencies'])
                            if values.get('playbooks.expected_concurrencies') else None)
        for harness, result in results.items():
            url = f'{client.uri}/#/experiments/{child["info"]["experiment_id"]}/runs/{child_id}?{urlencode({"workspace": client.headers["X-MLflow-Workspace"]})}'
            checksum = 'sha256:' + hashlib.sha256(raw).hexdigest()
            forge = {'mlflow_tracking_uri': client.uri, 'mlflow_workspace': client.headers['X-MLflow-Workspace'],
                     'mlflow_experiment_id': str(child['info']['experiment_id']), 'mlflow_parent_run_id': parent_id,
                     'mlflow_benchmark_run_id': child_id, 'mlflow_run_url': url, 'pr_number': pr_number,
                     'tested_revision': expected_revision, 'kpis_artifact_path': artifact_path,
                     'kpis_checksum': checksum, 'completeness': result['completeness']}
            if comment_id is not None:
                forge['pr_comment_id'] = comment_id
            if values.get('playbooks.expected_concurrencies'):
                forge['expected_concurrencies'] = json.loads(values['playbooks.expected_concurrencies'])
            if values.get('playbooks.prow_job_id'):
                forge['prow_job_id'] = values['playbooks.prow_job_id']
            if values.get('playbooks.prow_job_url'):
                forge['prow_job_url'] = values['playbooks.prow_job_url']
            run = {'schema_version': 2, 'evidence_source': 'forge-kpis', 'run_id': result['run_id'],
                   'recipe_id': recipe['recipe_id'], 'deployment_scope': recipe['deployment']['scope'],
                   'hardware_profile': recipe['hardware_profile'], 'hardware_profile_revision': profile['profile_revision'],
                   'platform': platform, 'harness': harness, 'result': 'result.json', 'forge': forge,
                   'command': values['playbooks.command'],
                   'environment': {'model_revision': values['playbooks.model_revision'],
                                   'image_digest': values['playbooks.image_digest'], 'platform': platform},
                   'workload': {'tests': [t.get('labels', {}) for t in document['tests']
                                         if t.get('labels', {}).get('benchmark_tool') == harness]},
                   'artifacts': [{'type': 'raw-output', 'path': 'kpis.json', 'checksum': checksum}]}
            directory = checked_path(repo, str(path.parent.relative_to(repo) / 'results' / result['run_id']))
            files = {'result.json': json.dumps(result, indent=2, allow_nan=False) + '\n',
                     'run.yaml': yaml.safe_dump(run, sort_keys=False), 'kpis.json': raw.decode()}
            if require_parent and (directory / 'run.yaml').is_file():
                # A newer duplicate report must not change immutable comment provenance.
                existing_text = (directory / 'run.yaml').read_text()
                existing = load_unique_yaml(existing_text)
                proposed = load_unique_yaml(files['run.yaml'])
                existing['forge'].pop('pr_comment_id', None)
                proposed['forge'].pop('pr_comment_id', None)
                if existing == proposed:
                    files['run.yaml'] = existing_text
            if directory.exists() and any(not (directory / name).is_file() or (directory / name).read_text() != text
                                          for name, text in files.items()):
                raise ValueError('immutable run already exists with different evidence')
            prepared.append((path, directory, files))
    if artifact_count == 0:
        raise NoKpiArtifactsError(f'parent {parent_id}: no KPI artifacts on FINISHED parent/direct children')
    if not prepared:
        raise ValueError('no successful supported KPI evidence found')
    # Validate every candidate before mutating any recipe.
    import jsonschema
    for _, _, files in prepared:
        jsonschema.validate(yaml.safe_load(files['run.yaml']),
                            json.loads((repo / 'schema/benchmark-run.schema.json').read_text()))
        jsonschema.validate(json.loads(files['result.json']),
                            json.loads((repo / 'schema/benchmark-result.schema.json').read_text()))
    updated_recipes = {}
    for path, directory, _ in prepared:
        text = updated_recipes.get(path, path.read_text())
        reference = str((directory / 'run.yaml').relative_to(path.parent))
        updated_recipes[path] = append_reference(text, reference)
    if require_parent:
        from forge_admission import admission_key, prepare_admission
        receipt_path, receipt_text, admission = prepare_admission(repo, prepared)
        key = admission_key(admission['receipt'])
        previous = getattr(admissions, 'positive_outputs', {}).get(key, set())
        if previous - set(admission['records']):
            raise ValueError('selected discovery omitted previously committed positive outputs')
        if receipt_path.exists() and receipt_path.read_text() != receipt_text:
            raise ValueError('immutable Forge admission already exists with different coverage')
    for path, directory, files in prepared:
        directory.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            if not (directory / name).exists():
                (directory / name).write_text(text)
    for path, text in updated_recipes.items():
        if path.read_text() != text:
            path.write_text(text)
    if require_parent:
        if not receipt_path.exists():
            receipt_path.write_text(receipt_text)
        if admissions is not None:
            admissions[key] = admission
            if hasattr(admissions, 'positive_outputs'):
                admissions.positive_outputs[key] = set(admission['records'])
                for run in admission['records'].values():
                    forge = run['forge']
                    for run_id in {forge['mlflow_parent_run_id'], forge['mlflow_benchmark_run_id']}:
                        admissions.known_sources[key[:2] + (run_id,)] = pr_number, expected_revision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--pr-number', type=int, required=True)
    parser.add_argument('--comment-id', type=int)
    parser.add_argument('--tested-revision', required=True)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--tracking-uri', default=os.environ.get('MLFLOW_TRACKING_URI'))
    parser.add_argument('--workspace', default=os.environ.get('MLFLOW_WORKSPACE', 'forge-sandbox'))
    parser.add_argument('--insecure', action='store_true', help='explicit opt-in for self-signed TLS')
    args = parser.parse_args()
    try:
        client = Client(args.tracking_uri, args.workspace, os.environ['MLFLOW_USERNAME'],
                        os.environ['MLFLOW_PASSWORD'], args.insecure)
        import_parent(client, args.repo.resolve(), args.run_id, args.tested_revision, args.pr_number, args.comment_id)
    except Exception as error:
        print(f'Forge intake rejected: {type(error).__name__}: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
