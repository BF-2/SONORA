"""Render and cache complete post-fader effect chains before mixing/cropping."""
import json
import os
from pathlib import Path
import sys
import tempfile
from .config import DATA, ROOT, binary
from .audio_effects import VERSION, native_effect


def restoration_inputs(store, artifact):
    sources = [store.get('artifacts', aid) for aid in artifact.get('input_artifact_ids', [])]
    reference = next((a for a in sources if a.get('media_type', '').startswith('audio/')), None)
    pitch = next((a for a in sources if a.get('stem_role') == 'pitch'), None)
    if not reference or not pitch:
        raise ValueError('人声厚度修复需要带原人声和 F0 来源的转换候选；该轨道可单独使用 C 空间。')
    return reference, pitch


def resolved_target(artifact, reference, pitch_data):
    """Use the exact generation target, including transpose, edits and locks."""
    from workers.pitch.engine import apply_target_parameters, shift_pitch_to_project, contour_hash
    recorded = artifact.get('worker_metadata', {})
    expected = artifact.get('requested_f0_hash') or recorded.get('requested_f0_hash')
    params = recorded.get('target_parameters')
    if params is not None and pitch_data.get('curve_layer') != 'target':
        offset = recorded.get('context_parameters', {}).get('source_timeline_offset_ticks', reference.get('timeline_offset_ticks', 0))
        target = apply_target_parameters(shift_pitch_to_project(pitch_data, offset), params)
    else:
        target = pitch_data.get('target_layer') or pitch_data
    if not expected or contour_hash(target) != expected:
        raise ValueError('人声厚度修复无法核对该候选实际使用的目标音高；请重新生成候选，或单独使用 C 空间。')
    return {'target_layer': target}


def prepared_track(store, artifact, track, cancel=None):
    from . import media
    effects = [fx for fx in track.get('effects', []) if fx.get('enabled', True)]
    reference = pitch = None
    if any(fx['type'] == 'vocal_restore' for fx in effects):
        reference, pitch = restoration_inputs(store, artifact)
    input_path = media.validate_artifact(artifact)
    if reference: media.validate_artifact(reference); media.validate_artifact(pitch)
    key = media.hash_json({'version': VERSION, 'input': artifact['sha256'], 'effects': effects,
        'gain': track.get('gain_db', 0), 'pan': track.get('pan', 0),
        'reference': reference and reference['sha256'], 'pitch': pitch and pitch['sha256'],
        'source_time': artifact.get('timeline_offset_ticks', 0), 'reference_time': reference and reference.get('timeline_offset_ticks', 0)})
    folder = DATA / 'cache' / 'mix-effects'; folder.mkdir(parents=True, exist_ok=True)
    destination = folder / (key + '.wav')
    if destination.is_file(): return destination
    # The chain always sees the full source; selection previews are cropped later.
    # This keeps convolution history and input-dependent gain identical to export.
    with tempfile.TemporaryDirectory(prefix=key[:10] + '-', dir=folder) as temporary:
        tmp = Path(temporary)
        gain = 10 ** (media.bounded(track.get('gain_db', 0), -96, 24) / 20)
        pan = media.bounded(track.get('pan', 0), -1, 1)
        left = (1 - max(0, pan)) ** .5; right = (1 + min(0, pan)) ** .5
        current = tmp / 'input.wav'
        media.run([binary('ffmpeg'), '-v', 'error', '-nostdin', *media.safe_input(input_path), '-af',
            f'aresample=48000,aformat=channel_layouts=stereo,volume={gain},pan=stereo|c0={left}*c0|c1={right}*c1',
            '-c:a', 'pcm_f32le', '-y', str(current)], cancel)
        diagnostics = []
        for i, fx in enumerate(effects):
            output = tmp / f'effect-{i}.wav'
            if native_effect(fx):
                req = {'input': str(current), 'output': str(output), 'effect': fx}
                if fx['type'] == 'vocal_restore':
                    target = resolved_target(artifact, reference, json.loads(Path(pitch['path']).read_text(encoding='utf-8')))
                    target_path = tmp / f'target-{i}.json'; target_path.write_text(json.dumps(target), encoding='utf-8')
                    req.update(reference=str(reference['path']), pitch=str(target_path),
                        reference_offset_seconds=(artifact.get('timeline_offset_ticks', 0) - reference.get('timeline_offset_ticks', 0)) / 48000,
                        pitch_offset_seconds=artifact.get('timeline_offset_ticks', 0) / 48000)
                request = tmp / f'effect-{i}.json'; request.write_text(json.dumps(req, ensure_ascii=False), encoding='utf-8')
                media.run([sys.executable, '-B', '-m', 'services.orchestrator.audio_effects', '--request', str(request)], cancel,
                    cwd=ROOT, env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1'})
                diagnostics.append(json.loads(Path(str(output) + '.json').read_text(encoding='utf-8')))
            else:
                filters = media.effects_filter([fx])
                media.run([binary('ffmpeg'), '-v', 'error', '-nostdin', *media.safe_input(current), '-af', ','.join(filters), '-c:a', 'pcm_f32le', '-y', str(output)], cancel)
            current = output
        meta = tmp / 'diagnostics.json'; meta.write_text(json.dumps({'version': VERSION, 'effects': diagnostics}, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(meta, destination.with_suffix('.json')); os.replace(current, destination)
    return destination
