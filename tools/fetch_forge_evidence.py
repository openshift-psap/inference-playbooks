#!/usr/bin/env python3
"""Fetch Forge evidence with explicit workspace routing and fail-closed attribution."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import ssl
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, HTTPSHandler

import yaml

from normalize_kpis import normalize
from forge_model_identity import validate_model_identity
from forge_serving_contract import model_mount_matches, validate_launch
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
                    if 'historical_data' in item['path'].split('/'):
                        continue
                    if item.get('is_dir'):
                        pending.append(item['path'])
                    elif item['path'].endswith('/kpis.json') or item['path'] == 'kpis.json':
                        matches.append(item['path'])
                token = page.get('next_page_token')
                if not token:
                    break
        current = [path for path in matches if re.fullmatch(
            r'01__test/\d+__postprocessing/kpis/kpis.json', path)]
        if current:
            matches = current
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


def serving_arguments(container):
    """Read vLLM argv, including the shell/eval form used by LWS and KServe."""
    command, args = container.get('command', []), container.get('args', [])
    try:
        tokens = []
        for part in [*command, *args]:
            tokens.extend(shlex.split(part))
        # KServe wraps the final invocation in eval "exec vllm serve ...".
        expanded = []
        for token in tokens:
            if re.match(r'(?:exec\s+)?vllm\s+serve\s', token):
                expanded.extend(shlex.split(token))
            else:
                expanded.append(token)
        tokens = expanded
        start = next((i for i in range(len(tokens) - 1)
                      if tokens[i:i + 2] == ['vllm', 'serve']), None)
        forwarded = any(re.fullmatch(r'\s*(?:exec\s+)?vllm\s+serve\s+["\']?\$@["\']?\s*', part)
                        for part in command)
        tokens = args if forwarded else (tokens[start + 2:] if start is not None else args)
        additional = [entry for entry in container.get('env', []) if entry.get('name') == 'VLLM_ADDITIONAL_ARGS']
        if len(additional) > 1 or any('value' not in entry for entry in additional):
            raise ValueError('unresolved serving arguments')
        for entry in additional:
            tokens = [*tokens, *shlex.split(entry['value'])]
    except (TypeError, ValueError):
        raise ValueError('unsupported or unresolved serving arguments') from None
    positional = forwarded or start is not None or not command
    model = tokens[0] if positional and tokens and not tokens[0].startswith('-') else None
    revisions = []
    for i, token in enumerate(tokens):
        if token in ('--revision', '--model'):
            if i + 1 == len(tokens) or tokens[i + 1].startswith('-'):
                raise ValueError('serving model or revision argument is missing')
            value = tokens[i + 1]
        elif token.startswith(('--revision=', '--model=')):
            value = token.partition('=')[2]
        else:
            continue
        if token.startswith('--revision'):
            revisions.append(value)
        elif model is not None and model != value:
            raise ValueError('serving model arguments disagree')
        else:
            model = value
    if len(revisions) > 1:
        raise ValueError('execution model revision is ambiguous')
    return model, revisions[0] if revisions else None


def distributed_serving_values(manifest, pods, checkpoint):
    """Use the producer's scoped pod capture, not sidecar/init images or aliases."""
    kind, spec = manifest['kind'], manifest['spec']
    if kind == 'LeaderWorkerSet':
        templates = spec['leaderWorkerTemplate']
        roles = [templates[key]['spec']['containers'] for key in ('leaderTemplate', 'workerTemplate')]
        names = {'vllm', 'vllm-leader', 'vllm-worker'}
    else:
        roles = [spec['template']['containers']]
        if 'worker' in spec:
            roles.append(spec['worker']['containers'])
        if 'prefill' in spec:
            raise ValueError('LLMInferenceService prefill serving roles are not supported')
        names = {'main'}
    servers = []
    for role in roles:
        selected = [container for container in role if container.get('name') in names]
        if len(selected) != 1:
            raise ValueError('execution serving role container is missing or ambiguous')
        servers.append(selected[0])

    uri = spec.get('model', {}).get('uri', '') if kind == 'LLMInferenceService' else ''
    if kind == 'LLMInferenceService':
        # PVC contents are not inspected or independently attributed. The recipe
        # and ordinary run model labels supply identity (checked by PR54).
        if not uri.startswith('pvc://') and uri != 'hf://' + checkpoint:
            raise ValueError('unsupported LLMInferenceService model URI or checkpoint')

    if pods.get('kind') not in ('List', 'PodList') or not isinstance(pods.get('items'), list):
        raise ValueError('serving pod capture must be a Kubernetes List')
    digests, revisions, observed = set(), set(), set()
    serving_pods = 0
    for pod in pods['items']:
        if pod.get('kind') != 'Pod':
            raise ValueError('serving pod capture contains a non-Pod item')
        statuses = pod.get('status', {}).get('containerStatuses', [])
        matched = False
        for container in pod.get('spec', {}).get('containers', []):
            matches = [i for i, server in enumerate(servers) if container.get('name') == server['name']
                       and (not server.get('image') or container.get('image') == server['image'])]
            if not matches:
                continue
            model, revision = serving_arguments(container)
            # main is also used by some router pods: only an actual vLLM
            # invocation is a serving container for LLMInferenceService.
            if kind == 'LLMInferenceService' and model is None:
                continue
            if kind == 'LLMInferenceService' and not model_mount_matches(pod, container, uri, model):
                raise ValueError('runtime model path disagrees with declared model storage')
            for i in matches:
                declared_model, declared_revision = serving_arguments(servers[i])
                if declared_model is not None and model != declared_model:
                    raise ValueError('runtime model argument disagrees with tested launch')
                if declared_revision is not None and revision != declared_revision:
                    raise ValueError('runtime serving revision disagrees with role configuration')
            status = [s for s in statuses if s.get('name') == container['name'] and s.get('ready') is True]
            if len(status) != 1:
                raise ValueError('ready serving container runtime evidence is missing or ambiguous')
            digest = re.fullmatch(r'(?:\S+@)?(sha256:[a-f0-9]{64})', status[0].get('imageID', ''))
            if not digest:
                raise ValueError('ready serving container image digest is missing')
            digests.add(digest[1])
            # A model-less LWS worker participates in the leader's model load.
            if model is not None or revision is not None:
                revisions.add(revision or 'main')
            observed.update(matches)
            matched = True
        serving_pods += matched
    if observed != set(range(len(servers))) or serving_pods < len(roles):
        raise ValueError('serving role runtime evidence is missing')
    if len(digests) != 1:
        raise ValueError('heterogeneous serving image digests are unsupported by the existing run contract')
    if len(revisions) != 1:
        raise ValueError('serving roles have missing or inconsistent model revisions')
    return revisions.pop(), digests.pop()


