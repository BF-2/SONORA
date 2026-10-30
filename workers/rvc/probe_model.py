"""Restricted, inference-free RVC checkpoint inspection. Never falls back to pickle."""
from pathlib import Path
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import safe_torch, sha256


def probe_model(path):
    torch = safe_torch()
    path = Path(path).resolve(strict=True)
    if path.suffix.lower() not in (".pth", ".pt") or path.stat().st_size > 2 * 1024**3:
        raise ValueError("Unsupported checkpoint type or size")
    # Static pickle-global inspection happens before tensor deserialization.
    unsafe = torch.serialization.get_unsafe_globals_in_checkpoint(path)
    if unsafe:
        raise ValueError("Restricted checkpoint contains unsupported globals: " + ", ".join(unsafe[:8]))
    data = torch.load(str(path), map_location="cpu", weights_only=True)
    if not isinstance(data, dict) or not isinstance(data.get("weight"), dict):
        raise ValueError("Not an inference RVC checkpoint; weight/config required")
    config, weights = data.get("config"), data["weight"]
    if not isinstance(config, (list, tuple)) or len(config) < 10 or "emb_g.weight" not in weights:
        raise ValueError("RVC config or speaker embedding missing")
    version = data.get("version", "v1")
    if version not in ("v1", "v2") or data.get("vocoder", "HiFi-GAN") not in ("HiFi-GAN", "RefineGAN"):
        raise ValueError("Unsupported RVC version/vocoder")
    sample_rate = int(config[-1])
    if sample_rate not in (24000,32000,40000,48000):
        raise ValueError("Unsupported RVC sample rate")
    use_f0 = bool(data.get("f0", 1))
    return {"engine_id":"rvc", "architecture":"RVC-" + version,
            "sample_rate": sample_rate, "content_dim":768 if version=="v2" else 256,
            "speaker_ids":list(range(int(weights["emb_g.weight"].shape[0]))),
            "f0_conditioned":use_f0, "vocoder":data.get("vocoder", "HiFi-GAN"),
            "capabilities":{"voice_conversion":True,"singing_conversion":True,"external_f0":use_f0,
                            "svs":False,"streaming":False,"local_segment_render":False,"notes":False,
                            "phoneme_timing":False,"training":False,"cancel_mode":"terminate_process"},
            "model_hash":sha256(path), "test_status":"safe_probe_passed"}


if __name__ == "__main__":
    results=[]
    for name in sys.argv[1:]:
        try: results.append({"path":name,"ok":True,**probe_model(name)})
        except Exception as exc: results.append({"path":name,"ok":False,"error":str(exc)})
    print(json.dumps(results,ensure_ascii=False))
