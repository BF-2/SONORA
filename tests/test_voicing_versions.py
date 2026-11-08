"""Per-Edit gating ownership, duration thresholds and smoothing behavior."""
import copy
import json
import numpy as np
import pytest
from workers.pitch.voicing import mute_unpitched, normalize_voicing_settings


def curve():
    return {'f0_hz': [220] * 5 + [None] * 3 + [220] * 5 + [None] * 8 + [220] * 5,
            'hop': 160, 'sample_rate': 16000, 'frame_center_offset': 32000}


def test_disabled_gate_is_an_exact_bypass():
    audio = np.linspace(-.7, .8, 12480, dtype=np.float64)
    result, audit = mute_unpitched(audio, 48000, curve(), 2, enabled=False, min_silence_ms=50, fade_ms=40)
    np.testing.assert_array_equal(result, audio)
    assert result is not audio and audit['bypassed'] and not audit['enabled']


@pytest.mark.parametrize('rate', [32000, 40000, 44100, 48000])
def test_threshold_classifies_whole_gaps_before_preview_cropping(rate):
    source = np.ones((round(.26 * rate), 2), dtype=np.float32)
    result, _ = mute_unpitched(source, rate, curve(), 2, min_silence_ms=50, fade_ms=0)
    assert np.all(result[round(.05 * rate):round(.08 * rate)] == 1)
    assert np.all(result[round(.13 * rate):round(.21 * rate)] == 0)
    for first, last in [(round(.14 * rate), round(.16 * rate)), (round(.06 * rate), round(.07 * rate))]:
        part, _ = mute_unpitched(source[first:last], rate, curve(), 2 + first / rate, min_silence_ms=50, fade_ms=0)
        np.testing.assert_array_equal(part, result[first:last])


def test_minimum_is_inclusive_and_smoothing_changes_only_transition_samples():
    audio = np.ones(12480, dtype=np.float32)
    exact, _ = mute_unpitched(audio, 48000, curve(), 2, min_silence_ms=30, fade_ms=0)
    higher, _ = mute_unpitched(audio, 48000, curve(), 2, min_silence_ms=30.1, fade_ms=0)
    assert not np.any(exact[2400:3840]) and np.all(higher[2400:3840] == 1)
    smooth, audit = mute_unpitched(audio, 48000, curve(), 2, min_silence_ms=50, fade_ms=20)
    assert smooth[0] == 0 and 0 < smooth[480] < 1 and smooth[1440] == 1
    assert not np.any(smooth[6240:10080]) and audit['fade_ms'] == 20
    cropped, _ = mute_unpitched(audio[240:12000], 48000, curve(), 2.005, min_silence_ms=50, fade_ms=20)
    np.testing.assert_array_equal(cropped, smooth[240:12000])


@pytest.mark.parametrize('invalid', [{'enabled': 1}, {'min_silence_ms': -1}, {'min_silence_ms': 2001},
    {'min_silence_ms': float('nan')}, {'fade_ms': 201}, {'fade_ms': True}, {'unexpected': 1}])
def test_invalid_settings_are_rejected(invalid):
    with pytest.raises(ValueError):normalize_voicing_settings(invalid)


def test_edit_switch_undo_recipe_and_generation_pin_own_settings():
    from tests.test_standard_workflow import separated_project, patch_project, pitch_action
    from tests.test_core import req, store, client, AUTH
    from services.orchestrator.production import pin_generation, active_version
    from services.orchestrator.rvc_service import render_signature
    p, _, main, follower = separated_project()
    first = p['active_pitch_versions'][follower['id']]
    settings_a = {'enabled': False, 'min_silence_ms': 70, 'fade_ms': 8}
    settings_b = {'enabled': True, 'min_silence_ms': 180, 'fade_ms': 25}
    p = patch_project(p, {'voice_parts': {'backing_vocal': {'parameters': {'voicing_settings': settings_a}}}})
    p = pitch_action(p, follower, 'create')
    second = p['active_pitch_versions'][follower['id']]
    assert active_version(p, follower['id'])['parameters']['voicing_settings'] == settings_a
    p = patch_project(p, {'voice_parts': {'backing_vocal': {'parameters': {'voicing_settings': settings_b}}}})
    assert 'voicing_settings' not in p['voice_parts']['lead_vocal']['parameters']
    p = pitch_action(p, follower, 'undo', second)
    assert p['voice_parts']['backing_vocal']['parameters']['voicing_settings'] == settings_a
    p = pitch_action(p, follower, 'redo', second)
    assert p['voice_parts']['backing_vocal']['parameters']['voicing_settings'] == settings_b
    pinned = {}
    pin_generation(copy.deepcopy(p), follower['id'], pinned)
    signature_b = render_signature(follower, pinned)
    assert pinned['voicing_settings'] == settings_b
    p = pitch_action(p, follower, 'select', first)
    assert p['voice_parts']['backing_vocal']['parameters']['voicing_settings'] == settings_a
    params = {'voicing_settings': settings_b}
    pin_generation(copy.deepcopy(p), follower['id'], params)
    assert params['voicing_settings'] == settings_a  # Caller cannot borrow another Edit's gate.
    assert render_signature(follower, params) != signature_b
    assert pinned['voicing_settings'] == settings_b  # The submitted job snapshot stays independent.
    recipe = req('GET', f'/projects/{p["id"]}/recipe')
    restored = req('POST', '/projects/restore', files={'file': ('voicing.json', json.dumps(recipe).encode(), 'application/json')})
    assert restored['pitch_versions'] == p['pitch_versions']
    assert restored['voice_parts']['backing_vocal']['parameters']['voicing_settings'] == settings_a
    before = store.get('projects', p['id'])
    bad = client.patch(f'/api/v1/projects/{p["id"]}', headers=AUTH, json={'revision': before['revision'],
        'patch': {'voice_parts': {'backing_vocal': {'parameters': {'voicing_settings': {'fade_ms': -1}}}}}})
    assert bad.status_code == 400 and store.get('projects', p['id']) == before
    params = {}
    pin_generation(copy.deepcopy(p), main['id'], params)
    assert params['voicing_settings'] == {'enabled': True, 'min_silence_ms': 0, 'fade_ms': 3}
