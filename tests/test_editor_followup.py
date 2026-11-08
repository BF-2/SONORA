"""Regression checks for erased F0, sample-aligned muting and harmony fusion."""
import numpy as np
import pytest
from workers.pitch.voicing import mute_unpitched
from workers.separation.ensemble import shifted_prediction, fuse_vocals, ensemble_manifests
from workers.separation.options import separation_options


@pytest.mark.parametrize('rate', [32000, 40000, 44100, 48000])
def test_unpitched_samples_are_zero_and_preview_matches_full_crop(rate):
    target = {'f0_hz': [220, 220, None, None, 330, 330, 330, None],
              'voiced': [True, True, False, False, True, True, True, False],
              'sample_rate': 16000, 'hop': 160, 'frame_center_offset': 32000}
    audio = np.full((round(.08 * rate), 2), .5, dtype=np.float32)
    full, evidence = mute_unpitched(audio, rate, target, 2.)
    assert np.max(np.abs(full[round(.02 * rate):round(.04 * rate)])) == 0
    assert np.max(np.abs(full[round(.07 * rate):])) == 0
    assert full[round(.05 * rate), 0] == .5
    first = round(.001 * rate)
    last = round(.069 * rate)
    preview, _ = mute_unpitched(audio[first:last], rate, target, 2 + first / rate)
    np.testing.assert_array_equal(preview, full[first:last])
    assert evidence['total_samples'] == len(audio) and full.shape == audio.shape


def test_all_unvoiced_and_outside_analysis_is_silent():
    curve = {'f0_hz': [None] * 10, 'sample_rate': 16000, 'hop': 160}
    result, _ = mute_unpitched(np.ones(9600), 48000, curve, 0)
    assert len(result) == 9600 and not np.any(result)


def test_invalid_f0_is_reported():
    with pytest.raises(ValueError, match='Nonfinite'):
        mute_unpitched(np.ones(100), 48000, {'f0_hz': [np.nan], 'hop': 160, 'sample_rate': 16000}, 0)


def test_shifted_inference_undoes_delays_and_channel_swaps():
    source = np.vstack([np.linspace(-1, 1, 10000), np.linspace(.3, -.7, 10000)]).astype(np.float32)
    calls = []
    def demix(value):
        calls.append(value.shape)
        return {'lead': value * .6}
    lead, offsets = shifted_prediction(demix, source, 'lead', 44100)
    assert len(calls) == 3 and len(set(offsets)) == 3
    np.testing.assert_allclose(lead, source * .6, atol=1e-7)


@pytest.mark.parametrize('suppression', [0, .5, 1])
def test_ensemble_keeps_stereo_samples_and_reconstruction(suppression):
    if suppression:
        pytest.importorskip('scipy', reason='Spectral fusion runs in the audio worker or system numerical-test environment')
    rng = np.random.default_rng(721)
    time = np.arange(44100) / 44100
    lead = np.tile((.2 * np.sin(2 * np.pi * 220 * time))[:, None], (1, 2))
    backing = np.column_stack([.08 * np.sin(2 * np.pi * 330 * time), .1 * np.sin(2 * np.pi * 440 * time)])
    source = (lead + backing).astype(np.float32)
    # Declared estimator-error fixture, independent of neural quality evaluation.
    error = .04 * rng.standard_normal(source.shape)
    first = lead + error
    second = lead - .5 * error
    new_lead, new_backing, metadata = fuse_vocals(source, first, second, suppression=suppression, spatial_protection=0)
    assert new_lead.shape == source.shape and new_backing.shape == source.shape
    np.testing.assert_allclose(new_lead + new_backing, source, atol=1e-7)
    assert metadata['reconstruction_max_abs'] < 1e-7
    if suppression == 0:
        assert np.mean((new_backing - backing) ** 2) < np.mean((source - first - backing) ** 2) / 4


def test_stable_stereo_harmony_is_recovered_from_center_leakage():
    time = np.arange(44100) / 44100
    lead = np.column_stack([np.sin(2 * np.pi * 220 * time)] * 2) * .3
    harmony = np.sin(2 * np.pi * 330 * time) * .1
    backing = np.column_stack([harmony * .4, harmony])
    source = lead + backing
    contaminated = lead + backing * .2
    _, result, metadata = fuse_vocals(source, contaminated, contaminated)
    assert metadata['spatial_method'] == 'stable_pan_projection'
    assert np.mean((result - backing) ** 2) < np.mean((source - contaminated - backing) ** 2) / 10


@pytest.mark.parametrize('narrow', [True, False])
def test_ambiguous_spatial_projection_preserves_neural_estimate(narrow):
    time = np.arange(44100) / 44100
    vocal = np.sin(2 * np.pi * 220 * time)
    lead = np.column_stack([vocal, vocal]) * .05
    harmony = np.sin(2 * np.pi * (330 if narrow else 220) * time) * .1
    backing = np.column_stack([harmony * (.98 if narrow else .4), harmony])
    output, _, metadata = fuse_vocals(lead + backing, lead, lead)
    assert metadata['spatial_method'] == 'ambiguous_pan_preserved'
    np.testing.assert_allclose(output, lead, atol=1e-8)


def test_enhanced_harmony_requires_two_distinct_models():
    primary = {'id': 'first', 'task': 'lead_backing', 'model_hash': 'a' * 64, 'sample_rate': 44100}
    with pytest.raises(ValueError, match='两套不同'):
        ensemble_manifests({'models': [primary], 'default_harmony_model': 'first'}, {})
    assert separation_options({'separation_task': 'lead_backing'})['separation_algorithm'] == 'ensemble'
    assert separation_options({})['separation_algorithm'] == 'single'
    with pytest.raises(ValueError):
        separation_options({'separation_algorithm': 'ensemble'})
