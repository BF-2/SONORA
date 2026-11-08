"""Resolve portable resource paths at runtime, including after moving the folder."""
from pathlib import Path
import copy

PATH_KEYS = {'python', 'root', 'original_root', 'path', 'origin_path', 'index_path',
             'config_path', 'model_dir', 'ffmpeg', 'ffprobe', 'environment_lock_path'}


def resolve_resources(config, root):
    if not config.get('portable_version'):
        return config
    root = Path(root).resolve()

    def resolve(value, key=None):
        if isinstance(value, dict):
            return {name: resolve(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [resolve(item, 'path' if key == 'roots' else None) for item in value]
        if isinstance(value, str) and key in PATH_KEYS and value.startswith('./'):
            path = (root / value).resolve()
            if not path.is_relative_to(root):
                raise ValueError('Portable resource path escapes project root')
            return str(path)
        return value

    return resolve(copy.deepcopy(config))
