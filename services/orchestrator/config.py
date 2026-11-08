from pathlib import Path
import json
import os
import shutil
from .resource_paths import resolve_resources

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.environ.get('SONORA_DATA', str(ROOT / 'runtime'))).resolve()
TOKEN = os.environ.get('SONORA_SESSION_TOKEN', '')
TIMEBASE = 48000
ALLOWED_ROOTS = [ROOT.resolve(), ROOT.parent.resolve(), (Path.home() / 'Downloads').resolve()]
ORIGINS = {'http://127.0.0.1:8765', 'http://localhost:8765', 'http://127.0.0.1:5173', 'http://localhost:5173', 'tauri://localhost', 'http://tauri.localhost', 'https://tauri.localhost'}
if os.environ.get('SONORA_PORT'):
    configured_port=int(os.environ['SONORA_PORT'])
    if not 1<=configured_port<=65535:raise ValueError('Invalid SONORA_PORT')
    ORIGINS.update({f'http://127.0.0.1:{configured_port}',f'http://localhost:{configured_port}'})
for folder in ('cache', 'jobs', 'exports', 'uploads', 'backups'):
    (DATA / folder).mkdir(parents=True, exist_ok=True)

def resources():
    p = ROOT / 'engine-envs/resources.json'
    return resolve_resources(json.loads(p.read_text(encoding='utf-8')), ROOT) if p.exists() else {}

def binary(name):
    configured = resources().get(name)
    return str(configured or shutil.which(name) or ROOT.parent / 'Applio' / f'{name}.exe')

def scoped_path(value, roots=None):
    p = Path(value).resolve(strict=True)
    if not p.is_file() or not any(p.is_relative_to(r) for r in (roots or ALLOWED_ROOTS)):
        raise ValueError('路径不在已配置的素材目录内，或不是普通文件。请使用文件上传。')
    return p
