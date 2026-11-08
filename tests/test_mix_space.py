"""Signal-level and API regressions for production mixing, without neural models."""
import copy
import json
import threading
import uuid
import numpy as np
import soundfile as sf
import pytest
from tests.test_core import store, req, client, AUTH, DATA, fixture_project
from services.orchestrator import media
from services.orchestrator.audio_effects import room, restore, settings, SR
from services.orchestrator.mixing import preview_key


def stimulus(seconds=2):
    t = np.arange(int(SR * seconds)) / SR
    mono = (.08 * np.sin(2 * np.pi * 260 * t) + .04 * np.sin(2 * np.pi * 1040 * t)) * (t < seconds * .55)
    return np.column_stack([mono, mono]).astype(np.float32)


def test_space_zero_is_exact_and_strength_changes_side_energy():
    x = stimulus()
    y, _ = room(x, {'type': 'vocal_space', 'strength': 0})
    assert np.array_equal(x, y)
    side = []
    for strength in (100, 200, 316):
        y, _ = room(x, {'type': 'vocal_space', 'strength': strength})
        assert y.shape == x.shape and np.isfinite(y).all()
        assert abs(np.mean(y ** 2) / np.mean(x ** 2) - 1) < .0001
        side.append(np.mean(((y[:, 0] - y[:, 1]) / 2) ** 2))
        assert np.sqrt(np.mean(y[int(1.15 * SR):int(1.3 * SR)] ** 2)) > 1e-5
    assert side[2] > side[1] > side[0] > 0
    centered, _ = room(x, {'type': 'vocal_space', 'width': 0})
    assert np.array_equal(centered[:, 0], centered[:, 1])


def test_room_keeps_antiphase_input_and_direct_timing():
    x = stimulus(); x[:, 1] *= -1
    y, _ = room(x, {'type': 'reverb', 'algorithm': 'diffuse-v1', 'wet': .15})
    assert np.mean(y ** 2) > .99 * np.mean(x ** 2)
    assert np.max(abs(y[:1000])) > .01
    silent, _ = room(np.zeros((100, 2), dtype=np.float32), {'type': 'vocal_space'})
    assert np.array_equal(silent, np.zeros_like(silent))


def test_restoration_preserves_length_silence_and_increases_body():
    x = stimulus(); ref = x.copy()
    t = np.arange(len(x)) / SR
    ref += (.12 * np.sin(2 * np.pi * 260 * t) * (t < 1.1))[:, None]
    pitch = {'f0_hz': [260.] * 200, 'sample_rate': 16000, 'hop': 160}
    y, info = restore(x, ref, pitch, 0)
    assert y.shape == x.shape and np.isfinite(y).all()
    assert np.max(abs(y[int(1.4 * SR):])) == 0
    f = np.fft.rfftfreq(SR, 1 / SR)
    before = abs(np.fft.rfft(x[:SR, 0])); after = abs(np.fft.rfft(y[:SR, 0]))
    assert after[np.argmin(abs(f - 260))] / after[np.argmin(abs(f - 1040))] > before[np.argmin(abs(f - 260))] / before[np.argmin(abs(f - 1040))]
    assert info['reference_audio_mixed'] is False
    assert np.array_equal(restore(x, ref, pitch, 0, 0)[0], x)


def test_native_preview_equals_full_slice_and_order_changes_sound():
    p, a = fixture_project()
    track = copy.deepcopy(p['tracks'][0]); track.update(mute=False, offset_ticks=2400)
    track['effects'] = [{'type': 'vocal_space', 'strength': 200, 'enabled': True}, {'type': 'limiter', 'limit_db': -18, 'enabled': True}]
    folder = DATA / 'jobs' / uuid.uuid4().hex; folder.mkdir()
    full = folder / 'full.wav'; clip = folder / 'clip.wav'; reordered = folder / 'reordered.wav'
    media.mix(store, [track], full, threading.Event())
    region = [12000, 36000]; media.mix(store, [track], clip, threading.Event(), range_ticks=region)
    x, sr = sf.read(full); y, _ = sf.read(clip)
    assert sr == SR and len(y) == 24000
    assert np.max(abs(x[region[0]:region[1]] - y)) < 1e-7
    assert np.max(abs(x[:2400])) == 0
    track['effects'].reverse(); media.mix(store, [track], reordered, threading.Event())
    z, _ = sf.read(reordered)
    assert np.sqrt(np.mean((x - z) ** 2)) > 1e-4


def test_invalid_parameters_rejected_atomically_and_legacy_preserved():
    p, _ = fixture_project(); tracks = copy.deepcopy(p['tracks'])
    for bad in [351, -1, True, float('inf')]:
        tracks[0]['effects'] = [{'type': 'vocal_space', 'strength': bad, 'enabled': True}]
        if not np.isfinite(float(bad)): continue  # JSON encoder correctly refuses infinity.
        response = client.patch('/api/v1/projects/' + p['id'], headers=AUTH, json={'revision': p['revision'], 'patch': {'tracks': tracks}})
        assert response.status_code == 400
        assert req('GET', '/projects/' + p['id'])['revision'] == p['revision']
    with pytest.raises(ValueError): settings({'type': 'vocal_space', 'strength': float('inf')})
    tracks[0]['effects'] = [{'type': 'vocal_restore', 'strength': 100, 'enabled': True}]
    response = client.patch('/api/v1/projects/' + p['id'], headers=AUTH, json={'revision': p['revision'], 'patch': {'tracks': tracks}})
    assert response.status_code == 400
    legacy = media.effects_filter([{'type': 'reverb', 'wet': .4, 'room_seconds': 1, 'predelay_ms': 18}])
    assert len(legacy) == 1 and legacy[0].startswith('aecho=1:1:')


def test_preview_key_does_not_change_when_unrelated_mix_is_added():
    p, _ = fixture_project(); k = preview_key(p, [0, 48000])
    p['assets'].append({'id': 'unrelated-preview', 'sha256': 'new-preview'})
    assert preview_key(p, [0, 48000]) == k


def test_restore_uses_verified_generation_f0_not_an_unedited_curve():
    from services.orchestrator.effect_render import resolved_target
    from workers.pitch.engine import apply_target_parameters, shift_pitch_to_project, contour_hash
    data = {'sample_rate':16000,'hop':160,'frame_center_offset':0,'f0_hz':[220.]*100,'voiced':[True]*100,'curve_layer':'detected'}
    params = {'transpose':12,'pitch_edits':[{'start_tick':48000,'end_tick':48480,'operation':'unvoiced'}]}
    reference = {'timeline_offset_ticks':48000}
    expected = apply_target_parameters(shift_pitch_to_project(data,48000),params)
    artifact = {'requested_f0_hash':contour_hash(expected),'worker_metadata':{'target_parameters':params}}
    actual = resolved_target(artifact,reference,data)['target_layer']
    assert actual['f0_hz'][0] is None and actual['f0_hz'][1] == 440
    artifact['requested_f0_hash'] = 'wrong-hash'
    with pytest.raises(ValueError):resolved_target(artifact,reference,data)
