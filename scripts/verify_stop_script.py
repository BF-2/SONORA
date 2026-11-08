"""Exercise the Windows stop script on isolated helper services, never port 8765."""
from pathlib import Path
import json
import os
import socket
import sqlite3
import subprocess
import time
import uuid
import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'runtime' / 'stop-script-acceptance'
PYTHON = ROOT / '.venv/Scripts/python.exe'
PS = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT / 'scripts/stop-sonora.ps1')]


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def listening(port):
    with socket.socket() as probe:
        probe.settimeout(.25)
        return probe.connect_ex(('127.0.0.1', port)) == 0


def stop(port, data, *flags):
    result = subprocess.run([*PS, '-Port', str(port), '-DataRoot', str(data), *flags],
                            capture_output=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
    return result.returncode


def service(name, dev=True, saved_token=True):
    data = OUT / name; data.mkdir(parents=True, exist_ok=True)
    port = free_port(); token = uuid.uuid4().hex
    env = {**os.environ, 'SONORA_DATA': str(data), 'SONORA_PORT': str(port), 'SONORA_SESSION_TOKEN': token,
           'SONORA_SKIP_CATALOG_SCAN': '1', 'SONORA_DEV_SESSION': '1' if dev else '0'}
    if saved_token:
        cipher = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
            "Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1')\n[Console]::In.ReadToEnd() | ConvertTo-SecureString -AsPlainText -Force | ConvertFrom-SecureString"],
            input=token, text=True, capture_output=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
        (data / 'launcher-session.json').write_text(json.dumps({'endpoint': f'http://127.0.0.1:{port}', 'token_dpapi': cipher}), encoding='utf-8')
    log = (data / 'service.log').open('wb')
    proc = subprocess.Popen([str(PYTHON), '-m', 'services.orchestrator.app', '--port', str(port)], cwd=ROOT,
        env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(100):
        if proc.poll() is not None:
            raise RuntimeError('Test helper exited before startup.')
        if listening(port):
            return proc, log, port, data, token
        time.sleep(.1)
    raise RuntimeError('Test helper startup timeout.')


def cleanup(proc):
    if proc.poll() is None:
        subprocess.run(['taskkill.exe', '/PID', str(proc.pid), '/T', '/F'], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    proc.wait(timeout=10)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    checks = []
    assert stop(free_port(), OUT / 'not-running') == 0
    checks.append('already stopped is successful')
    for name, dev, saved in [('graceful', True, True), ('legacy', False, False), ('busy', True, True)]:
        proc, log, port, data, token = service(name + '-' + uuid.uuid4().hex[:8], dev, saved)
        try:
            assert stop(port, data, '-WhatIf') == 0 and listening(port)
            if name == 'busy':
                with sqlite3.connect(data / 'sonora.sqlite') as db:
                    job = {'id': 'stop-test-job', 'project_id': 'stop-test-project', 'idempotency_key': 'stop-test', 'state': 'running'}
                    db.execute('INSERT INTO jobs(id,project_id,idem,body) VALUES(?,?,?,?)', (job['id'], job['project_id'], job['idempotency_key'], json.dumps(job)))
                assert stop(port, data) == 2 and listening(port)
                assert stop(port, data, '-Force') == 0 and not listening(port)
                checks.append('busy service retained; explicit Force stops only its process tree')
            else:
                assert stop(port, data) == 0 and not listening(port)
                if name == 'graceful':
                    assert proc.wait(timeout=10) == 0, 'Graceful shutdown must exit normally.'
                checks.append(name + ' stop; WhatIf leaves service running')
            assert (data / 'sonora.sqlite').is_file()
        finally:
            cleanup(proc); log.close()
    port = free_port()
    proc = subprocess.Popen([str(PYTHON), '-m', 'http.server', str(port), '--bind', '127.0.0.1'], cwd=ROOT,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        for _ in range(100):
            if listening(port):break
            time.sleep(.1)
        assert stop(port, OUT / 'foreign', '-Force') == 1
        assert listening(port) and proc.poll() is None
        checks.append('foreign listener is protected even with Force')
    finally:
        cleanup(proc)
    report = {'passed': True, 'checks': checks, 'production_service_stopped': False}
    (OUT / 'verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
