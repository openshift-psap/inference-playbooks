#!/usr/bin/env python3
"""Verify published runner images and record candidate multi-architecture release data.

This is host-side publication tooling, not part of a benchmark run. It never
invents a digest: every recorded value is derived from bytes returned by the
registry and is checked against the exact scanned image config. Writing the
candidate ``release.json`` here does not change the default release; a reviewed
change to ``benchmarks/runner/release.json`` remains a separate maintainer step.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from contributor_bundle import FORGE_REVISION, IMAGE, json_bytes, json_load

VERSION = re.compile(r'v[0-9]+\.[0-9]+(?:\.[0-9]+)?')
REVISION = re.compile(r'[a-f0-9]{40}')
DIGEST = re.compile(r'sha256:[a-f0-9]{64}')
REPOSITORY = re.compile(r'[^\s/@]+(?:/[^\s/@]+)+')
ARCHITECTURES = ('amd64', 'arm64')
MANIFEST_TYPES = frozenset({
    'application/vnd.oci.image.manifest.v1+json',
    'application/vnd.docker.distribution.manifest.v2+json',
})
INDEX_TYPES = frozenset({
    'application/vnd.oci.image.index.v1+json',
    'application/vnd.docker.distribution.manifest.list.v2+json',
})
PLATFORM_RECORD = frozenset({
    'schema_version', 'arch', 'executed_image_id', 'config_digest',
    'manifest_digest', 'layer_count', 'forge_revision',
})
# A registry that positively reports an absent tag lets publication proceed.
# Anything else (authorization, network, unknown errors) must fail closed.
ABSENT_TAG_MESSAGES = ('not found', 'manifest unknown', 'no such manifest', 'name unknown')


class ReleaseError(ValueError):
    """Locally constructed failure reason; never includes registry content."""


def digest_of(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def repository_of(reference: str) -> str:
    # Digest-pinned references recorded here never carry a tag.
    return reference.split('@', 1)[0]


def validate_release(document) -> dict:
    """Validate release metadata and return its normalized fields.

    Schema version 1 records the single published AMD64 image and remains valid.
    Schema version 2 additionally records the index and one child manifest per
    architecture. Both require a consistent tag, digest, Forge pin, and source
    revision.
    """
    if (not isinstance(document, dict) or type(document.get('schema_version')) is not int
            or document['schema_version'] not in (1, 2)):
        raise ReleaseError('runner release metadata is unsupported')
    if document.get('forge_revision') != FORGE_REVISION:
        raise ReleaseError('runner release uses a different Forge revision')
    image, version = document.get('image'), document.get('version')
    source_revision = document.get('source_revision')
    if not isinstance(image, str) or not re.fullmatch(IMAGE, image):
        raise ReleaseError('runner release must record a digest-pinned image')
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ReleaseError('runner release must record a version')
    if document.get('tag') != image.split('@', 1)[0] + ':' + version:
        raise ReleaseError('runner release must record a consistent tag, digest, and version')
    if not isinstance(source_revision, str) or not REVISION.fullmatch(source_revision):
        raise ReleaseError('runner release must record a 40-character source revision')
    result = {'schema_version': document['schema_version'], 'image': image, 'version': version,
              'tag': document['tag'], 'forge_revision': FORGE_REVISION,
              'source_revision': source_revision, 'platforms': {}}
    if document['schema_version'] == 1:
        return result
    platforms = document.get('platforms')
    if not isinstance(platforms, dict) or set(platforms) != set(ARCHITECTURES):
        raise ReleaseError('runner release must record amd64 and arm64 platform images')
    index_repository, index_digest = image.split('@', 1)
    seen = set()
    for arch in ARCHITECTURES:
        entry = platforms.get(arch)
        child = entry.get('image') if isinstance(entry, dict) else None
        if not isinstance(child, str) or not re.fullmatch(IMAGE, child):
            raise ReleaseError(f'runner release {arch} platform must record a digest-pinned image')
        if repository_of(child) != index_repository:
            raise ReleaseError(f'runner release {arch} platform must use the index repository')
        digest = child.split('@', 1)[1]
        if digest == index_digest or digest in seen:
            raise ReleaseError(f'runner release {arch} platform digest must be distinct')
        seen.add(digest)
        result['platforms'][arch] = {'image': child}
    return result


def verify_published(manifest: bytes, config_digest: str, executed_image_id: str,
                     arch: str, forge_revision: str) -> dict:
    """Check a registry manifest identifies the exact scanned image config.

    ``executed_image_id`` is the frozen local identity used for scanning and
    smoke execution. Docker's containerd store can report an index digest there,
    so it is recorded separately and never compared to ``config_digest``. The
    config digest is derived from the exported image and must equal the config
    reference in the pushed single-platform manifest.
    """
    if arch not in ARCHITECTURES:
        raise ReleaseError('published image architecture is unsupported')
    for label, value in (('config digest', config_digest),
                         ('executed image ID', executed_image_id)):
        if not isinstance(value, str) or not DIGEST.fullmatch(value):
            raise ReleaseError(f'scanned image {label} must be a SHA-256 digest')
    if forge_revision != FORGE_REVISION:
        raise ReleaseError('published image records a different Forge revision')
    try:
        document = json_load(manifest)
    except ValueError:
        raise ReleaseError('published image manifest is not valid JSON') from None
    if isinstance(document, dict) and document.get('mediaType') in INDEX_TYPES:
        raise ReleaseError('published image is a multi-platform index; build with '
                           '--provenance=false --sbom=false so the pushed manifest is one image')
    if not isinstance(document, dict) or document.get('schemaVersion') != 2 \
            or document.get('mediaType') not in MANIFEST_TYPES:
        raise ReleaseError('published image is not a single-platform manifest')
    config = document.get('config')
    layers = document.get('layers')
    if not isinstance(config, dict) or config.get('digest') != config_digest:
        raise ReleaseError('published image manifest does not reference the scanned image config')
    if not isinstance(layers, list) or not layers:
        raise ReleaseError('published image manifest records no layers')
    for layer in layers:
        if not isinstance(layer, dict) or not isinstance(layer.get('digest'), str) \
                or not DIGEST.fullmatch(layer['digest']):
            raise ReleaseError('published image manifest has an invalid layer digest')
    return {'schema_version': 1, 'arch': arch, 'executed_image_id': executed_image_id,
            'config_digest': config_digest,
            'manifest_digest': digest_of(manifest), 'layer_count': len(layers),
            'forge_revision': forge_revision}


def validate_platform_record(document) -> dict:
    if (not isinstance(document, dict) or set(document) != PLATFORM_RECORD
            or type(document.get('schema_version')) is not int or document['schema_version'] != 1
            or document.get('arch') not in ARCHITECTURES
            or document.get('forge_revision') != FORGE_REVISION
            or not isinstance(document.get('executed_image_id'), str)
            or not DIGEST.fullmatch(document['executed_image_id'])
            or not isinstance(document.get('config_digest'), str)
            or not DIGEST.fullmatch(document['config_digest'])
            or not isinstance(document.get('manifest_digest'), str)
            or not DIGEST.fullmatch(document['manifest_digest'])
            or type(document.get('layer_count')) is not int or document['layer_count'] < 1):
        raise ReleaseError('platform publication record is inconsistent')
    return document


def verify_index(index: bytes, index_digest: str, platforms: dict) -> dict:
    """Check the published index bytes select exactly the recorded child manifests."""
    if digest_of(index) != index_digest:
        raise ReleaseError('published index bytes do not match the recorded index digest')
    try:
        document = json_load(index)
    except ValueError:
        raise ReleaseError('published index is not valid JSON') from None
    if not isinstance(document, dict) or document.get('schemaVersion') != 2 \
            or document.get('mediaType') not in INDEX_TYPES:
        raise ReleaseError('published image is not a multi-platform index')
    manifests = document.get('manifests')
    if not isinstance(manifests, list) or len(manifests) != len(ARCHITECTURES):
        raise ReleaseError('published index must contain exactly the supported platforms')
    seen = {}
    for entry in manifests:
        platform = entry.get('platform') if isinstance(entry, dict) else None
        if not isinstance(platform, dict) or platform.get('os') != 'linux' \
                or platform.get('architecture') not in ARCHITECTURES:
            raise ReleaseError('published index contains an unsupported platform')
        arch = platform['architecture']
        if arch in seen:
            raise ReleaseError('published index repeats a platform')
        if entry.get('mediaType') not in MANIFEST_TYPES:
            raise ReleaseError('published index does not reference image manifests')
        if entry.get('digest') != platforms[arch]['manifest_digest']:
            raise ReleaseError(f'published index {arch} digest differs from the recorded manifest')
        seen[arch] = entry['digest']
    if set(seen) != set(ARCHITECTURES):
        raise ReleaseError('published index is missing an architecture')
    return seen


def verify_child(child: bytes, record: dict) -> None:
    """Check a child manifest matches both its index digest and scanned config."""
    if digest_of(child) != record['manifest_digest']:
        raise ReleaseError(f"published {record['arch']} manifest bytes differ from the index digest")
    try:
        document = json_load(child)
    except ValueError:
        raise ReleaseError('published child manifest is not valid JSON') from None
    if not isinstance(document, dict) or document.get('schemaVersion') != 2 \
            or document.get('mediaType') not in MANIFEST_TYPES:
        raise ReleaseError('published child is not a single-platform manifest')
    config = document.get('config')
    if not isinstance(config, dict) or config.get('digest') != record['config_digest']:
        raise ReleaseError('published child manifest does not reference the scanned image config')


def release_document(repository: str, version: str, index_digest: str, platforms: dict,
                     source_revision: str) -> dict:
    """Build candidate schema-version-2 metadata from verified values only."""
    if not isinstance(repository, str) or not re.fullmatch(REPOSITORY, repository) or '@' in repository:
        raise ReleaseError('runner release repository must be a registry path without a tag or digest')
    document = {
        'schema_version': 2,
        'version': version,
        'tag': repository + ':' + version,
        'image': repository + '@' + index_digest,
        'forge_revision': FORGE_REVISION,
        'source_revision': source_revision,
        'platforms': {arch: {'image': repository + '@' + platforms[arch]['manifest_digest']}
                      for arch in ARCHITECTURES},
    }
    validate_release(document)
    return document


def _run(argv, run):
    result = run(argv, capture_output=True)
    if result.returncode:
        raise ReleaseError(f'container tool failed: {" ".join(argv[:3])}')
    return result.stdout


def _message(result) -> str:
    parts = []
    for value in (getattr(result, 'stderr', None), getattr(result, 'stdout', None)):
        if isinstance(value, bytes):
            parts.append(value.decode('utf-8', 'replace'))
        elif isinstance(value, str):
            parts.append(value)
    return '\n'.join(parts).lower()


def guard_tag(reference: str, engine: str = 'docker', run=subprocess.run) -> None:
    """Fail unless the registry positively reports that the tag is absent.

    Authorization, network, and unrecognized failures are not evidence that a
    tag is free, so they abort publication instead of overwriting silently.
    """
    repository, _, tag = reference.rpartition(':')
    if not repository or not tag or '/' in tag or '@' in reference:
        raise ReleaseError('tag guard requires a tagged reference without a digest')
    result = run([engine, 'buildx', 'imagetools', 'inspect', reference],
                 capture_output=True)
    if result.returncode == 0:
        raise ReleaseError('refusing to overwrite an existing tag')
    if not any(token in _message(result) for token in ABSENT_TAG_MESSAGES):
        raise ReleaseError('cannot confirm the tag is absent: registry check failed; '
                           'confirm the registry state before retrying')


def load_platforms(platforms_dir: Path) -> dict:
    records = {}
    for arch in ARCHITECTURES:
        path = platforms_dir / f'platform-{arch}' / 'digests.json'
        try:
            document = json_load(path.read_bytes())
        except (OSError, ValueError):
            raise ReleaseError(f'missing or invalid platform publication record for {arch}') from None
        record = validate_platform_record(document)
        if record['arch'] != arch:
            raise ReleaseError(f'platform publication record does not identify {arch}')
        records[arch] = record
    return records


def assemble(repository: str, version: str, platforms_dir: Path, source_revision: str,
             output: Path, engine: str = 'docker', run=subprocess.run) -> dict:
    """Publish the index from verified child manifests and record candidate metadata.

    The child images must already have been published by the caller. This step
    only assembles an index that references those exact manifest digests, then
    verifies the published bytes. It never modifies the default release file.
    """
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ReleaseError('runner release version must look like v0.2.0')
    if not isinstance(source_revision, str) or not REVISION.fullmatch(source_revision):
        raise ReleaseError('runner release source revision must be a 40-character Git SHA')
    platforms = load_platforms(platforms_dir)
    tag = repository + ':' + version
    guard_tag(tag, engine, run)
    children = [repository + '@' + platforms[arch]['manifest_digest'] for arch in ARCHITECTURES]
    _run([engine, 'buildx', 'imagetools', 'create', '-t', tag, *children], run)
    index = _run([engine, 'buildx', 'imagetools', 'inspect', tag, '--raw'], run)
    index_digest = digest_of(index)
    verify_index(index, index_digest, platforms)
    child_documents = {}
    for arch in ARCHITECTURES:
        child = _run([engine, 'buildx', 'imagetools', 'inspect',
                      repository + '@' + platforms[arch]['manifest_digest'], '--raw'], run)
        verify_child(child, platforms[arch])
        child_documents[arch] = child
    document = release_document(repository, version, index_digest, platforms, source_revision)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'release.json').write_bytes(json_bytes(document))
    (output / 'index.json').write_bytes(index)
    for arch in ARCHITECTURES:
        (output / f'child-{arch}.json').write_bytes(child_documents[arch])
        (output / f'platform-{arch}.json').write_bytes(json_bytes(platforms[arch]))
    return {'release': document, 'index_digest': index_digest,
            'platform_digests': {arch: platforms[arch]['manifest_digest'] for arch in ARCHITECTURES}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)

    published = commands.add_parser('verify-published', help='verify one pushed manifest against the scanned image')
    published.add_argument('--manifest', type=Path, required=True)
    published.add_argument('--config-digest', required=True,
                           help='config digest derived from the exported scanned image')
    published.add_argument('--executed-image-id', required=True,
                           help='frozen local image ID used for scanning and smoke execution')
    published.add_argument('--arch', required=True, choices=ARCHITECTURES)
    published.add_argument('--forge-revision', required=True,
                           help='Forge revision label read from the scanned image')
    published.add_argument('--output', type=Path, required=True)

    guard = commands.add_parser('guard-tag', help='fail unless a tagged reference is confirmed absent')
    guard.add_argument('--reference', required=True, help='tagged registry reference')
    guard.add_argument('--engine', default='docker')

    assembly = commands.add_parser('assemble', help='assemble and verify the published multi-architecture index')
    assembly.add_argument('--repository', required=True, help='registry path without tag or digest')
    assembly.add_argument('--version', required=True)
    assembly.add_argument('--platforms', type=Path, required=True,
                          help='directory containing platform-<arch>/digests.json')
    assembly.add_argument('--source-revision', required=True)
    assembly.add_argument('--engine', default='docker')
    assembly.add_argument('--output', type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == 'verify-published':
            record = verify_published(args.manifest.read_bytes(), args.config_digest,
                                      args.executed_image_id, args.arch,
                                      args.forge_revision)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(json_bytes(record))
            print(json.dumps(record, sort_keys=True))
        elif args.command == 'guard-tag':
            guard_tag(args.reference, engine=args.engine)
            print(f'Tag is available: {args.reference}')
        else:
            summary = assemble(args.repository, args.version, args.platforms,
                               args.source_revision, args.output, engine=args.engine)
            print(json.dumps(summary, indent=2, sort_keys=True))
    except (OSError, ReleaseError) as error:
        print(f'Runner release verification failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
