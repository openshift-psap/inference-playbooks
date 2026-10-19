"""Positive Forge execution receipts; committed metadata only on repeat ingestion."""
import hashlib
import json
import re
import subprocess
from urllib.parse import urlparse

from jsonschema import Draft202012Validator

from recipe_evidence import load_unique_yaml


RECEIPT = 'forge-import.json'
FILES = {'run.yaml', 'result.json', 'kpis.json'}
SOURCE = {'tracking_origin', 'workspace', 'parent_run_id', 'pr_number', 'tested_revision'}


class Admissions(dict):
    def __init__(self):
        super().__init__()
        self.known_sources = {}
        self.positive_outputs = {}


def origin(uri):
    parsed = urlparse(uri)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Forge origin must be HTTPS without credentials')
    return f'https://{parsed.netloc.lower()}'


def admission_key(receipt):
    return receipt['tracking_origin'], receipt['workspace'], receipt['parent_run_id']


def source(run):
    forge = run['forge']
    return {'tracking_origin': origin(forge['mlflow_tracking_uri']),
            'workspace': forge['mlflow_workspace'], 'parent_run_id': forge['mlflow_parent_run_id'],
            'pr_number': forge['pr_number'], 'tested_revision': forge['tested_revision']}


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def prepare_admission(repo, prepared):
    """Called only after the complete positive output set passed import validation."""
    algorithm = git(repo, 'rev-parse', '--show-object-format').strip()
    if algorithm not in {'sha1', 'sha256'}:
        raise ValueError('unsupported Git object format')
    outputs, records = [], {}
    common = None
    for recipe_path, directory, files in sorted(prepared, key=lambda item: str(item[1])):
        run = load_unique_yaml(files['run.yaml'])
        current = source(run)
        if common is not None and current != common:
            raise ValueError('Forge admission outputs disagree on execution attribution')
        common = current
        run_path = str((directory / 'run.yaml').relative_to(repo))
        blobs = {}
        for name, text in files.items():
            raw = text.encode()
            blobs[name] = hashlib.new(algorithm, f'blob {len(raw)}\0'.encode() + raw).hexdigest()
        outputs.append({'recipe_path': str(recipe_path.relative_to(repo)), 'run_path': run_path,
                        'benchmark_run_id': run['forge']['mlflow_benchmark_run_id'],
                        'harness': run['harness'], 'files': blobs})
        if run_path in records:
            raise ValueError('duplicate Forge admission output')
        records[run_path] = run
    receipt = {'version': 1, **common, 'outputs': outputs}
    anchor = repo / outputs[0]['run_path']
    return anchor.parent / RECEIPT, json.dumps(receipt, indent=2, sort_keys=True) + '\n', {
        'receipt': receipt, 'records': records}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate Forge receipt key')
        result[key] = value
    return result


