#!/usr/bin/env python3
"""Maintainer-only data import: inspect by default; review, optionally upload, then apply."""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from urllib.parse import urlparse

import jsonschema
import yaml

from contributor_bundle import (WORKLOADS, checksum, json_bytes, json_load, read_archive,
                                replay, runtime_identity, shareable)
from fetch_forge_evidence import append_reference, checked_path, tested_inputs
from forge_serving_contract import validate_launch
from prepare_contributor import source_files


def validate_evidence(repo: Path, request: dict, files: dict, *, historical=False):
    from validate import load_schema_registry, validate_document, load_schema
    if historical:
        # Replay preserves historical evidence, not a claim that current inputs
        # were tested. New admissions below still require current-input equality.
        path = checked_path(repo, request['recipe_path'])
        recipe = yaml.safe_load(files['inputs/' + request['recipe_path']])
    else:
        path, recipe = tested_inputs(repo, request['recipe_path'], request['tested_revision'])
    errors = validate_document(path, recipe, load_schema(repo, 'recipe'), load_schema_registry(repo))
    if errors:
        raise ValueError('current recipe schema validation failed: ' + '; '.join(errors))
    folder = str(Path(request['manifest_path']).parent)
    expected = source_files(repo, request['recipe_path'], request['tested_revision'], folder)
    if {name: data for name, data in files.items() if name.startswith('inputs/')} != expected:
        raise ValueError('bundled input closure differs from tested Git snapshot')
    runtime = json_load(files['runtime.json'])
    shareable(runtime)
    if runtime['harness_invocation']['image'] != request['harness_image']:
        raise ValueError('executed harness image differs from request pin')
    if (runtime['launch']['kind'] == 'LLMInferenceService') != (request['platform']['stack'] == 'rhoai'):
        raise ValueError('serving kind differs from selected platform')
    if not isinstance(runtime['uid_admission'], list):
        raise ValueError('UID admission decisions must be a list')
    validate_launch(repo, request['manifest_path'], request['tested_revision'], runtime['launch'],
                    require_current=not historical, uid_admission=runtime['uid_admission'])
    revision, image_digest = runtime_identity({**runtime, '_recipe': recipe}, request)
    result = replay(request, files)
    if json_load(files['result.json']) != result:
        raise ValueError('submitted normalized result differs from raw KPI replay')
    return path, recipe, result, revision, image_digest


def replay_run(repo: Path, path: Path, run: dict):
    """Called by ordinary local/pre-commit/CI validation without network or GPU access."""
    request = json_load((path.parent / 'request.json').read_bytes())
    files = {name: (path.parent / name).read_bytes() for name in ('kpis.json', 'runtime.json', 'result.json')}
    for name in request['inputs']:
        files['inputs/' + name] = subprocess.run([
            'git', '-C', str(repo), 'show', f'{request["tested_revision"]}:{name}'],
            check=True, capture_output=True).stdout
    _, recipe, result, revision, digest = validate_evidence(repo, request, files, historical=True)
    if (run['run_id'] != result['run_id'] or run['recipe_id'] != recipe['recipe_id']
            or run['platform'] != request['platform'] or run['hardware_profile'] != recipe['hardware_profile']
            or run['environment']['model_revision'] != revision or run['environment']['image_digest'] != digest
            or run['deployment_scope'] != recipe['deployment']['scope']
            or run['hardware_profile_revision'] != yaml.safe_load(files['inputs/' + recipe['hardware_profile']])['profile_revision']
            or run['harness'] != WORKLOADS[request['workload']]['harness']
            or run['workload'] != WORKLOADS[request['workload']]
            or run['command'] != 'python /opt/forge/projects/inference_playbooks/orchestration/cli.py test'
            or run['environment'].get('validation_origin') != 'contributor'):
        raise ValueError('contributor run attribution differs from replay')
    for filename in ('request.json', 'runtime.json', 'kpis.json'):
        if checksum((path.parent / filename).read_bytes()) != run['contributor'][filename.replace('.json', '_checksum')]:
            raise ValueError('contributor evidence checksum mismatch')
        if not any(a.get('path') == filename and a.get('checksum') == checksum((path.parent / filename).read_bytes())
                   for a in run['artifacts']):
            raise ValueError('checksummed contributor replay artifact reference missing')
    native = False
    for artifact in run['artifacts']:
        if artifact.get('path'):
            target = path.parent / artifact['path']
            if target.is_symlink() or not target.resolve().is_relative_to(path.parent.resolve()):
                raise ValueError('contributor artifact escapes run directory')
            if checksum(target.read_bytes()) != artifact.get('checksum'):
                raise ValueError('contributor artifact checksum mismatch')
            if artifact['path'].startswith('raw-harness-'):
                shareable(json_load(target.read_bytes()))
                native = True
        elif artifact.get('uri') and artifact.get('checksum') == run['contributor']['bundle_checksum']:
            native = True  # Retained full native exports; ordinary replay remains offline.
    if not native:
        raise ValueError('native harness export provenance missing')
    return result


