#!/usr/bin/env python3
"""Package two verified static bundles: main at / and staging at /staging/."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil


def verify_bundle(root):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f"missing site bundle: {root}")
    if any(path.is_symlink() for path in root.rglob('*')):
        raise ValueError(f"bundle contains symlinks: {root}")
    provenance = json.loads((root / 'build-provenance.json').read_text())
    build = provenance['build']
    if build.get('dirty') is not False or not re.fullmatch(r'[a-f0-9]{40}', build.get('source_sha') or ''):
        raise ValueError(f"bundle requires a clean committed source revision: {root}")
    checksums = provenance['sha256']
    if not isinstance(checksums, dict) or not {'index.html', 'catalog.json'} <= checksums.keys():
        raise ValueError(f"incomplete bundle manifest: {root}")
    for name, expected in checksums.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or str(relative) != name or '\\' in name:
            raise ValueError(f"unsafe bundle path: {name}")
        path = root / name
        if not path.is_file() or not re.fullmatch(r'[a-f0-9]{64}', expected or ''):
            raise ValueError(f"missing file or invalid checksum: {name}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"bundle checksum mismatch: {name}")
    names = set(checksums) | {'build-provenance.json'}
    actual = {path.relative_to(root).as_posix() for path in root.rglob('*') if path.is_file()}
    if actual != names:
        raise ValueError(f"bundle has unexpected files: {root}")
    return root, names, build


def assemble(production, staging, output):
    production, production_files, production_build = verify_bundle(production)
    staging, staging_files, staging_build = verify_bundle(staging)
    output = Path(output).resolve()
    for source in (production, staging):
        if output.is_relative_to(source) or source.is_relative_to(output):
            raise ValueError('output must not overlap either source bundle')
    if any(name.split('/')[0] in {'staging', 'pages-provenance.json'} for name in production_files):
        raise ValueError('production bundle uses a reserved staging packaging path')
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError('use a fresh or empty output directory; stale files are not retained')
    output.mkdir(parents=True, exist_ok=True)
    for root, names, destination in ((production, production_files, output), (staging, staging_files, output / 'staging')):
        for name in sorted(names):
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / name, target)
    (output / 'pages-provenance.json').write_text(json.dumps({
        'schema_version': 1,
        'production': {'path': '/', 'build': production_build},
        'staging': {'path': '/staging/', 'build': staging_build},
    }, indent=2, sort_keys=True) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--production', required=True, type=Path)
    parser.add_argument('--staging', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    try:
        assemble(args.production, args.staging, args.out)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'Pages assembly failed: {error}\n')
    print(f'Combined Pages bundle: {args.out}')


if __name__ == '__main__':
    main()