def load_admissions(repo):
    """Scan HEAD and status once; never read raw artifacts or normalized results."""
    from fetch_forge_evidence import checked_path
    tree = {}
    for entry in git(repo, 'ls-tree', '-r', '-z', 'HEAD').split('\0'):
        if entry:
            metadata, path = entry.split('\t', 1)
            mode, kind, oid = metadata.split()
            tree[path] = (mode, kind, oid)
    dirty = set()
    entries = iter(git(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all').split('\0'))
    for entry in entries:
        if entry:
            dirty.add(entry[3:])
            if 'R' in entry[:2] or 'C' in entry[:2]:
                dirty.add(next(entries))

    def committed(relative):
        # Preserve the lexical Git path: resolving first would trust an alias's target.
        if any(part in {'', '.', '..'} for part in relative.split('/')):
            raise ValueError('Forge admission source path must be canonical and relative')
        if tree.get(relative, ())[:2] != ('100644', 'blob'):
            raise ValueError(f'Forge admission source is not a committed regular file: {relative}')
        if relative in dirty:
            raise ValueError(f'dirty Forge admission source: {relative}')
        component = repo
        for part in relative.split('/'):
            component = component / part
            if component.is_symlink():
                raise ValueError(f'Forge admission source traverses a symlink: {relative}')
        path = checked_path(repo, relative)
        if str(path.relative_to(repo)) != relative:
            raise ValueError('Forge admission source path is not canonical')
        return path

    def clean_input(relative):
        path = checked_path(repo, relative)
        if any(name == relative or name.startswith(relative + '/') for name in dirty):
            raise ValueError(f'dirty Forge deployment input: {relative}')
        if path.is_file() or path.is_symlink():
            committed(relative)

    validator = Draft202012Validator(json.loads((repo / 'schema/benchmark-run.schema.json').read_text()))
    records, owners, receipts, identities, run_ids = {}, {}, set(), {}, set()
    admissions = Admissions()
    groups = admissions.positive_outputs
    for relative in sorted(tree):
        if not re.fullmatch(r'models/[^/]+/recipes/[^/]+/recipe.yaml', relative):
            continue
        recipe_path = committed(relative)
        recipe = load_unique_yaml(recipe_path.read_text())
        for reference in recipe.get('benchmark_runs', []):
            if not isinstance(reference, str):
                raise ValueError('Forge benchmark reference must be a canonical relative path')
            run_path = f'{recipe_path.parent.relative_to(repo)}/{reference}'
            path = committed(run_path)
            if not path.is_relative_to(recipe_path.parent):
                raise ValueError('Forge benchmark reference escapes recipe')
            run = load_unique_yaml(path.read_text())
            if run.get('evidence_source') != 'forge-kpis':
                continue
            validator.validate(run)
            if run_path in records or run['run_id'] in run_ids:
                raise ValueError('duplicate referenced Forge run')
            run_ids.add(run['run_id'])
            identity = (origin(run['forge']['mlflow_tracking_uri']), run['forge']['mlflow_workspace'],
                        run['forge']['mlflow_benchmark_run_id'])
            attribution = (source(run), relative, run['platform'], run['environment'],
                           run['hardware_profile'], run['hardware_profile_revision'], run['command'],
                           run['forge']['kpis_checksum'])
            if identity in identities and identities[identity] != attribution:
                raise ValueError('conflicting Forge benchmark attribution')
            identities[identity] = attribution
            if (run['recipe_id'] != recipe['recipe_id'] or
                    run['deployment_scope'] != recipe['deployment']['scope']):
                raise ValueError('Forge run disagrees with recipe ownership')
            records[run_path], owners[run_path] = run, relative
            group_key = admission_key(source(run))
            groups.setdefault(group_key, set()).add(run_path)
            for run_id in {run['forge']['mlflow_parent_run_id'], run['forge']['mlflow_benchmark_run_id']}:
                key = identity[:2] + (run_id,)
                owner = run['forge']['pr_number'], run['forge']['tested_revision']
                if key in admissions.known_sources and admissions.known_sources[key] != owner:
                    raise ValueError('conflicting Forge execution PR/revision ownership')
                admissions.known_sources[key] = owner
            receipt_path = str(path.parent.relative_to(repo) / RECEIPT)
            if receipt_path in tree:
                receipts.add(receipt_path)

    for relative in sorted(receipts):
        receipt = json.loads(committed(relative).read_text(), object_pairs_hook=unique_object)
        if (not isinstance(receipt, dict) or set(receipt) != SOURCE | {'version', 'outputs'} or
                type(receipt['version']) is not int or receipt['version'] != 1 or
                not isinstance(receipt['outputs'], list) or not receipt['outputs'] or
                type(receipt['pr_number']) is not int or receipt['pr_number'] < 1 or
                not isinstance(receipt['workspace'], str) or not receipt['workspace'] or
                not isinstance(receipt['parent_run_id'], str) or
                not re.fullmatch('[a-f0-9]{32}', receipt['parent_run_id']) or
                not isinstance(receipt['tested_revision'], str) or
                not re.fullmatch('[a-f0-9]{40}', receipt['tested_revision']) or
                not isinstance(receipt['tracking_origin'], str) or
                origin(receipt['tracking_origin']) != receipt['tracking_origin']):
            raise ValueError('invalid Forge admission receipt')
        covered, output_ids = {}, set()
        for output in receipt['outputs']:
            if (not isinstance(output, dict) or set(output) != {
                    'recipe_path', 'run_path', 'benchmark_run_id', 'harness', 'files'}):
                raise ValueError('invalid Forge admission output')
            run_path = output['run_path']
            if not isinstance(run_path, str) or run_path not in records or run_path in covered:
                raise ValueError('Forge admission output is missing, duplicate, or unreferenced')
            run = records[run_path]
            if ({key: receipt[key] for key in SOURCE} != source(run) or
                    output['recipe_path'] != owners[run_path] or
                    output['benchmark_run_id'] != run['forge']['mlflow_benchmark_run_id'] or
                    output['harness'] != run['harness'] or
                    run['run_id'] != f"forge-{output['benchmark_run_id']}-{output['harness']}"):
                raise ValueError('Forge admission output attribution mismatch')
            output_id = (output['benchmark_run_id'], output['harness'])
            if output_id in output_ids:
                raise ValueError('duplicate Forge admission harness')
            output_ids.add(output_id)
            if not isinstance(output['files'], dict) or set(output['files']) != FILES:
                raise ValueError('incomplete Forge admission output files')
            for name, oid in output['files'].items():
                file_path = str((repo / run_path).parent.relative_to(repo) / name)
                committed(file_path)
                if oid != tree[file_path][2]:
                    raise ValueError('Forge admission blob identity mismatch')
            covered[run_path] = run
        anchor = min(covered)
        if relative != str((repo / anchor).parent.relative_to(repo) / RECEIPT):
            raise ValueError('Forge admission anchor is not the first referenced output')
        key = admission_key(receipt)
        if set(covered) != groups[key]:
            raise ValueError('Forge admission does not bind the complete referenced positive output set')
        if key in admissions:
            raise ValueError('conflicting Forge execution receipts')
        admissions[key] = {'receipt': receipt, 'records': covered, 'require_clean': clean_input}
    return admissions


def admitted_execution(repo, admissions, client, parent_id, revision, pr_number, checked_inputs):
    """A receipt hit still requires current local deployment attribution, never KPI replay."""
    from fetch_forge_evidence import checked_path, tested_inputs
    key = origin(client.uri), client.headers['X-MLflow-Workspace'], parent_id
    known = getattr(admissions, 'known_sources', {}).get(key)
    if known is not None and known != (pr_number, revision):
        raise ValueError('indexed Forge execution PR/revision attribution mismatch')
    admission = admissions.get(key)
    if admission is None:
        return False
    receipt = admission['receipt']
    if receipt['pr_number'] != pr_number or receipt['tested_revision'] != revision:
        raise ValueError('indexed Forge execution PR/revision attribution mismatch')
    for output in receipt['outputs']:
        run = admission['records'][output['run_path']]
        if run['forge']['mlflow_tracking_uri'].rstrip('/') != client.uri.rstrip('/'):
            raise ValueError('indexed Forge tracking URI mismatch')
        key = output['recipe_path'], revision
        if key not in checked_inputs:
            _, recipe = tested_inputs(repo, key[0], revision, admission.get('require_clean'))
            profile = load_unique_yaml(checked_path(repo, recipe['hardware_profile']).read_text())
            checked_inputs[key] = recipe, profile
        recipe, profile = checked_inputs[key]
        if (run['recipe_id'] != recipe['recipe_id'] or run['deployment_scope'] != recipe['deployment']['scope'] or
                run['hardware_profile'] != recipe['hardware_profile'] or
                run['hardware_profile_revision'] != profile['profile_revision'] or
                not any(all(platform[k] == v for k, v in run['platform'].items())
                        for platform in recipe['platforms'])):
            raise ValueError('indexed Forge execution disagrees with current deployment attribution')
    return True
