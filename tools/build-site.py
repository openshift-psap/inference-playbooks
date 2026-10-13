#!/usr/bin/env python3
"""Validate, check drift, and assemble the site/catalog bundle from one checkout."""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from catalog import build_catalog, build_identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--out', type=Path, default=Path('.build/site'))
    parser.add_argument('--require-clean', action='store_true')
    parser.add_argument('--source-sha')
    args = parser.parse_args()
    repo, output = args.repo.resolve(), args.out.resolve()
    try:
        if not output.is_relative_to(repo / '.build'):
            raise ValueError('site output must be inside the ignored .build/ directory')
        if (repo / 'catalog/catalog.json').is_file():
            raise ValueError('obsolete committed catalog snapshot must not be a runtime input')
        identity = build_identity(repo)
        if args.require_clean and identity['dirty']:
            raise ValueError('publication requires a clean checkout')
        if args.source_sha and args.source_sha != identity['source_sha']:
            raise ValueError('source SHA does not match the checked-out event SHA')
        for tool, flags in [('validate.py', ['--current', '--require-converted-raw']), ('check_manifests.py', ['--all'])]:
            subprocess.run([sys.executable, str(repo / 'tools' / tool), '--repo', str(repo), *flags], check=True)
        catalog = build_catalog(repo, identity)
        files = [path for path in (repo / 'site').rglob('*') if path.is_file() and path.name != 'catalog.json']
        expected = {path.relative_to(repo / 'site').as_posix() for path in files} | {'catalog.json', 'build-provenance.json'}
        if output.exists() and any(path.relative_to(output).as_posix() not in expected for path in output.rglob('*') if path.is_file()):
            raise ValueError('output contains unexpected stale files; use a fresh .build/ directory')
        output.mkdir(parents=True, exist_ok=True)
        for path in files:
            if not path.resolve().is_relative_to(repo / 'site'):
                raise ValueError(f'{path}: site input escapes source directory')
            destination = output / path.relative_to(repo / 'site')
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        (output / 'catalog.json').write_text(json.dumps(catalog, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8')
        checksums = {path.relative_to(output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in sorted(output.rglob('*')) if path.is_file() and path.name != 'build-provenance.json'}
        (output / 'build-provenance.json').write_text(json.dumps({'build': identity, 'sha256': checksums}, indent=2, sort_keys=True) + '\n')
        print(f'Site bundle: {output}')
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f'Site build failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
