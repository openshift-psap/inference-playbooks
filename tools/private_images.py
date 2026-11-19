"""Private serving-image bindings. Public operations never read the environment."""
from __future__ import annotations

import copy
import re

ENV_NAME = re.compile(r'^[A-Z_][A-Z0-9_]*$')
IMAGE_REF = re.compile(r'^[a-z0-9][a-z0-9.-]*(?::[0-9]+)?/'
                       r'[a-z0-9]+(?:[._/-]+[a-z0-9]+)*'
                       r'(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?'
                       r'(?:@sha256:[a-f0-9]{64})?$')
SERVERS = {'vllm', 'vllm-leader', 'vllm-worker', 'main'}
QUALIFICATION = 'Results collected using a private pre-release image; image identity is withheld.'


def binding(serving, overrides=None):
    overrides = overrides or {}
    replaces = any(key in overrides for key in ('image', 'image_env', 'image_visibility'))
    source = overrides if replaces else serving
    visibility = source.get('image_visibility', 'public')
    if visibility == 'private':
        name = source.get('image_env', '')
        if not isinstance(name, str) or not ENV_NAME.fullmatch(name) or 'image' in source:
            raise ValueError('private image requires image_env and prohibits literal image')
        return {'image_visibility': 'private', 'image_env': name}
    if visibility != 'public' or 'image_env' in source or not source.get('image'):
        raise ValueError('public image requires image and prohibits image_env')
    return {'image': source['image']}


def public_image(value):
    return ('private.invalid/runtime:' + value['image_env']
            if value.get('image_visibility') == 'private' else value['image'])


def merge_image_metadata(serving, overrides):
    selected = binding(serving, overrides)
    inherit = selected == binding(serving)
    result = {key: value for key, value in serving.items()
              if key not in {'image', 'image_env', 'image_visibility', 'engine', 'image_usage'}}
    result.update(selected)
    for key in ('engine', 'image_usage'):
        if key in overrides:
            result[key] = overrides[key]
        elif inherit and key in serving:
            result[key] = serving[key]
    return result


def request_binding(request, files):
    from pathlib import PurePosixPath
    from recipe_evidence import load_unique_yaml
    recipe = load_unique_yaml(files['inputs/' + request['recipe_path']].decode())
    platform = next(p for p in recipe['platforms']
                    if all(p[k] == v for k, v in request['platform'].items()))
    path = str(PurePosixPath(request['recipe_path']).parent / platform['overrides'])
    overrides = load_unique_yaml(files['inputs/' + path].decode()) or {}
    return binding(recipe['serving'], overrides)


def resolve_private(value, environment):
    """Execution-only: errors contain neither the value nor fragments of it."""
    image = environment.get(value['image_env'], '')
    if (not isinstance(image, str) or not IMAGE_REF.fullmatch(image)
            or not any(char in image.split('/')[0] for char in '.:')):
        raise ValueError('private image environment variable is missing or invalid')
    if image.startswith('private.invalid/'):
        raise ValueError('private image placeholder cannot be executed')
    return image


def replace_serving_images(document, expected, replacement):
    """Replace only declared serving containers; reject image drift in every role."""
    result = copy.deepcopy(document)
    found = 0
    def visit(value):
        nonlocal found
        if isinstance(value, dict):
            for container in value.get('containers', []):
                if container.get('name') in SERVERS:
                    if container.get('image') != expected:
                        raise ValueError('serving image differs from declared binding')
                    container['image'] = replacement
                    found += 1
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(result)
    if not found:
        raise ValueError('no supported serving containers for private image binding')
    return result


def private_tokens(image, digests=()):
    """Include registry/repository/tag fragments, not only the complete reference."""
    repository, _, digest = image.partition('@')
    last = repository.rsplit('/', 1)[-1]
    tag = last.split(':', 1)[1] if ':' in last else ''
    repository = repository.rsplit(':', 1)[0] if tag else repository
    tokens = {v for v in (image, repository, repository.split('/')[0], tag, digest, *digests) if v}
    return tokens | {v.removeprefix('sha256:') for v in tokens if v.startswith('sha256:')}


def assert_no_private_tokens(files, tokens):
    for raw in files.values():
        text = raw.decode('utf-8') if isinstance(raw, bytes) else str(raw)
        if any(token in text for token in tokens):
            raise ValueError('private image identifier remains in public export')


def observed_digest(status, private=False):
    value = status.get('imageID', '')
    if private:
        if value != 'private':
            raise ValueError('private runtime image identity must be withheld')
        return None
    match = re.fullmatch(r'(?:\S+@)?(sha256:[a-f0-9]{64})', value)
    if not match:
        raise ValueError('runtime image digest missing')
    return match[1]


def sanitize_runtime(runtime, selected, actual_image):
    """Call only after checking actual runtime identity, before public serialization."""
    result = copy.deepcopy(runtime)
    placeholder = public_image(selected)
    digests = set()
    result['launch'] = replace_serving_images(result['launch'], actual_image, placeholder)
    for key in ('pods_before', 'pods_after'):
        result[key] = replace_serving_images(result[key], actual_image, placeholder)
        for pod in result[key]['items']:
            for status in pod['status']['containerStatuses']:
                if status['name'] in SERVERS:
                    digests.add(observed_digest(status))
                    status['imageID'] = 'private'
    result['image_visibility'] = 'private'
    return result, private_tokens(actual_image, digests)
