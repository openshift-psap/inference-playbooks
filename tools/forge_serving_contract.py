"""Bind captured launch configuration to the selected manifest at the tested SHA."""
import copy
import hashlib
import math
import re
import subprocess
from pathlib import Path, PurePosixPath

import yaml

from recipe_evidence import load_unique_yaml, load_unique_yaml_all


def model_mount_matches(pod, container, uri, model):
    """Bind a controller-generated local path to its declared PVC, not an HF ID."""
    if uri.startswith('hf://'):
        return model == uri.removeprefix('hf://')
    if not uri.startswith('pvc://') or not isinstance(model, str):
        return False
    claim, _, directory = uri.removeprefix('pvc://').partition('/')
    if (not claim or PurePosixPath(directory).is_absolute() or '..' in PurePosixPath(directory).parts
            or not PurePosixPath(model).is_absolute()):
        return False
    source_volumes = pod['spec'].get('volumes', [])
    volumes = {volume['name']: volume for volume in source_volumes}
    if len(volumes) != len(source_volumes):
        return False
    for mount in container.get('volumeMounts', []):
        volume = volumes.get(mount.get('name'), {})
        if volume.get('persistentVolumeClaim', {}).get('claimName') != claim or 'subPathExpr' in mount:
            continue
        if mount.get('subPath', '') == directory:
            expected = mount.get('mountPath')
        elif not mount.get('subPath'):
            expected = str(PurePosixPath(mount.get('mountPath', '')) / directory)
        else:
            continue
        if model == expected:
            return True
    return False


PRIMARY_KINDS = {'Deployment', 'LeaderWorkerSet', 'LLMInferenceService'}
PRIMARY_APIS = {'Deployment': 'apps/v1', 'LeaderWorkerSet': 'leaderworkerset.x-k8s.io/v1',
                'LLMInferenceService': 'serving.kserve.io/v1alpha2'}
BUNDLE_FILES = ('bundle-metadata.yaml', 'source-bundle.yaml', 'submitted-bundle.yaml')


def same_document(actual, expected):
    """YAML scalar types matter: Python's True == 1 is not serving equality."""
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return ({(type(key), key) for key in actual} == {(type(key), key) for key in expected} and
                all(same_document(actual[key], value) for key, value in expected.items()))
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(map(same_document, actual, expected))
    if type(expected) is float and not math.isfinite(expected):
        return False
    return (expected is None or type(expected) in (str, int, float, bool)) and actual == expected


def bundle_documents(raw):
    """Never include raw YAML or parser context in an intake error."""
    try:
        documents = [doc for doc in load_unique_yaml_all(raw.decode('utf-8')) if doc is not None]
    except (UnicodeError, yaml.YAMLError, ValueError, TypeError, RecursionError):
        raise ValueError('invalid Forge bundle YAML') from None
    if not documents:
        raise ValueError('Forge bundle requires nonempty object documents')
    return documents


def placed_objects(documents, namespace, *, submitted=False):
    """Known namespaced serving objects only; no wrappers or filtered companions."""
    objects = {}
    for original in documents:
        if (not isinstance(original, dict) or not isinstance(original.get('kind'), str) or
                original['kind'] not in PRIMARY_KINDS):
            raise ValueError('unsupported Forge bundle resource kind (Services and Secrets are forbidden)')
        api, metadata = original.get('apiVersion'), original.get('metadata')
        if (api != PRIMARY_APIS[original['kind']] or not isinstance(metadata, dict) or
                not isinstance(metadata.get('name'), str) or not metadata['name']):
            raise ValueError('Forge bundle object identity is missing or invalid')
        if 'namespace' in metadata and metadata['namespace'] != namespace:
            raise ValueError('Forge bundle explicit namespace differs from target namespace')
        if submitted and 'namespace' not in metadata:
            raise ValueError('Forge submitted bundle is missing target namespace placement')
        placed = copy.deepcopy(original)
        placed['metadata']['namespace'] = namespace
        identity = api.partition('/')[0], original['kind'], metadata['name'], namespace
        if identity in objects:
            raise ValueError('duplicate Forge bundle object identity')
        objects[identity] = placed
    return objects