def upload_archive(path: Path, prefix: str, digest: str) -> str:
    """No contributor credentials; immutable single-object writes using maintainer AWS CLI."""
    parsed = urlparse(prefix)
    if parsed.scheme != 's3' or not parsed.netloc or parsed.params or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('use a durable s3://bucket/prefix without credentials or query strings')
    key = parsed.path.strip('/') + '/' + digest.removeprefix('sha256:') + '.tar.gz'
    key = key.lstrip('/')
    encoded = base64.b64encode(bytes.fromhex(digest.removeprefix('sha256:'))).decode()
    # Conditional write fails closed on conflicts; no read/list/overwrite/delete permission needed.
    command = ['aws', 's3api', 'put-object', '--bucket', parsed.netloc, '--key', key,
               '--body', str(path), '--if-none-match', '*', '--checksum-algorithm', 'SHA256',
               '--checksum-sha256', encoded]
    result = subprocess.run(command, capture_output=True)
    if result.returncode:
        # An existing identical checksummed object is a retry, not an overwrite.
        head = subprocess.run(['aws', 's3api', 'head-object', '--bucket', parsed.netloc, '--key', key,
                               '--checksum-mode', 'ENABLED'], check=True, capture_output=True)
        if json_load(head.stdout).get('ChecksumSHA256') != encoded:
            raise ValueError('S3 object conflict or upload failure; local evidence preserved')
    elif json_load(result.stdout).get('ChecksumSHA256') != encoded:
        raise ValueError('S3 did not confirm the uploaded SHA-256')
    return f's3://{parsed.netloc}/{key}'


