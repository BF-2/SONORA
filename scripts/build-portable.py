"""Build a clean Windows portable ZIP with optional preinstalled local runtimes."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import stat
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = '1.1.0'
SCRIPTS = ['start-sonora.ps1', 'stop-sonora.ps1', 'prepare-sonora.ps1', 'portable_setup.py']


def regular_files(directory):
    def reparse(path):
        info = path.lstat()
        return path.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))
    for folder, dirs, files in os.walk(directory, followlinks=False):
        base = Path(folder)
        dirs[:] = [name for name in dirs if name != '__pycache__' and not reparse(base / name)]
        for name in files:
            path = base / name
            if path.suffix not in ('.pyc', '.log') and not reparse(path):
                yield path


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def package_files(root):
    root = Path(root)
    single = ['README.md', '一键准备声序.bat', '启动声序.bat', '启动声序-浏览器.bat', '停止声序.bat',
              'engine-envs/portable-assets.json', 'docs/portable-setup.md']
    single += ['scripts/' + name for name in SCRIPTS]
    single += ['requirements/portable-' + name + '.txt' for name in ('core', 'rvc', 'separation')]
    single += ['apps/web/qa/six-stage/primary-' + name + '-export.png' for name in ('classic', 'soft')]
    result = {Path(p): root / p for p in single}
    for directory in ['services', 'workers', 'vendor', 'apps/web/dist']:
        for path in (root / directory).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                result[path.relative_to(root)] = path
    # Source checkouts can prepare their own frontend with the portable Node installer.
    for name in ('package.json', 'package-lock.json', 'index.html', 'vite.config.ts', 'tsconfig.json', 'tsconfig.app.json', 'tsconfig.node.json'):
        path = root / 'apps/web' / name
        if path.is_file():
            result[path.relative_to(root)] = path
    for directory in ['apps/web/src', 'packages/contracts']:
        for path in (root / directory).rglob('*'):
            if path.is_file() and path.suffix not in ('.pyc', '.tsbuildinfo'):
                result[path.relative_to(root)] = path
    return result


def build(root=ROOT, output=None, tools=None, include_runtimes=False, version=VERSION):
    root = Path(root).resolve()
    output = Path(output or root / 'releases').resolve()
    output.mkdir(parents=True, exist_ok=True)
    if not (root / 'apps/web/dist/index.html').is_file():
        raise ValueError('Build apps/web first.')
    files = package_files(root)
    if tools:
        tools = Path(tools).resolve()
        uv = tools / 'bin/uv.exe'
        inputs = json.loads((root / 'engine-envs/portable-assets.json').read_text(encoding='utf-8'))
        if sha256(uv) != inputs['uv']['executable_sha256']:
            raise ValueError('Bundled uv executable checksum mismatch')
        files[Path('.tools/bin/uv.exe')] = uv
        if include_runtimes:
            for name in ('python-base', 'runtimes'):
                for path in regular_files(tools / name):
                    files[Path('.tools') / path.relative_to(tools)] = path
            for name in ('core', 'rvc', 'separation'):
                if not (tools / 'runtimes' / name / 'python.exe').is_file():
                    raise ValueError('Missing preinstalled runtime: ' + name)
            prepared_root = tools.parent
            for name in ('ffmpeg.exe', 'ffprobe.exe'):
                files[Path('resources/tools') / name] = prepared_root / 'resources/tools' / name
    elif include_runtimes:
        raise ValueError('--tools is required with --include-runtimes')
    for relative, path in files.items():
        if not path.is_file():
            raise FileNotFoundError(path)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Invalid archive path')
        if path.is_symlink():
            raise ValueError('Release inputs must be ordinary files: ' + str(path))
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root).decode().strip()
    manifest = {'version': version, 'commit': commit, 'platform': 'windows-x64',
                'runtimes_preinstalled': include_runtimes, 'files': {p.as_posix(): sha256(f) for p, f in sorted(files.items())}}
    archive = output / f'SONORA-v{version}-windows-x64-portable.zip'
    prefix = 'SONORA/'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as bundle:
        for relative, path in sorted(files.items()):
            bundle.write(path, prefix + relative.as_posix())
        bundle.writestr(prefix + 'portable-release.json', json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    checksum = sha256(archive)
    checksum_path = archive.with_suffix('.zip.sha256')
    checksum_path.write_text(checksum + '  ' + archive.name + '\n', encoding='ascii')
    print(json.dumps({'archive': str(archive), 'bytes': archive.stat().st_size, 'sha256': checksum,
                      'checksum_file': str(checksum_path), 'files': len(files), 'commit': commit}, ensure_ascii=False), flush=True)
    return archive


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--tools', type=Path)
    parser.add_argument('--include-runtimes', action='store_true')
    parser.add_argument('--version', default=VERSION)
    args = parser.parse_args()
    build(output=args.output, tools=args.tools, include_runtimes=args.include_runtimes, version=args.version)