def validate_bundle(repo, manifest_path, revision, recipe_id, recipe_path, namespace,
                    metadata_raw, source_raw, submitted_raw):
    """Re-read complete customer artifacts from Git; never execute historical code.

    Contract v1 source selection mirrors catalog.artifacts_for: immediate YAML/YML
    files in the selected platform directory, not a primary-resource subset.
    """
    if (not isinstance(namespace, str) or not re.fullmatch(r'[a-z0-9](?:[-a-z0-9]*[a-z0-9])?', namespace)
            or len(namespace) > 63):
        raise ValueError('Forge bundle target namespace is missing or invalid')
    if (not isinstance(manifest_path, str) or any(part in {'', '.', '..'} for part in manifest_path.split('/')) or
            not re.fullmatch(r'models/[^/]+/recipes/[^/]+/manifests/[^/]+/[^/]+\.ya?ml', manifest_path) or
            not re.fullmatch('[a-f0-9]{40}', revision)):
        raise ValueError('Forge bundle selection must identify a canonical tested platform manifest')
    if recipe_path != str(PurePosixPath(manifest_path).parents[2] / 'recipe.yaml'):
        raise ValueError('Forge bundle recipe path disagrees with selected manifest')
    try:
        metadata = load_unique_yaml(metadata_raw.decode('utf-8'))
    except (UnicodeError, yaml.YAMLError, ValueError, TypeError, RecursionError):
        raise ValueError('invalid Forge bundle metadata YAML') from None
    fields = {'contract_version', 'recipe_id', 'recipe_path', 'recipe_revision', 'forge_revision',
              'target_namespace', 'source_files', 'source_bundle_sha256', 'submitted_bundle_sha256'}
    if (not isinstance(metadata, dict) or set(metadata) != fields or
            type(metadata['contract_version']) is not int or metadata['contract_version'] != 1 or
            metadata['recipe_id'] != recipe_id or metadata['recipe_path'] != recipe_path or
            metadata['recipe_revision'] != revision or metadata['target_namespace'] != namespace or
            not isinstance(metadata['forge_revision'], str) or
            not re.fullmatch('[a-f0-9]{40}', metadata['forge_revision'])):
        raise ValueError('Forge bundle metadata attribution mismatch')
    for key, raw in (('source_bundle_sha256', source_raw), ('submitted_bundle_sha256', submitted_raw)):
        if metadata[key] != hashlib.sha256(raw).hexdigest():
            raise ValueError('Forge bundle artifact checksum mismatch')

    repo = Path(repo).resolve()
    folder = PurePosixPath(manifest_path).parent
    tree = subprocess.run(['git', '-C', str(repo), 'ls-tree', '-z', f'{revision}:{folder}'],
                          check=True, capture_output=True).stdout.decode('utf-8')
    expected_files, expected_documents = [], []
    paths = []
    for entry in tree.split('\0'):
        if not entry:
            continue
        details, name = entry.split('\t', 1)
        if PurePosixPath(name).suffix not in {'.yaml', '.yml'}:
            continue
        mode, kind, _ = details.split()
        if kind != 'blob' or mode not in {'100644', '100755'}:
            raise ValueError('Forge bundle source must be a regular Git file')
        paths.append(str(folder / name))
    if not paths or manifest_path not in paths:
        raise ValueError('Forge bundle selected source is missing')
    current_paths = sorted(str(path.relative_to(repo)) for path in (repo / folder).glob('*')
                           if path.suffix in {'.yaml', '.yml'})
    if current_paths != sorted(paths):
        raise ValueError('current customer artifact set differs from tested revision')
    for path in sorted(paths):
        current = repo
        for part in PurePosixPath(path).parts:
            current /= part
            if current.is_symlink():
                raise ValueError('Forge bundle source must not traverse a symlink')
        raw = subprocess.run(['git', '-C', str(repo), 'show', f'{revision}:{path}'],
                             check=True, capture_output=True).stdout
        if not current.is_file() or current.read_bytes() != raw:
            raise ValueError('selected manifest differs from tested revision')
        documents = bundle_documents(raw)
        expected_documents.extend(documents)
        expected_files.append({'path': path, 'sha256': hashlib.sha256(raw).hexdigest(),
                               'document_count': len(documents)})
    try:
        if not same_document(metadata['source_files'], expected_files):
            raise ValueError('Forge bundle source inventory differs from tested revision')
        source, submitted = bundle_documents(source_raw), bundle_documents(submitted_raw)
        if not same_document(source, expected_documents):
            raise ValueError('Forge source bundle differs from tested manifest documents')
        expected = placed_objects(source, namespace)
        actual = placed_objects(submitted, namespace, submitted=True)
        if not same_document(actual, expected):
            raise ValueError('submitted bundle configuration differs from tested manifest')
        if len(actual) != 1:
            raise ValueError('Forge bundle serving resource is missing or ambiguous')
    except RecursionError:
        raise ValueError('invalid recursive Forge bundle data') from None
    return submitted[0]
