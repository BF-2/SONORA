"""Restricted, inference-free RVC checkpoint inspection. Never falls back to pickle."""
from pathlib import Path
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import safe_torch, sha256
from catalog_probe import static_checkpoint


def probe_model(path):
    torch = safe_torch()
    path = Path(path).resolve(strict=True)
    if path.suffix.lower() not in (".pth", ".pt") or path.stat().st_size > 2 * 1024**3:
        raise ValueError("Unsupported checkpoint type or size")
    metadata=static_checkpoint(path)
    if metadata.get('kind')!='inference_export':raise ValueError('Training checkpoint is not an inference export')
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
    required=['emb_g.weight','enc_p.emb_phone.weight','enc_p.proj.weight']
    if data.get('f0',1):required.append('enc_p.emb_pitch.weight')
    if data.get('vocoder','HiFi-GAN')=='HiFi-GAN':required.append('dec.conv_pre.weight')
    if any(not isinstance(weights.get(key),torch.Tensor) for key in required):
        raise ValueError('Missing required RVC inference tensors')
    if weights['enc_p.emb_phone.weight'].ndim!=2 or int(weights['enc_p.emb_phone.weight'].shape[1])!=(768 if version=='v2' else 256):
        raise ValueError('RVC content dimension does not match architecture version')
    sample_rate = int(config[-1])
    if sample_rate not in (24000,32000,40000,48000):
        raise ValueError("Unsupported RVC sample rate")
    use_f0 = bool(data.get("f0", 1))
    capacity=int(weights['emb_g.weight'].shape[0])
    count=data.get('speakers_id');speaker_source='checkpoint'
    if type(count) is not int:
        info_path=path.parent/'model_info.json'
        if info_path.is_file() and info_path.stat().st_size<1024*1024:
            info=json.loads(info_path.read_text(encoding='utf-8-sig'));count=info.get('speakers_id');speaker_source='verified_training_metadata'
    ids=list(range(count)) if type(count) is int and 0<count<=capacity else []
    return {**metadata,"engine_id":"rvc", "architecture":"RVC-" + version,
            "sample_rate": sample_rate, "content_dim":768 if version=="v2" else 256,
            "speaker_ids":ids,"speaker_ids_trained":ids,"embedding_speaker_capacity":capacity,
            "speaker_metadata_status":speaker_source if ids else 'unknown',
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
