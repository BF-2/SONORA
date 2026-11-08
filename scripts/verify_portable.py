"""Validate an installed/extracted portable folder with its own interpreters."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
import urllib.request
import uuid
import wave


def verify(root, inference=False):
    root = Path(root).resolve()
    out = root / 'runtime/portable-verification'
    out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'HF_HUB_OFFLINE': '1',
           'TRANSFORMERS_OFFLINE': '1', 'NUMBA_CACHE_DIR': str(out / 'numba')}
    report = {'passed': False, 'checks': [], 'inference': {}, 'root': str(root)}
    interpreters = {name: root / '.tools/runtimes' / name / 'python.exe' for name in ('core', 'rvc', 'separation')}
    for name, executable in interpreters.items():
        text = subprocess.check_output([str(executable), '-I', '-c', 'import sys,json; print(json.dumps([sys.version_info[:2],sys.prefix]))'], env=env)
        version, prefix = json.loads(text)
        assert version == [3, 12] and Path(prefix).is_relative_to(root), (name, prefix)
    report['checks'].append('all three Python runtimes resolve inside extracted directory')
    sys.path.insert(0, str(root))
    from services.orchestrator.resource_paths import resolve_resources
    config = resolve_resources(json.loads((root / 'engine-envs/resources.json').read_text(encoding='utf-8')), root)
    for engine, name in [('rvc', 'rvc'), ('pitch', 'rvc'), ('separation', 'separation')]:
        command = [str(interpreters[name]), str(root / 'workers' / engine / 'run.py'), '--probe']
        result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, encoding='utf-8', timeout=90)
        (out / (engine + '-probe.log')).write_text(result.stdout + result.stderr, encoding='utf-8')
        assert result.returncode == 0, engine + ' probe failed; see ' + str(out)
        json.loads(result.stdout)
    report['checks'].append('RVC, F0 and separation worker probes passed')

    def worker(engine, name, parameters, **extra):
        folder = out / name; folder.mkdir(exist_ok=True)
        request = {'protocol_version': 1, 'job_id': name, 'input_path': str(out / 'signal.wav'),
                   'output_dir': str(folder), 'resource_config_path': str(root / 'engine-envs/resources.json'),
                   'parameters': {'device': 'cpu', 'cpu_threads': 2, **parameters}, **extra}
        (folder / 'request.json').write_text(json.dumps(request), encoding='utf-8')
        with (folder / 'worker.log').open('w', encoding='utf-8') as log:
            result = subprocess.run([str(interpreters['separation' if engine == 'separation' else 'rvc']),
                str(root / 'workers' / engine / 'run.py'), '--request', str(folder / 'request.json'),
                '--response', str(folder / 'response.json')], cwd=root, env=env, stdout=log, stderr=log, timeout=600)
        response = json.loads((folder / 'response.json').read_text(encoding='utf-8'))
        assert result.returncode == 0 and response.get('ok'), response.get('error') or name
        for output in response['outputs']:
            path = Path(output['path'])
            assert path.is_file() and path.is_relative_to(root)
            assert hashlib.sha256(path.read_bytes()).hexdigest() == output['sha256']
        print('[portable verification]', name, 'passed', flush=True)
        report['inference'][name] = {'outputs': len(response['outputs']), 'seconds': response['metadata']['elapsed_seconds']}
        return response

    if inference:
        assert len(config['models']) == 6 and len(config['separation']['models']) == 3
        with wave.open(str(out / 'signal.wav'), 'wb') as stream:
            stream.setparams((2, 2, 44100, 0, 'NONE', 'not compressed'))
            frames = bytearray()
            for i in range(52920):
                value = int(4000 * (math.sin(2*math.pi*220*i/44100) + .25*math.sin(2*math.pi*440*i/44100)))
                frames += struct.pack('<hh', value, int(value * .8))
            stream.writeframes(frames)
        pitch = worker('pitch', 'rmvpe', {'pitch_algorithm': 'rmvpe'})
        from services.orchestrator.model_catalog import ModelCatalog
        catalog = ModelCatalog(out / 'model-catalog.sqlite', resource_config=config)
        selected_model = catalog.resolve_model(config['models'][0]['id'])
        assert selected_model['index_match']['fully_validated']
        worker('rvc', 'm6-with-index', {'model_id': config['models'][0]['id'], 'index_rate': .15,
                                     'pitch_algorithm': 'rmvpe'}, pitch_path=pitch['outputs'][0]['path'], model=selected_model)
        worker('separation', 'fv4', {'separation_task': 'vocals_instrumental', 'separation_algorithm': 'single',
                                   'segment_size': 64, 'overlap_step_seconds': .25}, model=config['separation']['models'][0])
        report['checks'].append('real CPU RMVPE, M6 RVC with paired index, and FV4 separation passed')

    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    token = uuid.uuid4().hex
    server_env = {**env, 'SONORA_SESSION_TOKEN': token, 'SONORA_PORT': str(port),
                  'SONORA_DATA': str(out / 'server-data'), 'SONORA_SKIP_CATALOG_SCAN': '1'}
    with (out / 'server.log').open('w', encoding='utf-8') as log:
        server = subprocess.Popen([str(interpreters['core']), '-m', 'services.orchestrator.app', '--port', str(port)],
            cwd=root, env=server_env, stdout=log, stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            base = f'http://127.0.0.1:{port}'
            for _ in range(150):
                try:
                    with urllib.request.urlopen(base + '/api/v1/health', timeout=2) as response:
                        assert json.load(response)['protocol_version'] == 1
                    break
                except OSError:
                    assert server.poll() is None, 'server exited; see server.log'
                    time.sleep(.2)
            else:
                raise RuntimeError('portable server did not start')
            request = urllib.request.Request(base + '/api/v1/projects', headers={'Authorization': 'Bearer ' + token})
            with urllib.request.urlopen(request, timeout=10) as response:
                assert isinstance(json.load(response)['projects'], list)
            with urllib.request.urlopen(base, timeout=10) as response:
                assert 'SONORA' in response.read().decode('utf-8')
            report['checks'].append('authenticated API and prebuilt frontend served by portable core')
            stopped = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', str(root / 'scripts/stop-sonora.ps1'), '-Port', str(port), '-DataRoot', str(out / 'server-data'), '-Force'],
                cwd=root, env=env, capture_output=True, timeout=45)
            assert stopped.returncode == 0, 'portable stop script failed: ' + stopped.stderr.decode('utf-8', errors='replace')
            server.wait(timeout=15)
            report['checks'].append('portable stop launcher identifies and stops only this test service')
        finally:
            if server.poll() is None:
                server.terminate()
            server.wait(timeout=15)
    report['passed'] = True
    filename = 'verification-inference.json' if inference else 'verification.json'
    (out / filename).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--inference', action='store_true')
    args = parser.parse_args()
    verify(args.root, args.inference)