def execution_values(client, parent_id, repo, expected_revision, pr_number):
    """Read existing Forge execution metadata into the existing run contract."""
    def artifact(path):
        return client.request('/get-artifact?' + urlencode({'run_id': parent_id, 'path': path}), raw=True).decode()

    def document(path):
        try:
            return load_unique_yaml(artifact(path))
        except (yaml.YAMLError, ValueError):
            raise ValueError('invalid Forge execution YAML') from None

    fjob = document('01__test/000__ci_metadata/fournos_fjob.yaml')
    env = fjob['spec']['env']
    # Recorded requested/tested recipe context, not an independent checkout
    # attestation and not the Forge checkout's PULL_PULL_SHA.
    revision = env.get('FORGE_FOREIGN_TESTING_PULL_PULL_SHA')
    if revision != expected_revision or not re.fullmatch('[a-f0-9]{40}', revision or ''):
        raise ValueError('stale or mismatched tested recipe revision')
    if str(env.get('FORGE_FOREIGN_TESTING_PULL_NUMBER')) != str(pr_number):
        raise ValueError('PR attribution mismatch')
    config = document('01__test/001__inference_playbooks_test_dir/config.yaml')['inference_playbooks']
    manifest_path = config['manifest_path']
    match = re.fullmatch(r'(models/[^/]+/recipes/[^/]+)/manifests/[^/]+/[^/]+\.yaml', manifest_path)
    if not match:
        raise ValueError('execution manifest path must identify a canonical recipe')
    relative = match[1] + '/recipe.yaml'
    path, recipe = tested_inputs(repo, relative, expected_revision)
    if config['recipe'] != recipe['recipe_id']:
        raise ValueError('execution recipe identity mismatch')
    platforms = [p for p in recipe['platforms'] if manifest_path.startswith(
        f'{match[1]}/manifests/{p["stack"]}-{p["version"]}/')]
    if len(platforms) != 1:
        raise ValueError('execution platform is missing or ambiguous')
    platform = platforms[0]
    declared_manifests = recipe.get('deployment', {}).get('manifests', [])
    if declared_manifests and manifest_path not in {
            str(path.parent.relative_to(repo) / item['path']) for item in declared_manifests}:
        raise ValueError('execution manifest is not declared by the tested recipe')
    profile = load_unique_yaml(checked_path(repo, recipe['hardware_profile']).read_text())
    source = '01__test/001__inference_playbooks_test_dir/src'
    listing = client.request('/api/2.0/mlflow/artifacts/list?' + urlencode({'run_id': parent_id, 'path': source}))
    paths = [item['path'] for item in listing.get('files', []) if not item.get('is_dir')
             and re.fullmatch(re.escape(source) + r'/[^/]+\.yaml', item['path'])]
    if platform['stack'] == 'rhoai':
        paths = [p for p in paths if not p.endswith(('-service.yaml', '-deployment.yaml', '-lws.yaml'))]
    else:
        paths = [p for p in paths if p.endswith(('-deployment.yaml', '-lws.yaml'))]
    manifests = [document(p) for p in paths]
    if len(manifests) != 1:
        raise ValueError('execution serving manifest is missing or ambiguous')
    deployment = manifests[0]
    kind = deployment.get('kind')
    if kind not in ('Deployment', 'LeaderWorkerSet', 'LLMInferenceService'):
        raise ValueError('unsupported execution serving manifest')
    if (kind == 'LLMInferenceService') != (platform['stack'] == 'rhoai'):
        raise ValueError('execution serving kind disagrees with selected platform')
    validate_launch(repo, manifest_path, expected_revision, deployment)
    log = artifact('01__test/run.log')
    if kind != 'Deployment':
        capture_name = 'lws' if kind == 'LeaderWorkerSet' else 'llmisvc'
        test_dir = '01__test/001__inference_playbooks_test_dir'
        listing = client.request('/api/2.0/mlflow/artifacts/list?' + urlencode({'run_id': parent_id, 'path': test_dir}))
        captures = [item['path'] for item in listing.get('files', []) if item.get('is_dir') and re.fullmatch(
            re.escape(test_dir) + r'/\d+__capture_' + capture_name + '_state', item['path'])]
        if len(captures) != 1:
            raise ValueError('execution serving state capture is missing or ambiguous')
        basename = 'leaderworkerset' if kind == 'LeaderWorkerSet' else 'llminferenceservice'
        pods = document(captures[0] + '/artifacts/' + basename + '.pods.yaml')
        model_revision, image_digest = distributed_serving_values(deployment, pods, recipe['serving']['model'])
        values = {'recipe_path': relative, 'tested_revision': revision, 'pr_number': str(pr_number),
                  'hardware_profile': recipe['hardware_profile'], 'hardware_profile_revision': str(profile['profile_revision']),
                  'platform_stack': platform['stack'], 'platform_version': platform['version'],
                  'model_revision': model_revision, 'image_digest': image_digest}
    else:
        values = deployment_serving_values(deployment, log)
        values.update({'recipe_path': relative, 'tested_revision': revision, 'pr_number': str(pr_number),
                       'hardware_profile': recipe['hardware_profile'], 'hardware_profile_revision': str(profile['profile_revision']),
                       'platform_stack': platform['stack'], 'platform_version': platform['version']})
    commands = set(re.findall(r'(?m)^\s*Command: (\S[^\r\n]*)$', log))
    if len(commands) != 1:
        raise ValueError('recorded common Forge invocation is missing or ambiguous')
    command = commands.pop()
    # This is the recorded invocation, not a command reconstructed from fjob args.
    argv = shlex.split(command)
    if not any(arg.endswith('/inference_playbooks/orchestration/ci.py') for arg in argv) or argv[-1] != 'test':
        raise ValueError('recorded command is not the Forge test invocation')
    values['command'] = command
    safe_artifact(values)
    return {'playbooks.' + key: value for key, value in values.items()}


