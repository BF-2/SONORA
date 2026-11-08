import hashlib
import http.server
import importlib.util
import json
from pathlib import Path
import threading
import zipfile

import pytest

from scripts.portable_setup import Downloader, child, extract_zip, select_files, source_urls
from services.orchestrator.resource_paths import resolve_resources

ROOT = Path(__file__).resolve().parents[1]
DATA = b'SONORA verified model bytes\n' * 128


@pytest.fixture
def origin():
    state = {'ranges': [], 'short': True, 'gets': 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            state['gets'] += 1
            if self.path == '/missing':
                self.send_error(503); return
            payload = b'corrupt'.ljust(len(DATA), b'!') if self.path == '/bad' else DATA
            offset = int(self.headers.get('Range', 'bytes=0-').split('=')[1].split('-')[0])
            state['ranges'].append(offset)
            if self.path == '/ignore':
                offset = 0
            self.send_response(206 if offset else 200)
            self.send_header('Content-Length', str(len(payload) - offset))
            if offset:
                self.send_header('Content-Range', f'bytes {offset}-{len(payload)-1}/{len(payload)}')
            self.end_headers()
            if self.path == '/short' and state['short']:
                state['short'] = False
                self.wfile.write(payload[:len(payload)//2])
            else:
                self.wfile.write(payload[offset:])

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', state
    finally:
        server.shutdown(); server.server_close(); thread.join()


def entry(url):
    return {'path': 'resources/model.pth', 'url': url, 'sha256': hashlib.sha256(DATA).hexdigest(), 'bytes': len(DATA)}


def downloader(root):
    return Downloader(root, {'github': [], 'huggingface': []}, preference='origin', report=lambda _: None)


def test_failed_origin_switches_mirror_and_reuses_verified_file(tmp_path, origin):
    url, state = origin
    path = downloader(tmp_path).fetch(entry(url + '/missing'), [url + '/missing', url + '/ok'])
    assert path.read_bytes() == DATA
    gets = state['gets']
    assert downloader(tmp_path).fetch(entry(url + '/missing')) == path
    assert state['gets'] == gets


def test_short_connection_resumes_at_exact_offset(tmp_path, origin):
    url, state = origin
    path = downloader(tmp_path).fetch(entry(url + '/short'))
    assert path.read_bytes() == DATA
    assert state['ranges'] == [0, len(DATA)//2]


def test_server_ignoring_range_restarts_partial(tmp_path, origin):
    url, _ = origin
    p = tmp_path / 'resources/model.pth.part'; p.parent.mkdir(); p.write_bytes(DATA[:80])
    assert downloader(tmp_path).fetch(entry(url + '/ignore')).read_bytes() == DATA


def test_corrupt_complete_partial_and_bad_mirror_are_quarantined(tmp_path, origin):
    url, _ = origin
    p = tmp_path / 'resources/model.pth.part'; p.parent.mkdir(); p.write_bytes(b'!' * len(DATA))
    path = downloader(tmp_path).fetch(entry(url + '/bad'), [url + '/bad', url + '/ok'])
    assert path.read_bytes() == DATA
    assert len(list(p.parent.glob('*.bad-*'))) >= 2


def test_existing_modified_model_is_preserved(tmp_path, origin):
    url, state = origin
    p = tmp_path / 'resources/model.pth'; p.parent.mkdir(); p.write_bytes(b'user model')
    with pytest.raises(ValueError):
        downloader(tmp_path).fetch(entry(url + '/ok'))
    assert p.read_bytes() == b'user model' and state['gets'] == 0


def test_zip_and_download_paths_cannot_escape(tmp_path):
    with pytest.raises(ValueError):
        child(tmp_path, '../escape')
    with pytest.raises(ValueError):
        child(tmp_path, 'C:/outside')
    archive = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('../escape', b'bad')
    with pytest.raises(ValueError):
        extract_zip(archive, tmp_path / 'extract')
    assert not (tmp_path / 'escape').exists()


def test_all_six_m6_pairs_and_base_ensemble_are_planned():
    manifest = json.loads((ROOT / 'engine-envs/portable-assets.json').read_text(encoding='utf-8'))
    m6 = select_files(manifest, 'm6')
    assert len([x for x in m6 if x['path'].endswith('.pth')]) == 6
    assert len([x for x in m6 if x['path'].endswith('.index')]) == 6
    assert len([x for x in select_files(manifest, 'base') if x['path'].endswith('.ckpt')]) == 3
    assert select_files(manifest, 'none') == []
    assert len({x['path'] for x in manifest['files']}) == len(manifest['files'])
    for file in manifest['files']:
        assert len(file['sha256']) == 64 and file['bytes'] > 0
    for voice in manifest['voices']:
        assert voice['inference_profile']['model_sha256'] == voice['model_hash']


def test_portable_paths_follow_new_root_without_mutating_template(tmp_path):
    template = {'portable_version': 1, 'models': [{'path': './resources/a.pth', 'index_path': './resources/a.index'}],
                'catalog': {'roots': ['./resources/models']}, 'environment_lock_path': './.tools/lock.json'}
    first = resolve_resources(template, tmp_path / 'first')
    moved = resolve_resources(template, tmp_path / '移动目录 with spaces')
    assert first['models'][0]['path'] != moved['models'][0]['path']
    assert moved['catalog']['roots'] == [str((tmp_path / '移动目录 with spaces/resources/models').resolve())]
    assert template['models'][0]['path'].startswith('./')
    with pytest.raises(ValueError):
        resolve_resources({'portable_version': 1, 'path': './../outside'}, tmp_path)


def test_mirrors_keep_pinned_revision_and_index_path():
    url = 'https://huggingface.co/owner/repo/resolve/commit123/folder/model.index'
    assert source_urls(url, {'huggingface': ['https://hf-mirror.com'], 'github': []}) == [
        url, 'https://hf-mirror.com/owner/repo/resolve/commit123/folder/model.index']


def test_packaging_excludes_private_runtime_models_and_credentials(tmp_path):
    spec = importlib.util.spec_from_file_location('portable_builder', ROOT / 'scripts/build-portable.py')
    builder = importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)
    for name in ['runtime/private.wav', 'resources/models/private.pth', '.env', '.git/config', '.tools/token']:
        p = tmp_path / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b'private')
    files = builder.package_files(tmp_path)
    assert all(str(name).replace('\\', '/') not in {p.as_posix() for p in files} for name in [
        'runtime/private.wav', 'resources/models/private.pth', '.env', '.git/config', '.tools/token'])
