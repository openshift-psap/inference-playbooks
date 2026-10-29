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
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath

from fetch_forge_evidence import safe_artifact
from forge_model_identity import kpi_labels, validate_model_identity
from normalize_kpis import normalize
from recipe_evidence import load_unique_yaml, load_unique_yaml_all

FORGE_REVISION = '93b85db135425516eb000de62d2483c62cdacacc'
LEGACY_FORGE_REVISION = '768a285851ade70d0d5737f9bd90564350410469'
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
    raw = path.read_bytes()
    if checksum(raw) != expected_checksum:
        raise ValueError('bundle checksum mismatch')
    files = {}
    # No extractall, filesystem writes, symlinks, devices, sparse files or executables.
    # Stream tar decoding rather than holding a second, fully expanded archive.
    # Artifact byte sizes are not capped; memory/storage must fit the evidence.
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r|gz') as archive:
        for member in archive:
            name = relative(member.name)
            if (not member.isfile() or member.issparse() or member.mode & 0o111
                    or name in files or member.size < 0):
                raise ValueError('unsafe or duplicate archive member')
            if len(files) >= MAX_FILES:
                raise ValueError('bundle exceeds file-count limit')
            files[name] = archive.extractfile(member).read()
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
    if len(files) >= MAX_FILES:
        raise ValueError('bundle exceeds file-count limit')
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
    shareable(request)
    if not re.fullmatch(r'[a-f0-9]{40}', request['tested_revision']):
        raise ValueError('tested revision must be an immutable Git commit')
    if request['forge_revision'] not in {FORGE_REVISION, LEGACY_FORGE_REVISION}:
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
    if request['forge_revision'] == FORGE_REVISION:
        portable_manifest(request, files)
    return recipe, model, profile


def portable_manifest(request, files):
    """The new Forge contract admits one unchanged, self-contained serving object."""
    path = PurePosixPath(request['manifest_path'])
    folder = PurePosixPath(request['recipe_path']).parent / 'manifests' / f'{request["platform"]["stack"]}-{request["platform"]["version"]}'
    manifests = {name for name in request['inputs'] if PurePosixPath(name).is_relative_to(folder)}
    if path.parent != folder or manifests != {str(path)}:
        raise ValueError('initial runner requires one self-contained YAML manifest per platform')
    documents = [doc for doc in load_unique_yaml_all(files['inputs/' + str(path)].decode()) if doc is not None]
    if (len(documents) != 1 or not isinstance(documents[0], dict)
            or documents[0].get('kind') not in {'Deployment', 'LeaderWorkerSet', 'LLMInferenceService'}):
        raise ValueError('requires one serving resource; authored Services and companions are unsupported')
    manifest = documents[0]
    if manifest['kind'] == 'LLMInferenceService':
        if 'prefill' in manifest['spec']:
            raise ValueError('LLMInferenceService P/D is not supported')
        if not manifest['spec']['model']['uri'].startswith('pvc://'):
            raise ValueError('standalone LLMInferenceService requires an existing populated PVC')
        def hostpath(value):
            if isinstance(value, dict):
                return ('hostPath' in value or any(hostpath(child) for child in value.values()))
            return isinstance(value, list) and any(hostpath(child) for child in value)
        if hostpath(manifest['spec']):
            raise ValueError('LLMInferenceService hostPath volumes are unsupported; use PVC-backed storage')
    return manifest


def replay(request: dict, files: dict[str, bytes]) -> dict:
    recipe, model, profile = validate_request(request, files)
    document = json_load(files['kpis.json'])
    safe_artifact(document)
    validate_model_identity(document, recipe, model)
    workload = WORKLOADS[request['workload']]
    harness = workload['harness']
    for test in document['tests']:
        labels = kpi_labels(test)
        if (labels.get('benchmark_tool') != harness or labels.get('benchmark_key') != workload['key']
                or labels.get('served_model') != recipe['serving']['model']):
            raise ValueError('KPI workload/harness/checkpoint attribution mismatch')
        definition = workload['definition']
        tokens = (data_tokens(definition['data']) if harness == 'guidellm'
                  else {name: definition[name] for name in ('input_tokens', 'output_tokens')})
        for label, key in (('prompt_toks', 'prompt_tokens' if harness == 'guidellm' else 'input_tokens'),
                           ('output_toks', 'output_tokens')):
            if label in labels and workload_number(labels[label]) != workload_number(tokens[key]):
                raise ValueError('KPI token labels differ from requested workload')
    levels = workload['definition'].get('rates', [workload['definition'].get('concurrency', 1)])
    results = normalize(document, run_id=request['run_id'], deployment_scope=recipe['deployment']['scope'],
                        accelerator_key=profile['accelerator_key'], expected_concurrencies={harness: levels})
    if set(results) != {harness}:
        raise ValueError('unexpected KPI harness set')
    return results[harness]