def import_bundle(repo: Path, bundle: Path, digest: str, *, apply=False, reviewed=False, s3_prefix=None):
    envelope, files = read_archive(bundle, digest)
    if envelope['outcome'] != 'succeeded':
        raise ValueError('failed/interrupted/incomplete executions are diagnostic-only, not importable evidence')
    if not any(name.startswith('raw/') for name in files):
        raise ValueError('native harness exports missing')
    request = envelope['request']
    path, recipe, result, revision, image_digest = validate_evidence(repo, request, files)
    runtime = json_load(files['runtime.json'])
    exports = runtime.get('native_exports', [])
    if ({entry['path']: entry['export_checksum'] for entry in exports}
            != {name: checksum(data) for name, data in files.items() if name.startswith('raw/')}):
        raise ValueError('native export provenance differs from bundle inventory')
    workload = WORKLOADS[request['workload']]
    directory = path.parent / 'results' / result['run_id']
    if (any(p.is_symlink() for p in (directory, directory.parent))
            or not directory.resolve().is_relative_to(path.parent.resolve())):
        raise ValueError('run path escapes recipe')
    output = {'request.json': json_bytes(request), 'runtime.json': files['runtime.json'],
              'kpis.json': files['kpis.json'], 'result.json': json_bytes(result)}
    artifacts = [{'type': 'raw-output', 'path': 'kpis.json', 'checksum': checksum(files['kpis.json'])},
                 {'type': 'harness-config', 'path': 'request.json', 'checksum': checksum(output['request.json'])},
                 {'type': 'other', 'path': 'runtime.json', 'checksum': checksum(output['runtime.json'])}]
    if not s3_prefix:
        for name, data in files.items():
            if name.startswith('raw/'):
                target = name.replace('/', '-')
                output[target] = data
                artifacts.append({'type': 'raw-output', 'path': target, 'checksum': checksum(data)})
    run = {'schema_version': 2, 'evidence_source': 'contributor-forge', 'run_id': result['run_id'],
           'recipe_id': recipe['recipe_id'], 'deployment_scope': recipe['deployment']['scope'],
           'hardware_profile': recipe['hardware_profile'],
           'hardware_profile_revision': yaml.safe_load(files['inputs/' + recipe['hardware_profile']])['profile_revision'],
           'platform': request['platform'], 'harness': workload['harness'], 'result': 'result.json',
           'command': 'python /opt/forge/projects/inference_playbooks/orchestration/cli.py test',
           'workload': workload,
           'environment': {'model_revision': revision, 'image_digest': image_digest,
                           'platform': request['platform'], 'validation_origin': 'contributor',
                           'qualification': 'Contributor-controlled execution; not independent validation.'},
           'contributor': {'bundle_checksum': digest, **{
               name.replace('.json', '_checksum'): checksum(output[name])
               for name in ('request.json', 'runtime.json', 'kpis.json')}}, 'artifacts': artifacts}
    def finalize():
        jsonschema.validate(run, json_load((repo / 'schema/benchmark-run.schema.json').read_bytes()))
        jsonschema.validate(result, json_load((repo / 'schema/benchmark-result.schema.json').read_bytes()))
        output['run.yaml'] = yaml.safe_dump(run, sort_keys=False).encode()
        for name, data in output.items():
            target = directory / name
            if target.exists() and (target.is_symlink() or target.read_bytes() != data):
                raise ValueError('immutable run already exists with different evidence')
        if directory.exists() and not {p.name for p in directory.iterdir()}.issubset(output):
            raise ValueError('existing run has different artifact coverage')
        return append_reference(path.read_text(), str((directory / 'run.yaml').relative_to(path.parent)))
    # Check all local identities/conflicts before using storage credentials or writing files.
    if s3_prefix:
        uri = s3_prefix.rstrip('/') + '/' + digest.removeprefix('sha256:') + '.tar.gz'
        run['artifacts'].append({'type': 'raw-output', 'uri': uri, 'checksum': digest,
                                 'size_bytes': bundle.stat().st_size})
    original = path.read_text()
    updated = finalize()
    report = {'run_id': result['run_id'], 'completeness': result['completeness'],
              'destination': str(directory), 'applied': False, 'maturity_changed': False}
    if not apply:
        return report
    if not reviewed:
        raise ValueError('apply requires --reviewed after a maintainer privacy/provenance review')
    if s3_prefix:
        uri = upload_archive(bundle, s3_prefix, digest)
        if uri != run['artifacts'][-1]['uri']:
            raise ValueError('unexpected storage destination')
    if path.read_text() != original:
        raise ValueError('recipe changed during import; retry without overwriting concurrent edits')
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in output.items():
        target = directory / name
        if not target.exists():
            with target.open('xb') as handle:
                handle.write(data)
    if path.read_text() != updated:
        path.write_text(updated)
    report['applied'] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('--checksum', required=True, help='sha256:... from the returned sidecar')
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--reviewed', action='store_true')
    parser.add_argument('--s3-prefix', help='optional maintainer-owned durable s3://bucket/prefix; requires AWS CLI v2')
    args = parser.parse_args()
    try:
        print(json.dumps(import_bundle(args.repo.resolve(), args.bundle, args.checksum,
                                       apply=args.apply, reviewed=args.reviewed, s3_prefix=args.s3_prefix), indent=2))
        return 0
    except (ValueError, KeyError, TypeError, OSError, EOFError, RecursionError, tarfile.TarError,
            subprocess.CalledProcessError, jsonschema.ValidationError) as error:
        print(f'Contributor evidence rejected: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
