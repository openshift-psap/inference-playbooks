#!/usr/bin/env python3
"""Fail-closed local secret scan of a built image; never publish or print matches."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

# Aqua Trivy 0.76.1; update deliberately and rerun the clean/canary image proofs.
SCANNER_IMAGE = 'docker.io/aquasec/trivy@sha256:790fd66d1697395d8693d1acaa1ea702406a20f109b6fc7bac8db623eca6c06c'


class ScanFailure(Exception):
    """Sanitized, locally constructed failure reason; never scanner output."""


def run(argv, *, timeout=600):
    # Scanner output can contain credential excerpts. Never echo it, even on error.
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def scan(image, engine):
    inspected = run([engine, 'image', 'inspect', '--format', '{{.Id}}', image])
    if inspected.returncode:
        raise ScanFailure('cannot inspect the local image')
    image_id = 'sha256:' + inspected.stdout.strip().removeprefix('sha256:')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', image_id):
        raise ScanFailure('invalid local image identity')
    with tempfile.TemporaryDirectory(prefix='runner-secret-scan-') as temporary:
        root = Path(temporary)
        inputs, reports = root / 'input', root / 'reports'
        inputs.mkdir(mode=0o700)
        reports.mkdir(mode=0o700)
        # Freeze the inspected image, not a tag that might move while scanning.
        saved = run([engine, 'save', '--output', str(inputs / 'image.tar'), image_id])
        if saved.returncode:
            raise ScanFailure('cannot export the inspected image')
        command = [engine, 'run', '--rm', '--network', 'none', '--read-only',
                   '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                   '--user', f'{os.getuid()}:{os.getgid()}']
        if Path(engine).name == 'podman':
            command.append('--userns=keep-id')
        command += ['--tmpfs', '/tmp:rw,nosuid,nodev,size=512m', '--workdir', '/tmp',
                    '-e', 'TRIVY_CACHE_DIR=/tmp/trivy-cache',
                    '-v', f'{inputs}:/input:ro,Z', '-v', f'{reports}:/reports:Z',
                    SCANNER_IMAGE, 'image', '--input', '/input/image.tar',
                    '--scanners', 'secret', '--image-config-scanners', 'secret',
                    '--secret-config', '', '--ignorefile', '/dev/null',
                    '--disable-telemetry', '--offline-scan', '--skip-version-check',
                    '--skip-db-update', '--skip-java-db-update', '--quiet',
                    '--timeout', '5m', '--exit-code', '1', '--format', 'json',
                    '--output', '/reports/report.json']
        result = run(command)
        report = reports / 'report.json'
        if not report.is_file() or report.stat().st_size > 32 * 1024 * 1024:
            raise ScanFailure('scanner did not produce a bounded report')
        document = json.loads(report.read_text())
        if (not isinstance(document, dict) or document.get('SchemaVersion') != 2
                or document.get('ArtifactType') != 'container_image'
                or document.get('Metadata', {}).get('ImageID') != image_id):
            raise ScanFailure('scanner report does not identify the inspected image')
        findings = sum(len(item.get('Secrets') or []) for item in document.get('Results') or [])
        if findings:
            raise ScanFailure(f'secret scan blocked the image: {findings} finding(s); matches suppressed')
        if result.returncode:
            raise ScanFailure('scanner failed; image is not approved')
    print(f'Secret scan passed for {image_id} using pinned Trivy 0.76.1.')
    print('Built-in rules/allowlists apply; this is not a guarantee of credential absence.')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', help='already-built local image reference')
    args = parser.parse_args(argv)
    engine = os.environ.get('CONTAINER_ENGINE') or shutil.which('podman') or shutil.which('docker')
    if not engine:
        parser.error('Podman or Docker is required')
    try:
        return scan(args.image, engine)
    except ScanFailure as error:
        print(f'Secret scan FAILED: {error}. No publication performed.')
        return 1
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        # Do not include exceptions containing scanner output or credential values.
        print('Secret scan failed or found secrets. Image is NOT approved; no publication performed.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
