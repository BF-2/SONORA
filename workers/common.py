"""SONORA worker protocol; subprocess isolation is not a security sandbox."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
PROTOCOL_VERSION = 1
ADAPTER_VERSION = "sonora-workers-1.0.0"


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path, limit=16 * 1024 * 1024):
    path = Path(path)
    if path.stat().st_size > limit:
        raise ValueError("JSON exceeds allowed size")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".partial")
    tmp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def resource_config(request=None):
    sys.path.insert(0, str(ROOT))
    from services.orchestrator.resource_paths import resolve_resources
    return resolve_resources(read_json((request or {}).get("resource_config_path") or ROOT / "engine-envs" / "resources.json"), ROOT)


def configure_media_tools(config):
    """Third-party workers also invoke ffmpeg/ffprobe by executable name."""
    executable = config.get('ffmpeg')
    if executable:
        tool_directory = str(Path(executable).resolve().parent)
        os.environ['PATH'] = tool_directory + os.pathsep + os.environ.get('PATH', '')


def verify_environment_lock(engine):
    """Detect updates to reused runtimes instead of silently running a new version."""
    import importlib.metadata as metadata
    locks=read_json(resource_config().get('environment_lock_path') or ROOT/'engine-envs/locked-environments.json')
    key='separation' if engine in ('separation','svs') else 'rvc'
    expected=locks[key]['packages']
    differences=[]
    for package,version in expected.items():
        try: actual=metadata.version(package)
        except metadata.PackageNotFoundError: actual='missing'
        if actual!=version: differences.append(f'{package}: locked {version}, actual {actual}')
    if differences:
        raise RuntimeError('Engine environment changed; re-probe and validate before rendering: '+'; '.join(differences[:8]))


def safe_torch():
    # Upstream loaders cannot silently opt out of restricted loading.
    os.environ["TORCH_FORCE_WEIGHTS_ONLY_LOAD"] = "1"
    import torch
    original = torch.load
    if not getattr(original, "_sonora_safe", False):
        def restricted(*args, **kwargs):
            if kwargs.get("pickle_module") is not None:
                raise ValueError("Custom pickle loaders are forbidden")
            kwargs["weights_only"] = True
            return original(*args, **kwargs)
        restricted._sonora_safe = True
        torch.load = restricted
    return torch


def setup_applio(config):
    root = Path(config["applio"]["root"]).resolve()
    if not (root / "rvc" / "infer" / "pipeline.py").is_file():
        raise FileNotFoundError("Applio private snapshot missing; run bootstrap_resources.py")
    os.chdir(root)
    sys.path.insert(0, str(root))
    sys.dont_write_bytecode = True
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1")
    return root


def device_name(parameters=None):
    torch = safe_torch()
    choice = (parameters or {}).get("device", "auto")
    if choice not in ("auto", "cpu", "cuda", "cuda:0"):
        raise ValueError("Unsupported device")
    if choice.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError("CUDA runtime is unavailable")
    return "cuda:0" if choice != "cpu" and torch.cuda.is_available() else "cpu"


def environment(engine):
    import importlib.metadata as metadata
    torch = safe_torch()
    result = {"id": engine, "protocol_version": 1, "adapter_version": ADAPTER_VERSION,
              "python": sys.version.split()[0], "torch": torch.__version__,
              "cuda_available": torch.cuda.is_available(), "status": "ready"}
    if torch.cuda.is_available():
        result["device"] = torch.cuda.get_device_name(0)
        result["vram_bytes"] = torch.cuda.get_device_properties(0).total_memory
    for package in ["audio-separator", "transformers", "librosa", "soundfile", "torchcrepe", "torchfcpe", "pyworld"]:
        try:
            result[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            pass
    return result


def audio_metadata(path, role, engine, processing_kind="model_inference", **extra):
    import soundfile as sf
    info = sf.info(str(path))
    if info.frames <= 0:
        raise ValueError("Worker produced empty audio")
    return {"path": str(Path(path).resolve()), "role": role, "processing_kind": processing_kind,
            "sample_rate": info.samplerate, "sample_count": info.frames, "channels": info.channels,
            "engine": engine, "worker": ADAPTER_VERSION, "source_offset": 0,
            "valid_range": [0, info.frames], "latency_samples": None, "delay_compensation_samples": 0,
            "time_map": {"source_rate": info.samplerate, "output_rate": info.samplerate, "source_offset": 0, "output_offset": 0},
            **extra}


def worker_main(engine, render, probe):
    parser = argparse.ArgumentParser()
    parser.add_argument("--request")
    parser.add_argument("--response")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    if args.probe:
        print(json.dumps(probe(), ensure_ascii=False))
        return 0
    if not args.request or not args.response:
        parser.error("--request and --response required")
    started = time.perf_counter()
    try:
        request = read_json(args.request)
        configure_media_tools(resource_config(request))
        if request.get("protocol_version") != PROTOCOL_VERSION:
            raise ValueError("Unsupported worker protocol")
        verify_environment_lock(engine)
        input_path = None
        if engine != "svs" or request.get("input_path"):
            input_path = Path(request["input_path"]).resolve(strict=True)
            if not input_path.is_file() or input_path.suffix.lower() not in (".wav", ".flac", ".aiff", ".aif"):
                raise ValueError("Workers accept decoded local PCM audio only")
        output_dir = Path(request["output_dir"]).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        request.update(output_dir=str(output_dir))
        if input_path: request['input_path']=str(input_path)
        result = render(request)
        for output in result.get("outputs", []):
            out = Path(output["path"]).resolve(strict=True)
            if not out.is_relative_to(output_dir):
                raise ValueError("Worker output escaped job directory")
            output["sha256"] = sha256(out)
        result.update(ok=True)
        result.setdefault("metadata", {}).update(elapsed_seconds=time.perf_counter()-started, adapter_version=ADAPTER_VERSION)
        write_json(args.response, result)
        return 0
    except BaseException as exc:
        traceback.print_exc()
        code = "missing_dependency" if isinstance(exc, (ImportError, FileNotFoundError)) else "inference_failed"
        if "out of memory" in str(exc).lower():
            code = "out_of_memory"
        write_json(args.response, {"ok": False, "error": {"code": code, "message": str(exc)[:2000]},
                                   "metadata": {"elapsed_seconds": time.perf_counter()-started}})
        return 1
