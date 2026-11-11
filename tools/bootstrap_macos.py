#!/usr/bin/env python3
"""Bootstrap the pinned native Apple Silicon contributor runner."""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import venv
from pathlib import Path, PurePosixPath

FORGE_REVISION = 'df3c14a5eab5e8f502402f51c48765b35067fca8'
FORGE_SHA256 = 'af97c678b08cd802d2aeafed6e238a228f1de525977c7c5c80bdfcd5eec3f47e'
OC_VERSION = '4.19.0'
OC_SHA256 = 'b58c16cd2a7be6253c74f7727871e816dc8bcba1969cc3848e1727d2cb6a93ce'
PROXY_VARIABLES = ('HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy')


def check_platform():
    if sys.platform != 'darwin' or platform.machine() != 'arm64' or sys.version_info[:2] != (3, 12):
        raise ValueError('native runner requires Apple Silicon macOS and Python 3.12')


def download(url: str, path: Path, expected_sha256: str):
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=120) as response, path.open('xb') as target:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                target.write(chunk)
        if digest.hexdigest() != expected_sha256:
            raise ValueError('downloaded archive checksum mismatch')
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def install_runtime(work: Path, runner: Path):
    check_platform()
    # Forge keys tasks relative to cwd; macOS exposes temporary paths through /var symlinks.
    work = work.resolve()
    runner = runner.resolve()
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    home = work / 'home'
    home.mkdir(mode=0o700)
    runtime = work / 'venv'
    # Some macOS Python builds cannot find their stdlib after copying the interpreter.
    venv.EnvBuilder(with_pip=True, symlinks=True).create(runtime)
    python = runtime / 'bin/python'
    pip_env = {'HOME': str(home), 'PATH': str(runtime / 'bin') + os.pathsep + os.environ.get('PATH', ''),
               'TMPDIR': str(work)}
    pip_env.update({name: os.environ[name] for name in PROXY_VARIABLES if name in os.environ})
    subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
                    '--no-cache-dir', '--require-hashes', '-r', str(runner / 'requirements.lock')],
                   check=True, env=pip_env)

    forge_archive = work / 'forge.tar.gz'
    download(f'https://codeload.github.com/thameem-abbas/forge/tar.gz/{FORGE_REVISION}',
             forge_archive, FORGE_SHA256)
    forge_extract = work / 'forge'
    forge_extract.mkdir()
    root = f'forge-{FORGE_REVISION}'
    with tarfile.open(forge_archive, 'r:gz') as archive:
        members = archive.getmembers()
        if not members or {PurePosixPath(member.name).parts[0] for member in members} != {root}:
            raise ValueError('unexpected Forge archive layout')
        archive.extractall(forge_extract, filter='data')
    forge = forge_extract / root
    (forge / 'REVISION').write_text(FORGE_REVISION + '\n')
    (forge / 'bin/caliper').chmod(0o755)

    oc_archive = work / 'oc.tar.gz'
    download(f'https://mirror.openshift.com/pub/openshift-v4/clients/ocp/{OC_VERSION}/openshift-client-mac-arm64-{OC_VERSION}.tar.gz',
             oc_archive, OC_SHA256)
    oc_dir = work / 'oc'
    oc_dir.mkdir()
    with tarfile.open(oc_archive, 'r:gz') as archive:
        try:
            member = archive.getmember('oc')
        except KeyError as error:
            raise ValueError('OpenShift archive does not contain oc') from error
        if not member.isfile():
            raise ValueError('OpenShift archive does not contain a regular oc binary')
        with archive.extractfile(member) as source, (oc_dir / 'oc').open('xb') as target:
            shutil.copyfileobj(source, target)
    (oc_dir / 'oc').chmod(0o755)
    subprocess.run([str(oc_dir / 'oc'), 'version', '--client'], check=True)
    return python, forge, oc_dir


def run_child(command, environment):
    process = subprocess.Popen(command, env=environment, start_new_session=True)
    previous = {}

    def forward(signum, _frame):
        if process.poll() is None:
            process.send_signal(signum)

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous[signum] = signal.signal(signum, forward)
    try:
        return process.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kubeconfig')
    parser.add_argument('context')
    parser.add_argument('namespace')
    parser.add_argument('output')
    args = parser.parse_args(argv)
    try:
        check_platform()
        package = Path(__file__).resolve().parent
        runner = package / 'runner'
        kubeconfig = Path(args.kubeconfig).expanduser().resolve(strict=True)
        if not kubeconfig.is_file():
            raise ValueError('kubeconfig must be a regular file')
        output = Path(args.output).expanduser().resolve()
        if output.is_relative_to(package):
            raise ValueError('output directory must be outside the package')
        output.mkdir(mode=0o700)
        with tempfile.TemporaryDirectory(prefix='contributor-macos-') as temporary:
            work = Path(temporary).resolve()
            python, forge, oc_dir = install_runtime(work / 'runtime', runner)
            home = work / 'runner-home'
            home.mkdir(mode=0o700)
            private_config = work / 'kubeconfig'
            shutil.copyfile(kubeconfig, private_config)
            private_config.chmod(0o400)
            environment = {
                'HOME': str(home),
                'KUBECONFIG': str(private_config),
                'PATH': os.pathsep.join((str(python.parent), str(forge / 'bin'), str(oc_dir), os.environ.get('PATH', ''))),
                'PYTHONNOUSERSITE': '1',
                'PYTHONDONTWRITEBYTECODE': '1',
                'PYTHONPATH': os.pathsep.join((str(runner / 'tools'), str(forge))),
                'TMPDIR': str(work),
            }
            environment.update({name: os.environ[name] for name in PROXY_VARIABLES if name in os.environ})
            return run_child([str(python), str(runner / 'tools/run_contributor.py'),
                              '--request', str(package / 'request.json'),
                              '--inputs', str(package / 'playbooks'), '--output', str(output),
                              '--forge', str(forge), '--namespace', args.namespace, '--context', args.context],
                             environment)
    except (OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as error:
        print(f'Cannot start native macOS contributor runner: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
