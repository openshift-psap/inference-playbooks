"""Read Forge's versioned complete-platform launch contract as data only."""
import copy
import hashlib
import json
import re
import subprocess
from pathlib import PurePosixPath

import yaml

from recipe_evidence import load_unique_yaml, load_unique_yaml_all


SOURCE = '01__test/001__inference_playbooks_test_dir/src'
SUPPORTED = {'Deployment': 'apps/v1', 'LeaderWorkerSet': 'leaderworkerset.x-k8s.io/v1',
             'LLMInferenceService': 'serving.kserve.io/v1alpha2'}


def yaml_data(raw, *, multiple=False):
    try:
        value = load_unique_yaml_all(raw) if multiple else load_unique_yaml(raw)
        json.dumps(value, allow_nan=False)
        return value
    except (yaml.YAMLError, ValueError, TypeError, RecursionError):
        raise ValueError('invalid Forge bundle YAML') from None


def canonical(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(',', ':'))


def read_bundle(repo, config, revision, artifact):
    """Bind complete source/submission artifacts to the explicitly tested Git tree."""
    selector = config.get('recipe')
    if not isinstance(selector, str) or not re.fullmatch(
            r'[a-z0-9][a-z0-9._-]*/recipes/[a-z0-9][a-z0-9._-]*', selector):
        raise ValueError('invalid Forge bundle recipe selector')
    relative = f'models/{selector}/recipe.yaml'
    if (repo / relative).resolve() != repo / relative:
        raise ValueError('Forge bundle recipe path must be canonical without symlinks')
    selection = config.get('manifest_path')
    if not isinstance(selection, str) or not re.fullmatch(
            r'manifests/[A-Za-z0-9._-]+/[^/]+\.ya?ml', selection):
        raise ValueError('invalid Forge bundle manifest selection')
    metadata = yaml_data(artifact(SOURCE + '/bundle-metadata.yaml'))
    if not isinstance(metadata, dict) or type(metadata.get('contract_version')) is not int or metadata['contract_version'] != 1:
        raise ValueError('unsupported Forge bundle contract version')
    if metadata.get('recipe_path') != relative or metadata.get('recipe_revision') != revision:
        raise ValueError('Forge bundle recipe path/revision attribution mismatch')
    if not isinstance(metadata.get('forge_revision'), str) or not re.fullmatch(r'[a-f0-9]{40}', metadata['forge_revision']):
        raise ValueError('invalid Forge bundle producer revision')
    from fetch_forge_evidence import tested_inputs
    path, recipe = tested_inputs(repo, relative, revision)
    if recipe.get('schema_version') != 4 or metadata.get('recipe_id') != recipe['recipe_id']:
        raise ValueError('Forge bundle recipe identity mismatch')
    platforms = [p for p in recipe['platforms']
                 if selection.split('/')[1] == f'{p["stack"]}-{p["version"]}']
    if len(platforms) != 1 or platforms[0].get('blocked'):
        raise ValueError('Forge bundle platform is missing, ambiguous or blocked')
    folder = str(PurePosixPath(relative).parent / PurePosixPath(selection).parent)
    listing = subprocess.run(['git', '-C', str(repo), 'ls-tree', '-z', f'{revision}:{folder}'],
                             check=True, capture_output=True).stdout.decode()
    inventory = []
    documents = []
    for entry in sorted(filter(None, listing.split('\0')), key=lambda e: e.split('\t', 1)[1]):
        attrs, name = entry.split('\t', 1)
        if PurePosixPath(name).suffix not in {'.yaml', '.yml'}:
            continue
        if attrs.split()[0] not in {'100644', '100755'}:
            raise ValueError('Forge bundle source must be a regular Git file')
        source_path = f'{folder}/{name}'
        raw = subprocess.run(['git', '-C', str(repo), 'show', f'{revision}:{source_path}'],
                             check=True, capture_output=True).stdout
        parsed = yaml_data(raw.decode(), multiple=True)
        if not parsed or any(not isinstance(doc, dict) for doc in parsed):
            raise ValueError('Forge bundle source requires object documents')
        documents.extend(parsed)
        inventory.append({'path': source_path, 'sha256': hashlib.sha256(raw).hexdigest(),
                          'document_count': len(parsed)})
    selected_path = str(PurePosixPath(relative).parent / selection)
    if selected_path not in {entry['path'] for entry in inventory} or canonical(metadata.get('source_files')) != canonical(inventory):
        raise ValueError('Forge bundle source inventory/checksum mismatch')
    # Current intake supports exactly one primary resource. InferenceService plus
    # ServingRuntime needs its own runtime attribution, not a guessed container.
    if len(documents) != 1 or documents[0].get('kind') not in SUPPORTED:
        raise ValueError('unsupported Forge bundle resource set')
    source = documents[0]
    if source.get('apiVersion') != SUPPORTED[source['kind']]:
        raise ValueError('unsupported Forge bundle API version')
    if (source['kind'] == 'LLMInferenceService') != (platforms[0]['stack'] == 'rhoai'):
        raise ValueError('Forge bundle serving kind disagrees with selected platform')
    namespace = metadata.get('target_namespace')
    if (not isinstance(namespace, str) or len(namespace) > 63 or
            not re.fullmatch(r'[a-z0-9](?:[-a-z0-9]*[a-z0-9])?', namespace) or
            config.get('namespace') != namespace):
        raise ValueError('Forge bundle namespace attribution mismatch')
    expected = copy.deepcopy(documents)
    object_metadata = expected[0].get('metadata', {})
    if not object_metadata.get('name') or 'generateName' in object_metadata:
        raise ValueError('Forge bundle requires a named object')
    if 'namespace' not in object_metadata:
        object_metadata['namespace'] = namespace
    elif object_metadata['namespace'] != namespace:
        raise ValueError('Forge bundle explicit namespace mismatch')
    for name, wanted in (('source', documents), ('submitted', expected)):
        raw = artifact(f'{SOURCE}/{name}-bundle.yaml')
        if hashlib.sha256(raw.encode()).hexdigest() != metadata.get(f'{name}_bundle_sha256'):
            raise ValueError('Forge bundle artifact checksum mismatch')
        if canonical(yaml_data(raw, multiple=True)) != canonical(wanted):
            raise ValueError('Forge bundle differs from Git source beyond namespace placement')
    return path, recipe, platforms[0], expected[0], metadata
