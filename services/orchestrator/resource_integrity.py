"""Verify the resources belonging to the active installation."""
import json
from pathlib import Path

from workers.common import sha256
from .resource_paths import resolve_resources


def verify_resource_file(path, expected_hash):
    path = Path(path)
    if not path.is_file():
        raise ValueError(f'缺少推理资源：{path}。请重新运行“一键准备声序.bat”。')
    if sha256(path) != expected_hash:
        raise ValueError(f'推理资源校验未通过：{path}。请重新准备资源后重试。')


def verify_applio_resources(config, root, *, check_models=True):
    root = Path(root).resolve()
    if config.get('portable_version'):
        config = resolve_resources(config, root)
        snapshot_path = root / 'vendor/applio/snapshot.json'
        verify_resource_file(snapshot_path, config['applio']['source_hash'])
        snapshot = json.loads(snapshot_path.read_text(encoding='utf-8'))
        engine_root = Path(config['applio']['root'])
        files = [{**entry, 'path': engine_root / entry['path']} for entry in snapshot['files']]
        if check_models:
            manifest = json.loads((root / 'engine-envs/portable-assets.json').read_text(encoding='utf-8'))
            files.extend({**entry, 'path': root / entry['path']} for entry in manifest['files']
                         if 'rvc-base' in entry['groups'])
    else:
        ledger_path = root / 'engine-envs/resource-links.json'
        if not ledger_path.is_file():
            return
        files = json.loads(ledger_path.read_text(encoding='utf-8'))['files']

    for entry in files:
        verify_resource_file(entry['path'], entry['sha256'])
