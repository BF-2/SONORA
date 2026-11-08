"""Project-local runtime/model preparation, invoked by prepare-sonora.ps1."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def child(root, relative):
    if Path(relative).is_absolute() or PureWindowsPath(relative).drive:
        raise ValueError('Expected a project-relative path: ' + str(relative))
    path = (Path(root) / relative).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError('Path escapes project directory: ' + str(relative))
    return path


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, path)


def source_urls(url, mirrors):
    host = urllib.parse.urlsplit(url).hostname
    if host == 'huggingface.co':
        return [url] + [url.replace('https://huggingface.co', m.rstrip('/'), 1)
                        for m in mirrors['huggingface']]
    if host == 'github.com':
        return [url] + [m.rstrip('/') + '/' + url for m in mirrors['github']]
    return [url]


def probe(url, timeout=5):
    started = time.monotonic()
    try:
        request = urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'SONORA-Setup/1'})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status not in (200, 206):
                return float('inf')
        return time.monotonic() - started
    except (OSError, urllib.error.HTTPError):
        return float('inf')


def ranked(urls, preference='auto'):
    urls = list(dict.fromkeys(urls))
    if preference == 'origin':
        return urls
    if preference == 'mirror':
        return urls[1:] + urls[:1]
    with ThreadPoolExecutor(max_workers=min(len(urls), 4)) as pool:
        scores = list(pool.map(probe, urls))
    return [url for _, url in sorted(zip(scores, urls), key=lambda pair: pair[0])]


class Downloader:
    def __init__(self, root, mirrors, preference='auto', timeout=25, report=print):
        self.root = Path(root).resolve()
        self.mirrors = mirrors
        self.preference = preference
        self.timeout = timeout
        self.report = report
        self.records = []

    def fetch(self, entry, urls=None):
        path = child(self.root, entry['path'])
        expected = entry.get('bytes')
        checksum = entry['sha256']
        if path.is_file():
            if (expected is None or path.stat().st_size == expected) and digest(path) == checksum:
                self.report('[已校验] ' + path.name)
                return path
            raise ValueError('已有文件与固定版本不同，保留原件：' + str(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        if entry['url'].startswith('bundled:'):
            src = child(self.root, entry['url'][8:])
            if digest(src) != checksum:
                raise ValueError('Bundled resource checksum mismatch: ' + str(src))
            shutil.copyfile(src, path)
            return path
        candidates = ranked(urls or source_urls(entry['url'], self.mirrors), self.preference)
        partial = path.with_name(path.name + '.part')
        failures = []
        for url in candidates:
            for attempt in range(2):
                started = time.monotonic()
                try:
                    offset = partial.stat().st_size if partial.exists() else 0
                    if expected is None and offset and digest(partial) == checksum:
                        os.replace(partial, path)
                        return path
                    if expected is not None and offset >= expected:
                        if offset == expected and digest(partial) == checksum:
                            os.replace(partial, path)
                            return path
                        partial.rename(partial.with_name(partial.name + '.bad-' + uuid.uuid4().hex[:8]))
                        offset = 0
                    headers = {'User-Agent': 'SONORA-Setup/1', 'Accept-Encoding': 'identity'}
                    if offset:
                        headers['Range'] = f'bytes={offset}-'
                    self.report('[下载] ' + path.name + ' · ' + urllib.parse.urlsplit(url).netloc)
                    request = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        if response.status == 206:
                            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                            if not match or int(match[1]) != offset or (expected is not None and int(match[3]) != expected):
                                raise ValueError('Invalid resume Content-Range')
                            mode = 'ab' if offset else 'wb'
                        elif response.status == 200:
                            mode, offset = 'wb', 0  # Origin ignored Range: restart this partial file.
                            length = response.headers.get('Content-Length')
                            if length and expected is not None and int(length) != expected:
                                raise ValueError('Unexpected remote file size')
                        else:
                            raise ValueError('Unexpected HTTP response')
                        last_report = time.monotonic()
                        with partial.open(mode) as stream:
                            while True:
                                block = response.read(1024 * 1024)
                                if not block:
                                    break
                                offset += len(block)
                                if expected is not None and offset > expected:
                                    raise ValueError('Remote file exceeded fixed size')
                                stream.write(block)
                                if time.monotonic() - last_report >= 3:
                                    amount = f'{offset / 1048576:.1f} MB'
                                    if expected:
                                        amount += f' / {expected / 1048576:.1f} MB ({offset / expected:.0%})'
                                    self.report('  ' + amount)
                                    last_report = time.monotonic()
                            stream.flush()
                            os.fsync(stream.fileno())
                    if (expected is not None and partial.stat().st_size != expected):
                        raise OSError('Incomplete download')
                    if digest(partial) != checksum:
                        partial.rename(partial.with_name(partial.name + '.bad-' + uuid.uuid4().hex[:8]))
                        raise ValueError('SHA-256 mismatch')
                    os.replace(partial, path)
                    self.records.append({'path': entry['path'], 'source': url, 'sha256': checksum,
                                         'bytes': path.stat().st_size, 'seconds': round(time.monotonic() - started, 2)})
                    self.report('[完成] ' + path.name)
                    return path
                except (OSError, ValueError, http.client.HTTPException) as exc:
                    # Redirect URLs can carry temporary CDN credentials; report only error categories.
                    error = ('HTTP ' + str(exc.code)) if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
                    failures.append(error)
                    self.report('  连接重试 / 切换下载源：' + error)
        raise RuntimeError(path.name + ' 下载未完成；重新运行可续传。' + ', '.join(failures))


def extract_zip(archive, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            child(destination, item.filename)
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Archive symlink is not supported')
        bundle.extractall(destination)


def select_files(manifest, models):
    groups = {'base': {'rvc-base', 'base'}, 'm6': {'rvc-base', 'm6'},
              'all': {'rvc-base', 'base', 'm6'}, 'none': set()}[models]
    return [entry for entry in manifest['files'] if groups.intersection(entry['groups'])]


class Installer:
    def __init__(self, root, manifest, uv, base_python, source='auto', backend='auto'):
        self.root = Path(root).resolve()
        self.manifest = manifest
        self.uv = str(Path(uv).resolve())
        self.base_python = Path(base_python).resolve()
        self.source = source
        self.backend = self.choose_backend(backend)
        self.downloader = Downloader(self.root, manifest['mirrors'], source)
        temporary = self.root / '.tools/tmp'
        temporary.mkdir(parents=True, exist_ok=True)
        self.env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                    'UV_CACHE_DIR': str(self.root / '.tools/cache/uv'), 'UV_NO_CONFIG': '1',
                    'UV_CREDENTIALS_DIR': str(self.root / '.tools/uv-credentials'),
                    'UV_PYTHON_DOWNLOADS': 'never', 'TEMP': str(temporary), 'TMP': str(temporary)}

    @staticmethod
    def choose_backend(value):
        if value != 'auto':
            return value
        try:
            check = subprocess.run(['nvidia-smi', '--query-gpu=driver_version,compute_cap', '--format=csv,noheader'],
                                   capture_output=True, text=True, timeout=8)
            if check.returncode == 0:
                driver, capability = check.stdout.splitlines()[0].split(',')
                if int(driver.split('.')[0]) >= 570 and float(capability) >= 7.5:
                    return 'cu128'
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        return 'cpu'

    def run(self, command, **kwargs):
        subprocess.run([str(x) for x in command], cwd=self.root, env=self.env, check=True, **kwargs)

    def ensure_vendor(self):
        source = self.root / 'vendor/applio'
        snapshot = json.loads((source / 'snapshot.json').read_text(encoding='utf-8'))
        for entry in snapshot['files']:
            origin = child(source, entry['path'])
            target = child(self.root / 'resources/applio', entry['path'])
            if digest(origin) != entry['sha256']:
                raise ValueError('Engine source checksum mismatch: ' + entry['path'])
            if target.exists():
                if digest(target) != entry['sha256']:
                    raise ValueError('已有引擎代码有改动，保留原件：' + str(target))
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(origin, target)

    def ensure_runtime(self, name):
        destination = self.root / '.tools/runtimes' / name
        python = destination / 'python.exe'
        if not python.is_file():
            print('[安装 Python 环境] ' + name, flush=True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging = destination.with_name(name + '.staging-' + uuid.uuid4().hex[:8])
            shutil.copytree(self.base_python.parent, staging, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            staging.rename(destination)
        # The uv base remains immutable; this independent project-owned copy is
        # managed by SONORA and accepts packages in its own site-packages.
        managed_marker = child(destination, 'Lib/EXTERNALLY-MANAGED')
        if managed_marker.is_file():
            managed_marker.unlink()
        requirement = self.root / 'requirements' / ('portable-' + name + '.txt')
        wanted = dict(re.findall(r'^([\w-]+)==([^\s]+)', requirement.read_text(), re.M))
        fingerprint = hashlib.sha256(requirement.read_bytes() + (b'' if name == 'core' else self.backend.encode())).hexdigest()
        marker = destination / 'sonora-environment.json'
        code = "import importlib.metadata as m,json; print(json.dumps({d.metadata['Name']:d.version for d in m.distributions() if d.metadata['Name']}))"
        if marker.exists() and json.loads(marker.read_text())['fingerprint'] == fingerprint:
            check = subprocess.run([str(python), '-I', '-c', code], capture_output=True, text=True)
            if check.returncode == 0:
                actual = json.loads(check.stdout)
                normalized = {key.lower().replace('_', '-'): value.split('+')[0] for key, value in actual.items()}
                correct_backend = name == 'core' or actual.get('torch', '').endswith('+' + self.backend)
                if correct_backend and all(normalized.get(key.lower().replace('_', '-')) == value for key, value in wanted.items()):
                    health = subprocess.run([self.uv, 'pip', 'check', '--python', str(python)], env=self.env,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if health.returncode == 0:
                        print('[依赖已就绪] ' + name, flush=True)
                        return python
        indexes = ranked(['https://pypi.org/simple'] + self.manifest['mirrors']['pypi'], self.source)
        for index in indexes:
            command = [self.uv, 'pip', 'install', '--python', python, '--default-index', index,
                       '--requirement', requirement]
            if name != 'core':
                command += ['--torch-backend', self.backend]
                # Exact wheel flavours also switch a preinstalled CPU bundle to CUDA.
                command += [package + '==' + wanted[package] + '+' + self.backend
                            for package in ('torch', 'torchaudio', 'torchvision') if package in wanted]
            print('[安装依赖] ' + name + ' · ' + index, flush=True)
            try:
                self.run(command)
                self.run([self.uv, 'pip', 'check', '--python', python])
                write_json(marker, {'fingerprint': fingerprint, 'backend': self.backend if name != 'core' else None})
                return python
            except subprocess.CalledProcessError:
                print('[切换依赖源]', flush=True)
        raise RuntimeError('依赖安装未完成：' + name)

    def ensure_ffmpeg(self):
        folder = self.root / 'resources/tools'
        if all((folder / (name + '.exe')).is_file() for name in ('ffmpeg', 'ffprobe')):
            for name in ('ffmpeg', 'ffprobe'):
                subprocess.run([str(folder / (name + '.exe')), '-version'], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=True, timeout=15)
            return
        entry = {**self.manifest['ffmpeg'], 'path': '.tools/cache/ffmpeg.zip'}
        archive = self.downloader.fetch(entry)
        destination = self.root / '.tools/ffmpeg'
        extract_zip(archive, destination)
        folder.mkdir(parents=True, exist_ok=True)
        for name in ('ffmpeg.exe', 'ffprobe.exe'):
            matches = list(destination.rglob(name))
            if len(matches) != 1:
                raise ValueError('Missing/ambiguous FFmpeg archive member: ' + name)
            if not (folder / name).exists():
                shutil.copyfile(matches[0], folder / name)

    def ensure_web(self):
        web = self.root / 'apps/web'
        if (web / 'dist/index.html').is_file():
            return
        print('[准备界面构建工具]', flush=True)
        node = self.root / '.tools/node' / ('node-v' + self.manifest['node']['version'] + '-win-x64/node.exe')
        if not node.is_file():
            entry = {**self.manifest['node'], 'path': '.tools/cache/node.zip'}
            archive = self.downloader.fetch(entry, [entry['url']] + entry['mirrors'])
            extract_zip(archive, self.root / '.tools/node')
        self.env['PATH'] = str(node.parent) + os.pathsep + self.env.get('PATH', '')
        npm = node.parent / 'node_modules/npm/bin/npm-cli.js'
        indexes = ranked(['https://registry.npmjs.org'] + self.manifest['mirrors']['npm'], self.source)
        for index in indexes:
            try:
                self.run([node, npm, '--prefix', web, 'ci', '--registry', index, '--no-audit', '--no-fund'])
                self.run([node, npm, '--prefix', web, 'run', 'build'])
                return
            except subprocess.CalledProcessError:
                print('[切换 npm 源]', flush=True)
        raise RuntimeError('界面依赖安装或构建未完成')

    def configure(self):
        def rel(path):
            return './' + Path(path).relative_to(self.root).as_posix()

        def available(record):
            for key, checksum in [('path', 'model_hash'), ('config_path', 'config_hash'), ('index_path', 'index_hash')]:
                if key not in record:
                    continue
                target = child(self.root, record[key])
                if not target.is_file() or digest(target) != record[checksum]:
                    return False
            return True

        separators = [x for x in self.manifest['separators'] if available(x)]
        voices = [x for x in self.manifest['voices'] if available(x)]
        for voice in voices:
            write_json(child(self.root, voice['path']).parent / 'model_info.json',
                       {'provider': 'haysay', 'role': voice['role'], 'source_url': voice['origin_url'],
                        'inference_profile': voice['inference_profile']})
        snapshot = self.root / 'vendor/applio/snapshot.json'
        config = {'schema_version': 1, 'portable_version': 1,
                  'environment_lock_path': './.tools/environments.lock.json',
                  'applio': {'python': './.tools/runtimes/rvc/python.exe', 'root': './resources/applio',
                             'revision': json.loads(snapshot.read_text())['revision'], 'source_hash': digest(snapshot)},
                  'pitch': {'python': './.tools/runtimes/rvc/python.exe', 'pyworld_status': 'installed'},
                  'separation': {'python': './.tools/runtimes/separation/python.exe', 'version': '0.44.2',
                                 'model_dir': './resources/separation', 'models': separators,
                                 'default_model': 'mel_band_roformer_vocals_fv4_gabox',
                                 'default_harmony_model': 'melband-karaoke-aufr33-viperx-10.1956'},
                  'models': voices, 'catalog': {'roots': ['./resources/models/haysay'], 'provider': 'haysay'},
                  'svs': {'python': './.tools/runtimes/separation/python.exe', 'models': [], 'default_model': None},
                  'ffmpeg': './resources/tools/ffmpeg.exe', 'ffprobe': './resources/tools/ffprobe.exe'}
        locks = {}
        for name, key in [('rvc', 'rvc'), ('separation', 'separation')]:
            python = self.root / '.tools/runtimes' / name / 'python.exe'
            command = "import importlib.metadata as m,json; print(json.dumps({d.metadata['Name']:d.version for d in m.distributions() if d.metadata['Name']}))"
            packages = json.loads(subprocess.check_output([str(python), '-I', '-c', command], env=self.env))
            locks[key] = {'python': rel(python), 'packages': packages}
        write_json(self.root / '.tools/environments.lock.json', locks)
        target = self.root / 'engine-envs/resources.json'
        if target.exists():
            old = json.loads(target.read_text(encoding='utf-8'))
            if not old.get('portable_version'):
                backup = self.root / '.tools/backups' / ('resources-' + time.strftime('%Y%m%d-%H%M%S') + '.json')
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(target, backup)
            # Preserve installed voicebanks when preparing an existing project.
            if old.get('svs', {}).get('models'):
                config['svs'] = old['svs']
        write_json(target, config)
        write_json(self.root / '.tools/installation.json', {'schema_version': 1, 'backend': self.backend,
                   'voices': len(voices), 'separators': len(separators), 'downloads': self.downloader.records})
        print(f'[配置完成] {len(separators)} 个分离模型，{len(voices)} 个 M6 模型及配套 index', flush=True)

    def install(self, models, download_only=False):
        self.ensure_vendor()
        for entry in select_files(self.manifest, models):
            self.downloader.fetch(entry)
        if download_only:
            return
        for name in ('core', 'rvc', 'separation'):
            self.ensure_runtime(name)
        self.ensure_ffmpeg()
        self.ensure_web()
        self.configure()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--uv', type=Path, required=True)
    parser.add_argument('--base-python', type=Path, required=True)
    parser.add_argument('--models', choices=['base', 'm6', 'all', 'none'], default='base')
    parser.add_argument('--source', choices=['auto', 'origin', 'mirror'], default='auto')
    parser.add_argument('--torch-backend', choices=['auto', 'cpu', 'cu128'], default='auto')
    parser.add_argument('--download-only', action='store_true')
    parser.add_argument('--configure-only', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    manifest = json.loads((root / 'engine-envs/portable-assets.json').read_text(encoding='utf-8'))
    installer = Installer(root, manifest, args.uv, args.base_python, args.source, args.torch_backend)
    lockfile = root / '.tools/prepare.lock'
    lockfile.parent.mkdir(exist_ok=True)
    with lockfile.open('a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0)
            if not lock.read(1):
                lock.write(b'0'); lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        if args.configure_only:
            installer.configure()
        else:
            installer.install(args.models, args.download_only)
    print('声序准备完成。双击“启动声序.bat”开始制作。', flush=True)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n下载已暂停，重新运行可继续。', file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print('准备未完成：' + str(exc), file=sys.stderr)
        sys.exit(1)
