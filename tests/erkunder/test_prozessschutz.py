"""Real Linux same-UID attack, isolated from the pytest process; no Docker."""

import os
import subprocess
import sys
import textwrap
from unittest.mock import Mock

import pytest

from src.erkunder import prozessschutz


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc boundary")
def test_same_uid_child_cannot_read_server_environment_or_memory():
    source = '''
import asyncio
import ctypes
import os
import subprocess
import sys
import httpx
from src.erkunder.platz import create_app

def probe(protected):
    child = """
import errno
import os
import sys
from pathlib import Path
parent, uid, protected = map(int, sys.argv[1:])
assert os.getuid() == uid
status = Path('/proc/self/status').read_text().splitlines()
for line in status:
    if line.startswith(('CapEff:', 'CapPrm:', 'CapAmb:')):
        assert int(line.split()[1], 16) == 0, line.split(':')[0]
assert 'ERKUNDER_INTERNAL_TOKEN' not in os.environ
for name in ('environ', 'mem') if protected else ('environ',):
    try:
        fd = os.open(f'/proc/{parent}/{name}', os.O_RDONLY)
    except OSError as error:
        assert protected and error.errno in (errno.EACCES, errno.EPERM), error.errno
        print(name + ': Permission denied')
    else:
        os.close(fd)
        assert not protected, name + ' unexpectedly readable'
        print(name + ': readable (control)')
"""
    result = subprocess.run(
        [sys.executable, '-c', child,
         str(os.getpid()), str(os.getuid()), str(int(protected))],
        env={'PATH': os.defpath}, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    print(result.stdout, end='')

async def main():
    # Establish a readable control; a host /proc policy must not fake success.
    libc = ctypes.CDLL(None)
    assert libc.prctl(4, 1, 0, 0, 0) == 0
    assert libc.prctl(38, 1, 0, 0, 0) == 0  # no_new_privs, as in Compose
    probe(False)
    app = create_app()
    async with app.router.lifespan_context(app):
        assert libc.prctl(3, 0, 0, 0, 0) == 0
        assert 'ERKUNDER_INTERNAL_TOKEN' not in os.environ
        probe(True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://test'
        ) as client:
            assert (await client.post('/abbrechen')).status_code == 403
            assert (await client.post('/abbrechen', headers={
                'X-Erkunder-Intern': 'synthetic-process-test'
            })).status_code == 200
        print('same UID; zero capabilities; no_new_privs; token removed; auth OK')

asyncio.run(main())
'''
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        env={"PATH": os.defpath, "ERKUNDER_INTERNAL_TOKEN": "synthetic-process-test"},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    print(result.stdout, end="")


@pytest.mark.parametrize("responses", [[-1], [0, 1], [0, -1]])
def test_prctl_failure_is_fatal(monkeypatch, responses):
    prctl = Mock(side_effect=responses)
    monkeypatch.setattr(
        prozessschutz.ctypes, "CDLL", lambda *a, **kw: Mock(prctl=prctl)
    )
    with pytest.raises((OSError, RuntimeError)):
        prozessschutz.protect_process()


@pytest.mark.asyncio
async def test_missing_token_prevents_start(monkeypatch):
    from src.erkunder.platz import create_app

    monkeypatch.delenv("ERKUNDER_INTERNAL_TOKEN", raising=False)
    app = create_app()
    with pytest.raises(RuntimeError, match="ERKUNDER_INTERNAL_TOKEN fehlt"):
        async with app.router.lifespan_context(app):
            pytest.fail("server started without credentials")
