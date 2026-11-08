"""Use Python file I/O for Unicode FAISS paths on Windows."""
import os


def enable_unicode_paths(faiss):
    native = faiss.read_index
    if getattr(native, '_sonora_unicode_paths', False):
        return

    def read_index(source, *args):
        if isinstance(source, (str, bytes, os.PathLike)):
            path = os.fsdecode(source)
            if not path.isascii():
                with open(path, 'rb') as stream:
                    return native(faiss.PyCallbackIOReader(stream.read), *args)
            source = path
        return native(source, *args)

    read_index._sonora_unicode_paths = True
    faiss.read_index = read_index