def workload_number(value):
    """Compare equivalent numeric strings exactly, rejecting bool/nonfinite data."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('invalid workload token/count parameter')
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('invalid workload token/count parameter') from None
    if not result.is_finite() or result < 0:
        raise ValueError('invalid workload token/count parameter')
    return result


def data_tokens(value):
    """Normalize the supported synthetic GuideLLM data specification."""
    if not isinstance(value, str):
        raise ValueError('invalid workload token data')
    parts = [part.strip().partition('=') for part in value.split(',')]
    if any(not key or not separator for key, separator, _ in parts):
        raise ValueError('invalid workload token data')
    values = {key: item for key, _, item in parts}
    if len(values) != len(parts) or values.pop('kind', 'synthetic_text') != 'synthetic_text':
        raise ValueError('invalid workload token data')
    if set(values) != {'prompt_tokens', 'output_tokens'}:
        raise ValueError('invalid workload token data')
    return {key: workload_number(item) for key, item in values.items()}


def validate_harness_invocation(request, invocation):
    """Bind captured harness arguments to the workload, not just its image."""
    if not isinstance(invocation, dict) or invocation.get('image') != request['harness_image']:
        raise ValueError('executed harness image differs from request pin')
    workload = WORKLOADS[request['workload']]
    definition = workload['definition']
    if workload['harness'] == 'aiperf':
        for key in ('input_tokens', 'output_tokens', 'concurrency', 'request_count'):
            if workload_number(invocation.get(key)) != workload_number(definition[key]):
                raise ValueError('executed harness workload parameters differ from request')
        return
    args = invocation.get('guidellm_args')
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError('recorded GuideLLM workload arguments required')
    options, index = {}, 0
    while index < len(args):
        flag, separator, value = args[index].partition('=')
        if not flag.startswith('--'):
            raise ValueError('invalid recorded GuideLLM workload arguments')
        index += 1
        if not separator and index < len(args) and not args[index].startswith('--'):
            value = args[index]
            index += 1
        if flag in options:
            raise ValueError('duplicate recorded GuideLLM workload argument')
        options[flag] = value
    if (data_tokens(options.get('--data')) != data_tokens(definition['data'])
            or options.get('--rate-type', 'concurrent') != 'concurrent'
            or workload_number(options.get('--max-seconds')) != workload_number(definition['max_seconds'])):
        raise ValueError('executed harness workload parameters differ from request')
    rates = options.get('--rate', '').split(',')
    if [workload_number(value.strip()) for value in rates] != [workload_number(value) for value in definition['rates']]:
        raise ValueError('executed harness workload parameters differ from request')


def runtime_identity(runtime: dict, request: dict) -> tuple[str, str]:
    """Require ready matching serving roles both before and after the workload."""
    from fetch_forge_evidence import distributed_serving_values, serving_arguments
    from contributor_security import check_runtime_uids, runtime_mounts
    launch = runtime['launch']
    recipe = runtime.get('_recipe')
    identities = []
    for key in ('pods_before', 'pods_after'):
        pods = runtime[key]
        check_runtime_uids(pods, runtime['uid_admission'])
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
            for field in ('command', 'args', 'env', 'resources'):
                if matches[0].get(field, []) != server.get(field, []):
                    raise ValueError('runtime serving configuration differs from launch')
            if runtime_mounts(pod['spec'], matches[0], launch['spec']['template']['spec']) != server.get('volumeMounts', []):
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
