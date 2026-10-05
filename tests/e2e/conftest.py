"""Shared HTTP preview for the catalog browser regression suite."""
import functools
import http.server
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='session')
def served(tmp_path_factory):
    directory = tmp_path_factory.mktemp('catalog-site')
    shutil.copytree(REPO / 'site', directory, dirs_exist_ok=True)
    subprocess.run([sys.executable, str(REPO / 'tools/catalog.py'), '--repo', str(REPO), '--out', str(directory / 'catalog.json')], check=True)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', json.loads((directory / 'catalog.json').read_text())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
