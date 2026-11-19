#!/usr/bin/env python3
"""Verify and install the architecture-specific OpenShift CLI during the image build.

This is build tooling only. It is copied into the image build, used once, and
removed from the final filesystem. The build layer that added it remains in
image history; the helper is absent from the runnable filesystem and never runs
during a benchmark.

Every source is a fixed URL with a recorded SHA-256. The build verifies the
archive checksum, the ELF machine of the extracted ``oc``, and finally runs the
binary, so a wrong-architecture or wrong-libc client cannot be built silently.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

SCHEMA_VERSION = 1
CHUNK = 1024 * 1024
ELF_HEADER_SIZE = 20  # e_ident(16) + e_type(2) + e_machine(2)
# Compatibility contract owned here, not in the source table.
ARCHITECTURES = {
    'amd64': {'machine': 'x86_64', 'elf_machine': 62},
    'arm64': {'machine': 'aarch64', 'elf_machine': 183},
}
SHA256 = re.compile(r'[a-f0-9]{64}')


class PlatformError(Exception):
    """Locally constructed failure reason; never includes downloaded content."""


def load_sources(path):
    """Load and validate the pinned per-architecture source table."""
    try:
        document = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise PlatformError('cannot read the architecture source table') from None
    if not isinstance(document, dict) or document.get('schema_version') != SCHEMA_VERSION:
        raise PlatformError('unsupported architecture source table')
    version = document.get('ocp_version')
    if not isinstance(version, str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise PlatformError('architecture source table must record the OpenShift version')
    platforms = document.get('platforms')
    if not isinstance(platforms, dict) or not platforms:
        raise PlatformError('architecture source table has no platforms')
    sources, urls, digests = {}, set(), set()
    for arch, entry in platforms.items():
        expected = ARCHITECTURES.get(arch)
        if expected is None or not isinstance(entry, dict):
            raise PlatformError(f'{arch}: unsupported architecture entry')
        if entry.get('machine') != expected['machine']:
            raise PlatformError(f'{arch}: machine name does not match the architecture')
        if entry.get('elf_machine') != expected['elf_machine']:
            raise PlatformError(f'{arch}: ELF machine does not match the architecture')
        url, digest = entry.get('url'), entry.get('sha256')
        member = entry.get('archive_member')
        if not isinstance(url, str) or not url.startswith('https://'):
            raise PlatformError(f'{arch}: source must be a fixed HTTPS URL')
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise PlatformError(f'{arch}: source must record a lowercase SHA-256')
        if not isinstance(member, str) or not member or '/' in member:
            raise PlatformError(f'{arch}: source must record a plain archive member name')
        if url in urls or digest in digests:
            raise PlatformError(f'{arch}: duplicate source pin')
        urls.add(url)
        digests.add(digest)
        sources[arch] = {'url': url, 'sha256': digest, 'archive_member': member,
                         'notes': entry.get('notes', '')}
    return {'ocp_version': version, 'platforms': sources}


def elf_machine(header):
    """Return ELF e_machine, independent of the host that reads the bytes."""
    if len(header) < ELF_HEADER_SIZE or header[:4] != b'\x7fELF':
        raise PlatformError('bundled oc is not an ELF binary')
    if header[4] != 2:
        raise PlatformError('bundled oc must be a 64-bit ELF binary')
    if header[5] not in (1, 2):
        raise PlatformError('bundled oc has an invalid ELF byte order')
    return int.from_bytes(header[18:20], 'little' if header[5] == 1 else 'big')


def verify_machine(binary, arch):
    expected = ARCHITECTURES[arch]['elf_machine']
    actual = elf_machine(binary[:ELF_HEADER_SIZE])
    if actual != expected:
        raise PlatformError(f'bundled oc ELF machine {actual} does not match {arch}')


def download(url, expected, destination):
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=120) as response, destination.open('wb') as stream:
            while chunk := response.read(CHUNK):
                digest.update(chunk)
                stream.write(chunk)
    except OSError:
        raise PlatformError('cannot download the OpenShift client archive') from None
    if digest.hexdigest() != expected:
        destination.unlink(missing_ok=True)
        raise PlatformError('OpenShift client archive checksum mismatch')


def extract(archive_path, member_name):
    try:
        with tarfile.open(archive_path, 'r:gz') as archive:
            matches = [item for item in archive.getmembers()
                       if item.name in {member_name, './' + member_name}]
            if len(matches) != 1 or not matches[0].isfile():
                raise PlatformError('OpenShift client archive member is missing or ambiguous')
            stream = archive.extractfile(matches[0])
            if stream is None:
                raise PlatformError('OpenShift client archive member is not readable')
            return stream.read()
    except (OSError, tarfile.TarError):
        raise PlatformError('cannot read the OpenShift client archive') from None


def probe(destination, version):
    try:
        result = subprocess.run([str(destination), 'version', '--client'],
                                capture_output=True, text=True, timeout=120)
    except OSError:
        raise PlatformError('bundled oc cannot run in this image userland') from None
    if result.returncode:
        raise PlatformError('bundled oc cannot run in this image userland')
    if not re.search(rf'(?m)^Client Version: {re.escape(version)}$', result.stdout):
        raise PlatformError('bundled oc does not report the declared OpenShift version')
    print(result.stdout.strip())


def install(sources, arch, image_arch, destination):
    entry = sources['platforms'].get(arch)
    if entry is None:
        raise PlatformError(f'no pinned OpenShift client for {arch}')
    if image_arch and image_arch != arch:
        raise PlatformError(f'build target {arch} does not match image architecture {image_arch}')
    destination = Path(destination)
    with tempfile.TemporaryDirectory(prefix='runner-oc-') as temporary:
        archive_path = Path(temporary) / 'oc.tar.gz'
        download(entry['url'], entry['sha256'], archive_path)
        binary = extract(archive_path, entry['archive_member'])
        verify_machine(binary, arch)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(binary)
        destination.chmod(0o755)
    probe(destination, sources['ocp_version'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    install_parser = commands.add_parser('install', help='verify and install the pinned oc')
    install_parser.add_argument('--manifest', type=Path, required=True)
    install_parser.add_argument('--arch', required=True, help='build target architecture')
    install_parser.add_argument('--image-arch', default='',
                                help='architecture reported by the image being built')
    install_parser.add_argument('--destination', type=Path, default=Path('/usr/local/bin/oc'))
    args = parser.parse_args(argv)
    try:
        sources = load_sources(args.manifest)
        install(sources, args.arch, args.image_arch, args.destination)
    except PlatformError as error:
        print(f'Architecture verification failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