def deployment_serving_values(deployment, log):
    """Preserve the existing Deployment capture format embedded in run.log."""
    containers = deployment['spec']['template']['spec']['containers']
    servers = [container for container in containers if container.get('name') == 'vllm']
    if len(servers) != 1:
        raise ValueError('execution serving container is missing or ambiguous')
    server = servers[0]
    model, revision = serving_arguments(server)
    if model is None:
        raise ValueError('execution model argument is missing or unsupported')
    plain = re.sub(r'(?m)^\| ?', '', log)
    digests = set()
    for block in re.split(r'(?m)^\s*containerID:', plain)[1:]:
        image = re.search(r'(?m)^\s*image: (\S+)\s*$', block)
        image_id = re.search(r'(?m)^\s*imageID: \S*@(sha256:[a-f0-9]{64})\s*$', block)
        name = re.search(r'(?m)^\s*name: (\S+)\s*$', block)
        ready = re.search(r'(?m)^\s*ready: (true|false)\s*$', block)
        if image and image_id and name and ready and ready[1] == 'true' and image[1] == server['image'] and name[1] == server['name']:
            digests.add(image_id[1])
    if len(digests) != 1:
        raise ValueError('ready serving container image digest is missing or ambiguous')
    return {'model_revision': revision or 'main', 'image_digest': digests.pop()}


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
    model_relative = str(path.parent.parent.parent.relative_to(repo) / 'model.yaml')
    inputs = [recipe['hardware_profile'], model_relative]
    inputs.extend(str((path.parent / manifest['path']).relative_to(repo))
                  for manifest in recipe.get('deployment', {}).get('manifests', []))
    inputs.extend(str((path.parent / p['overrides']).relative_to(repo)) for p in recipe['platforms'])
    inputs.extend(str((path.parent / p['pinned_manifest']).relative_to(repo))
                  for p in recipe['platforms'] if p.get('pinned_manifest'))
    inputs.extend(str(checked_path(repo, str(path.parent.relative_to(repo) / p['config'])).relative_to(repo))
                  for p in recipe['platforms'] if p.get('config'))
    if require_clean is not None:
        for input_path in [*inputs, str(path.parent.relative_to(repo) / 'config')]:
            require_clean(input_path)
    diff = subprocess.run(['git', '-C', str(repo), 'diff', '--name-only', expected_revision, '--', *inputs,
                           str(path.parent.relative_to(repo) / 'config')], check=True, capture_output=True, text=True)
    if diff.stdout:
        if diff.stdout.splitlines() != [model_relative]:
            raise ValueError('merged deployment inputs differ from tested revision')
        summary = subprocess.run(['git', '-C', str(repo), 'diff', '--summary', expected_revision,
                                  '--', model_relative], check=True, capture_output=True, text=True)
        if summary.stdout:
            raise ValueError('merged deployment inputs differ from tested revision')
        current_model = load_unique_yaml(checked_path(repo, model_relative).read_text())
        tested_model = load_unique_yaml(subprocess.run(
            ['git', '-C', str(repo), 'show', f'{expected_revision}:{model_relative}'],
            check=True, capture_output=True, text=True).stdout)
        # Retired catalog-only metadata may occur in tested historical snapshots.
        # All remaining model fields, including unknown future fields, must agree.
        for model in (tested_model, current_model):
            model.pop('presentation', None)
        if tested_model != current_model:
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
    parent_artifact = client.kpis(parent_id)
    current_parent = parent_artifact is not None and re.fullmatch(
        r'01__test/\d+__postprocessing/kpis/kpis.json', parent_artifact[0])
    candidates = [parent] if current_parent else [parent, *client.children(parent)]
    if require_parent and any(run['info']['status'] not in {'FINISHED', 'FAILED', 'KILLED'}
                              for run in candidates):
        raise ValueError('selected execution has nonterminal direct children')
    prepared = []
    artifact_count = 0
    for child in candidates:
        if child['info']['status'] != 'FINISHED':
            continue
        child_id = child['info']['run_id']
        artifact = parent_artifact if child_id == parent_id else client.kpis(child_id)
        if artifact is None:
            continue
        artifact_count += 1
        values = {**tags(parent), **tags(child)}
        manifest_bound = False
        required = ('recipe_path', 'tested_revision', 'hardware_profile', 'hardware_profile_revision',
                    'model_revision', 'image_digest', 'platform_stack', 'platform_version', 'pr_number', 'command')
        if current_parent and any(not values.get('playbooks.' + key) for key in required):
            recorded = execution_values(client, parent_id, repo, expected_revision, pr_number)
            if any(key in values and values[key] != value for key, value in recorded.items()):
                raise ValueError('Forge attribution tags disagree with recorded execution')
            values = {**values, **recorded}
            manifest_bound = True
        path, recipe, profile, platform = attribution(repo, values, expected_revision, pr_number)
        artifact_path, raw = artifact
        document = json.loads(raw)
        safe_artifact(document)
        model = load_unique_yaml((path.parent.parent.parent / 'model.yaml').read_text())
        validate_model_identity(document, recipe, model)
        for test in document['tests']:
            labels = test.get('labels', {})
            # Forge 768a2858 takes product_version from platforms[0], not the
            # selected manifest. Once launch-bound, it is descriptive raw metadata;
            # platform attribution comes from that exact tested manifest instead.
            if not manifest_bound and platform['stack'] == 'vllm' and labels.get('product_version') and labels['product_version'].lstrip('v') != platform['version'].lstrip('v'):
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
