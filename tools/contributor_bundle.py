"""Portable contributor evidence: data-only archives and deterministic offline replay.

The archive is not an execution attestation. Never run code from a returned bundle.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path, PurePosixPath

from fetch_forge_evidence import safe_artifact
from forge_model_identity import validate_model_identity
from normalize_kpis import normalize
from recipe_evidence import load_unique_yaml, load_unique_yaml_all

FORGE_REVISION = '768a285851ade70d0d5737f9bd90564350410469'
MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
MAX_FILES = 1024
IMAGE = r'[^\s/@]+(?:/[^\s/@]+)+@sha256:[a-f0-9]{64}'
WORKLOADS = {
    'guidellm-8k1k': {'key': 'IP-8k/1k', 'harness': 'guidellm', 'definition': {
        'data': 'prompt_tokens=8000,output_tokens=1000',
        'rates': [1, 4, 16, 32, 64, 128], 'max_seconds': 300}},
    'guidellm-smoke': {'key': 'ci-quick', 'harness': 'guidellm', 'definition': {
        'data': 'prompt_tokens=1000,output_tokens=1000', 'rates': [1, 5], 'max_seconds': 60}},
    'aiperf-smoke': {'key': 'aiperf-smoke', 'harness': 'aiperf', 'definition': {
        'tool': 'aiperf', 'concurrency': 1, 'request_count': 10,
        'input_tokens': 128, 'output_tokens': 64}},
}


def checksum(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def json_bytes(value) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def json_load(data: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def relative(name: str) -> str:
    if (not isinstance(name, str) or not name or len(name) > 512
            or '\\' in name or re.search(r'[\x00-\x20\x7f]', name)
            or PurePosixPath(name).is_absolute() or '..' in name.split('/')
            or str(PurePosixPath(name)) != name):
        raise ValueError('noncanonical or escaping bundle path')
    return name


def shareable(value, _budget=None, _depth=0):
    """Conservative source-side guard; not a substitute for human privacy review."""
    if _budget is None:
        _budget = [100000]
    _budget[0] -= 1
    if _budget[0] < 0 or _depth > 64:
        raise ValueError('evidence structure exceeds complexity limit')
    if isinstance(value, dict):
        if value.get('kind') == 'Secret':
            raise ValueError('Secret contents must not enter evidence')
        for entry in value.get('env', []) if isinstance(value.get('env'), list) else []:
            if 'value' in entry and re.search(r'(?i)(?:^|_)(?:token|password|secret|api_?key)(?:$|_)', str(entry.get('name', ''))):
                raise ValueError('literal credential environment value must use a local Secret reference')
        for key, item in value.items():
            if re.search(r'(?i)(password|authorization|api[_-]?key|access[_-]?token|hostname|ip_address|^prompts?$)', key):
                raise ValueError('sensitive evidence field; keep private diagnostics local')
            shareable(item, _budget, _depth + 1)
    elif isinstance(value, list):
        for item in value:
            shareable(item, _budget, _depth + 1)
    elif isinstance(value, str):
        # Wildcard/loopback bind configuration identifies no contributor host.
        checked = re.sub(r'(?<![0-9.])(?:0\.0\.0\.0|127\.0\.0\.1)(?![0-9.])', '${BIND_ADDRESS}', value)
        safe_artifact(checked)
        if re.search(r'(?:\.svc\.cluster\.local\b|-----BEGIN .*PRIVATE KEY|\b[A-Za-z0-9-]+\.(?:internal|local)\b)', value):
            raise ValueError('private endpoint or key in evidence')


def read_archive(path: Path, expected_checksum: str) -> tuple[dict, dict[str, bytes]]:
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', expected_checksum):
        raise ValueError('explicit SHA-256 required')
    if path.stat().st_size > MAX_TOTAL:
        raise ValueError('compressed bundle exceeds limit')
    raw = path.read_bytes()
    if checksum(raw) != expected_checksum:
        raise ValueError('bundle checksum mismatch')
    files, size = {}, 0
    # No extractall, filesystem writes, symlinks, devices, sparse files or executables.
    # Bound decompression before tar parsing (including PAX/GNU extended headers).
    with gzip.GzipFile(fileobj=io.BytesIO(raw), mode='rb') as zipped:
        expanded = zipped.read(MAX_TOTAL + 1)
    if len(expanded) > MAX_TOTAL:
        raise ValueError('expanded bundle exceeds limit')
    with tarfile.open(fileobj=io.BytesIO(expanded), mode='r:') as archive:
        for member in archive:
            name = relative(member.name)
            if (not member.isfile() or member.issparse() or member.mode & 0o111
                    or name in files or member.size < 0 or member.size > MAX_FILE):
                raise ValueError('unsafe, duplicate, or oversized archive member')
            size += member.size
            if size > MAX_TOTAL or len(files) >= MAX_FILES:
                raise ValueError('expanded bundle exceeds limit')
            files[name] = archive.extractfile(member).read(MAX_FILE + 1)
            if len(files[name]) != member.size:
                raise ValueError('truncated archive member')
    envelope = json_load(files.pop('bundle.json'))
    if (not isinstance(envelope, dict) or set(envelope) != {'schema_version', 'request', 'outcome', 'files'}
            or type(envelope['schema_version']) is not int or envelope['schema_version'] != 1):
        raise ValueError('unsupported bundle envelope')
    if envelope['outcome'] not in {'succeeded', 'failed', 'interrupted', 'collection-failed'}:
        raise ValueError('unknown execution outcome')
    if envelope['files'] != {name: checksum(data) for name, data in files.items()}:
        raise ValueError('bundle inventory mismatch')
    validate_request(envelope['request'], files)
    for name, data in files.items():
        if name.startswith('inputs/'):
            for document in load_unique_yaml_all(data.decode()):
                shareable(document)
        elif name in {'kpis.json', 'runtime.json', 'result.json'} or re.fullmatch(r'raw/harness-[0-9]+\.json', name):
            shareable(json_load(data))
        else:
            raise ValueError('bundle contains unsupported evidence file')
    return envelope, files


def write_archive(path: Path, request: dict, outcome: str, files: dict[str, bytes]) -> str:
    if (len(files) >= MAX_FILES or any(len(data) > MAX_FILE for data in files.values())
            or sum(len(data) for data in files.values()) > MAX_TOTAL // 2):
        raise ValueError('evidence exceeds portable bundle limits; keep private diagnostics local')
    envelope = {'schema_version': 1, 'request': request, 'outcome': outcome,
                'files': {relative(name): checksum(data) for name, data in files.items()}}
    payload = {'bundle.json': json_bytes(envelope), **files}
    # Reproducible bytes; no contributor usernames, filesystem metadata or timestamps.
    with path.open('xb') as output, gzip.GzipFile(fileobj=output, mode='wb', mtime=0, filename='') as zipped:
        with tarfile.open(fileobj=zipped, mode='w') as archive:
            for name, data in sorted(payload.items()):
                info = tarfile.TarInfo(relative(name))
                info.mode, info.size = 0o600, len(data)
                archive.addfile(info, io.BytesIO(data))
    digest = checksum(path.read_bytes())
    path.with_name(path.name + '.sha256').write_text(digest + '\n')
    return digest


def validate_request(request: dict, files: dict[str, bytes]):
    required = {'schema_version', 'recipe_path', 'tested_revision', 'platform', 'manifest_path',
                'inputs', 'forge_revision', 'runner_image', 'harness_image', 'workload', 'run_id'}
    if (not isinstance(request, dict) or set(request) != required
            or type(request['schema_version']) is not int or request['schema_version'] != 1):
        raise ValueError('unsupported request format')
    if not re.fullmatch(r'[a-f0-9]{40}', request['tested_revision']):
        raise ValueError('tested revision must be an immutable Git commit')
    if request['forge_revision'] != FORGE_REVISION:
        raise ValueError('unsupported Forge revision')
    for key in ('runner_image', 'harness_image'):
        registry = request[key].split('/')[0]
        if (not re.fullmatch(IMAGE, request[key])
                or (registry != 'localhost' and '.' not in registry and ':' not in registry)):
            raise ValueError('fully qualified digest-pinned images required')
    if not re.fullmatch(r'contributor-[a-f0-9]{32}', request['run_id']):
        raise ValueError('invalid contributor run identity')
    recipe_path = relative(request['recipe_path'])
    if not re.fullmatch(r'models/[^/]+/recipes/[^/]+/recipe.yaml', recipe_path):
        raise ValueError('canonical recipe path required')
    platform = request['platform']
    if set(platform) != {'stack', 'version'} or platform['stack'] not in {'vllm', 'rhoai'}:
        raise ValueError('unsupported platform')
    folder = str(PurePosixPath(recipe_path).parent / 'manifests' / f'{platform["stack"]}-{platform["version"]}')
    manifest = relative(request['manifest_path'])
    if not manifest.startswith(folder + '/') or not manifest.endswith('.yaml'):
        raise ValueError('manifest does not belong to the selected platform')
    workload = request['workload']
    if workload not in WORKLOADS:
        raise ValueError('unsupported workload; AgentX requires a separately audited immutable dataset contract')
    inputs = request['inputs']
    if not isinstance(inputs, dict) or not inputs:
        raise ValueError('input inventory required')
    for name, digest in inputs.items():
        relative(name)
        if not name.endswith(('.yaml', '.yml', '.json')) or not name.startswith(('models/', 'hardware-profiles/')):
            raise ValueError('unsupported input path')
        if checksum(files['inputs/' + name]) != digest:
            raise ValueError('input checksum mismatch')
    if {name for name in files if name.startswith('inputs/')} != {'inputs/' + name for name in inputs}:
        raise ValueError('unexpected bundled input')
    recipe = load_unique_yaml(files['inputs/' + recipe_path].decode())
    if not any(all(entry.get(k) == v for k, v in platform.items()) and not entry.get('blocked')
               for entry in recipe['platforms']):
        raise ValueError('selected platform missing or blocked')
    if workload == 'guidellm-8k1k' and recipe['workload_profile'] != workload:
        raise ValueError('benchmark workload differs from recipe')
    profile = load_unique_yaml(files['inputs/' + recipe['hardware_profile']].decode())
    model = load_unique_yaml(files[f'inputs/models/{recipe["model_id"]}/model.yaml'].decode())
    return recipe, model, profile


def replay(request: dict, files: dict[str, bytes]) -> dict:
    recipe, model, profile = validate_request(request, files)
    document = json_load(files['kpis.json'])
    safe_artifact(document)
    validate_model_identity(document, recipe, model)
    workload = WORKLOADS[request['workload']]
    harness = workload['harness']
    for test in document['tests']:
        labels = test.get('labels', {})
        if (labels.get('benchmark_tool') != harness or labels.get('benchmark_key') != workload['key']
                or labels.get('served_model') != recipe['serving']['model']):
            raise ValueError('KPI workload/harness/checkpoint attribution mismatch')
    levels = workload['definition'].get('rates', [workload['definition'].get('concurrency', 1)])
    results = normalize(document, run_id=request['run_id'], deployment_scope=recipe['deployment']['scope'],
                        accelerator_key=profile['accelerator_key'], expected_concurrencies={harness: levels})
    if set(results) != {harness}:
        raise ValueError('unexpected KPI harness set')
    return results[harness]


def runtime_identity(runtime: dict, request: dict) -> tuple[str, str]:
    """Require ready matching serving roles both before and after the workload."""
    from fetch_forge_evidence import distributed_serving_values, serving_arguments
    launch = runtime['launch']
    recipe = runtime.get('_recipe')
    identities = []
    for key in ('pods_before', 'pods_after'):
        pods = runtime[key]
        replicas = launch['spec'].get('replicas', 1)
        if launch['kind'] != 'Deployment':
            size = (launch['spec']['leaderWorkerTemplate']['size'] if launch['kind'] == 'LeaderWorkerSet'
                    else launch['spec'].get('parallelism', {}).get('pipeline', 1))
            if len(pods['items']) < replicas * size:
                raise ValueError('incomplete serving replica/role capture')
            identities.append(distributed_serving_values(launch, pods, recipe['serving']['model']))
            continue
        servers = [c for c in launch['spec']['template']['spec']['containers'] if c['name'] == 'vllm']
        if len(servers) != 1 or len(pods['items']) != replicas or replicas < 1:
            raise ValueError('missing serving runtime evidence')
        server = servers[0]
        expected_model, expected_revision = serving_arguments(server)
        digests = set()
        for pod in pods['items']:
            matches = [c for c in pod['spec']['containers'] if c['name'] == server['name']]
            if len(matches) != 1 or matches[0]['image'] != server['image']:
                raise ValueError('runtime serving image differs from launch')
            if serving_arguments(matches[0]) != (expected_model, expected_revision):
                raise ValueError('runtime model/revision differs from launch')
            for field in ('command', 'args', 'env', 'resources', 'volumeMounts'):
                if matches[0].get(field, []) != server.get(field, []):
                    raise ValueError('runtime serving configuration differs from launch')
            statuses = [s for s in pod['status']['containerStatuses'] if s['name'] == server['name'] and s.get('ready') is True]
            if len(statuses) != 1:
                raise ValueError('ready serving container evidence missing')
            digest = re.fullmatch(r'(?:\S+@)?(sha256:[a-f0-9]{64})', statuses[0].get('imageID', ''))
            if not digest:
                raise ValueError('runtime image digest missing')
            digests.add(digest[1])
        if len(digests) != 1:
            raise ValueError('heterogeneous serving image digests unsupported')
        identities.append((expected_revision or 'main', digests.pop()))
    if identities[0] != identities[1]:
        raise ValueError('serving identity changed during benchmark')
    return identities[0]
