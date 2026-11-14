"""Portable installs must verify their own resources even with a legacy ledger present."""
import hashlib
import importlib
import json
from pathlib import Path
import shutil
import sys
import threading

import pytest

from scripts.portable_setup import Installer
from services.orchestrator.resource_integrity import verify_applio_resources
from services.orchestrator.resource_paths import resolve_resources


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


@pytest.fixture
def portable_install(tmp_path):
    root = tmp_path / 'original'
    files = []
    for relative, content in [('LICENSE', b'engine license'), ('rvc/configs/config.py', b'# engine code\n')]:
        path = root / 'resources/applio' / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        files.append({'path': relative, 'sha256': hashlib.sha256(content).hexdigest()})
    snapshot_path = root / 'vendor/applio/snapshot.json'
    write_json(snapshot_path, {'revision': 'test-revision', 'files': files})
    model_path = root / 'resources/applio/rvc/models/predictors/rmvpe.pt'
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b'unit model bytes')
    manifest = {'files': [{'path': model_path.relative_to(root).as_posix(), 'groups': ['rvc-base'],
                           'sha256': hashlib.sha256(model_path.read_bytes()).hexdigest()}]}
    write_json(root / 'engine-envs/portable-assets.json', manifest)
    # A clean source checkout includes this developer-machine ledger.
    write_json(root / 'engine-envs/resource-links.json', {
        'files': [{'path': 'E:\\voice\\aponyvoice\\resources\\applio\\LICENSE', 'sha256': '0' * 64}]})
    config = {'portable_version': 1, 'applio': {'root': './resources/applio',
              'source_hash': hashlib.sha256(snapshot_path.read_bytes()).hexdigest()}}
    return root, config


def test_portable_integrity_follows_moved_install_and_ignores_legacy_ledger(portable_install, tmp_path):
    root, config = portable_install
    moved = tmp_path / '移动目录 with spaces'
    shutil.move(root, moved)
    verify_applio_resources(config, moved)
    assert config['applio']['root'] == './resources/applio'


@pytest.mark.parametrize('relative', [
    'resources/applio/LICENSE',
    'resources/applio/rvc/models/predictors/rmvpe.pt',
    'vendor/applio/snapshot.json',
])
@pytest.mark.parametrize('change', ['missing', 'modified'])
def test_portable_integrity_reports_missing_and_changed_files(portable_install, relative, change):
    root, config = portable_install
    path = root / relative
    if change == 'missing':
        path.unlink()
    else:
        path.write_bytes(b'changed resource')
    with pytest.raises(ValueError, match='缺少推理资源' if change == 'missing' else '推理资源校验未通过') as error:
        verify_applio_resources(config, root)
    assert str(path) in str(error.value)


@pytest.mark.parametrize('missing_license', [False, True])
def test_environment_only_preparation_checks_code_before_reporting_success(portable_install, monkeypatch, missing_license):
    root, config = portable_install
    (root / 'resources/applio/rvc/models/predictors/rmvpe.pt').unlink()
    verify_applio_resources(config, root, check_models=False)
    manifest = {'separators': [], 'voices': [], 'mirrors': {}}
    installer = Installer(root, manifest, sys.executable, sys.executable, backend='cpu')
    monkeypatch.setattr('scripts.portable_setup.subprocess.check_output', lambda *args, **kwargs: b'{}')
    target = root / 'engine-envs/resources.json'
    if missing_license:
        (root / 'resources/applio/LICENSE').unlink()
        with pytest.raises(ValueError, match='缺少推理资源.*LICENSE'):
            installer.configure()
        assert not target.exists()
    else:
        installer.configure()
        saved = json.loads(target.read_text(encoding='utf-8'))
        assert saved['portable_version'] == 1 and saved['models'] == []


def test_legacy_integrity_keeps_validating_registered_absolute_paths(tmp_path):
    path = tmp_path / 'shared-engine/LICENSE'
    path.parent.mkdir()
    path.write_bytes(b'legacy engine')
    write_json(tmp_path / 'engine-envs/resource-links.json', {
        'files': [{'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]})
    verify_applio_resources({}, tmp_path)
    path.write_bytes(b'changed engine')
    with pytest.raises(ValueError, match='推理资源校验未通过'):
        verify_applio_resources({}, tmp_path)
    path.unlink()
    with pytest.raises(ValueError, match='缺少推理资源'):
        verify_applio_resources({}, tmp_path)


def test_pitch_scheduler_reaches_worker_with_portable_resources_and_legacy_ledger(portable_install, monkeypatch):
    from tests.test_core import fixture_project, scheduler, media
    from tests.test_review_regressions import _job
    from tests.test_specialist_integration import _pitch

    root, config = portable_install
    module = importlib.import_module('services.orchestrator.scheduler')
    script = root / 'workers/pitch/run.py'
    script.parent.mkdir(parents=True)
    script.write_text('# pitch protocol fixture\n', encoding='utf-8')
    config['pitch'] = {'python': sys.executable}
    resolved = resolve_resources(config, root)
    project, source = fixture_project()
    job = _job(project, operation='pitch', parameters={'f0_min': 73})
    calls = []

    def run_worker(args, cancel, log, env, cwd):
        calls.append(args)
        request = json.loads(Path(args[args.index('--request') + 1]).read_text(encoding='utf-8'))
        path, raw, _ = _pitch(source, request['parameters'], Path(request['output_dir']) / 'fixture.json')
        write_json(Path(args[args.index('--response') + 1]), {
            'ok': True, 'outputs': [{'path': str(path), 'role': 'pitch', 'processing_kind': 'dsp',
                'sample_rate': 16000, 'sample_count': raw['sample_count'], 'channels': 1}],
            'metadata': {'analysis_parameters': raw['analysis_parameters']}})

    monkeypatch.setattr(module, 'ROOT', root)
    monkeypatch.setattr(module, 'resources', lambda: resolved)
    monkeypatch.setattr(media, 'run', run_worker)
    outputs = scheduler.engine('pitch', job, source, threading.Event())
    assert len(calls) == 1
    assert len(outputs) == 1 and outputs[0]['stem_role'] == 'pitch'
    assert scheduler.pitch_matches(outputs[0], job['parameters'])
