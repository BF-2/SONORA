"""Deterministic shifted inference and complementary two-model vocal fusion."""
import numpy as np


def shifted_prediction(demix, mixture, target, sample_rate):
    predictions = []
    offsets = [0, round(.113 * sample_rate), round(.227 * sample_rate)]
    for shift in offsets:
        padded = np.pad(mixture, ((0, 0), (shift, 0)), mode='reflect' if mixture.shape[1] > 1 else 'edge')
        # Swap channels on one pass to average channel-order sensitivity too.
        swap = shift == offsets[-1]
        prediction = demix(padded[::-1].copy() if swap else padded)
        if target not in prediction:
            raise ValueError('Shifted inference did not return the registered lead stem')
        estimate = np.asarray(prediction[target], dtype=np.float32)
        if estimate.ndim != 2 or estimate.shape[0] != mixture.shape[0] or estimate.shape[1] < shift + mixture.shape[1]:
            raise ValueError('Shifted inference returned an invalid time/channel grid')
        if swap:
            estimate = estimate[::-1]
        estimate = estimate[:, shift:shift + mixture.shape[1]]
        if not np.isfinite(estimate).all():
            raise ValueError('Nonfinite shifted prediction')
        predictions.append(estimate)
    return np.mean(predictions, axis=0, dtype=np.float64).astype(np.float32), offsets


def protect_spatial_backing(source, lead, sample_rate, strength):
    """Recover coherent wide backing when the neural Main is center dominant."""
    mid = np.mean(lead, axis=1)
    side = (lead[:, 0] - lead[:, 1]) * .5
    center_ratio = np.sqrt(np.sum(side.astype(np.float64) ** 2) / (np.sum(mid.astype(np.float64) ** 2) + 1e-12))
    center_confidence = np.clip((.3 - center_ratio) / .2, 0, 1)
    if center_confidence == 0:
        return lead, {'spatial_center_confidence': 0., 'spatial_blend_mean': 0.}
    source_mid = source.mean(axis=1).astype(np.float64)
    source_side = ((source[:, 0] - source[:, 1]) * .5).astype(np.float64)
    backing_mid = source_mid - mid
    side_energy = np.dot(source_side, source_side)
    covariance = np.dot(backing_mid, source_side)
    global_coherence = covariance ** 2 / (np.dot(backing_mid, backing_mid) * side_energy + 1e-16)
    if global_coherence > .8:
        # A stable pan image supplies a separate observation of the backing.
        # Estimate its mid/side ratio over the whole take to suppress unrelated
        # center content, then blend according to the neural residual agreement.
        coefficient = np.dot(source_mid, source_side) / (side_energy + 1e-12)
        predicted_coefficient = covariance / (side_energy + 1e-12)
        ambiguous_pan = abs(coefficient) > 4 or abs(coefficient - predicted_coefficient) > .3 * max(abs(predicted_coefficient), 1.)
        if ambiguous_pan:
            return lead, {'spatial_center_confidence': float(center_confidence), 'spatial_blend_mean': 0.,
                          'spatial_global_coherence': float(global_coherence), 'spatial_method': 'ambiguous_pan_preserved'}
        spatial_backing = np.column_stack([(coefficient + 1) * source_side, (coefficient - 1) * source_side])
        blend = strength * center_confidence * np.clip((global_coherence - .8) / .15, 0, 1)
        backing = (source - lead) * (1 - blend) + spatial_backing * blend
        return (source - backing).astype(np.float32), {'spatial_center_confidence': float(center_confidence),
            'spatial_blend_mean': float(blend), 'spatial_global_coherence': float(global_coherence),
            'spatial_method': 'stable_pan_projection'}
    from scipy.signal import stft, istft
    from scipy.ndimage import uniform_filter
    mixture = stft(source.T, fs=sample_rate, nperseg=2048, noverlap=1536, boundary='zeros')[2]
    estimate = stft(lead.T, fs=sample_rate, nperseg=2048, noverlap=1536, boundary='zeros')[2]
    mixture_mid = (mixture[0] + mixture[1]) * .5
    mixture_side = (mixture[0] - mixture[1]) * .5
    def smooth(value):
        return uniform_filter(value, size=(3, 31))
    cross = mixture_mid * np.conj(mixture_side)
    cross = smooth(cross.real) + 1j * smooth(cross.imag)
    side_power = smooth(np.abs(mixture_side) ** 2)
    mid_power = smooth(np.abs(mixture_mid) ** 2)
    coherence = np.clip(np.abs(cross) ** 2 / (side_power * mid_power + 1e-16), 0, 1)
    coefficient = cross / (side_power + 1e-12)
    reliable_pan = np.abs(coefficient) <= 4
    coefficient *= np.minimum(1., 4. / (np.abs(coefficient) + 1e-12))
    # Temporal coherence avoids assigning unrelated center notes to a side image.
    confidence = np.clip((coherence - .65) / .3, 0, 1)
    confidence *= reliable_pan
    confidence *= np.clip(side_power / (mid_power + side_power + 1e-12) / .04, 0, 1)
    confidence *= float(strength * center_confidence)
    spatial_mid = coefficient * mixture_side
    spatial_backing = np.stack([spatial_mid + mixture_side, spatial_mid - mixture_side])
    neural_backing = mixture - estimate
    fused_backing = neural_backing + confidence[None] * (spatial_backing - neural_backing)
    backing = istft(fused_backing, fs=sample_rate, nperseg=2048, noverlap=1536, boundary=True)[1].T[:len(source)]
    return (source - backing).astype(np.float32), {
        'spatial_center_confidence': float(center_confidence), 'spatial_blend_mean': float(confidence.mean()),
        'spatial_global_coherence': float(global_coherence), 'spatial_method': 'time_frequency_coherence'}


def fuse_vocals(source, first_lead, second_lead, secondary_weight=.5, suppression=0., sample_rate=44100, spatial_protection=.8):
    """Use aligned waveform averaging, with optional conservative backing masking.

    The shared stereo mask attenuates bins on which both models assign little
    energy to backing. All removed energy is reassigned to Main for consistency.
    """
    source = np.asarray(source, dtype=np.float32)
    first = np.asarray(first_lead, dtype=np.float32)
    second = np.asarray(second_lead, dtype=np.float32)
    if source.shape != first.shape or source.shape != second.shape or source.ndim != 2:
        raise ValueError('Ensemble requires identical sample and channel grids')
    if not all(np.isfinite(value).all() for value in (source, first, second)):
        raise ValueError('Nonfinite ensemble input')
    if not 0 <= secondary_weight <= 1 or not 0 <= suppression <= 1 or not 0 <= spatial_protection <= 1:
        raise ValueError('Ensemble controls must be between zero and one')
    lead = first * (1 - secondary_weight) + second * secondary_weight
    spatial_metadata = {'spatial_center_confidence': 0., 'spatial_blend_mean': 0.}
    if spatial_protection and source.shape[1] == 2 and len(source) >= 2048 and np.any(source[:, 0] != source[:, 1]):
        lead, spatial_metadata = protect_spatial_backing(source, lead, sample_rate, spatial_protection)
    backing = source - lead
    if suppression > 0 and len(source) >= 2048:
        from scipy.signal import stft, istft
        from scipy.ndimage import uniform_filter
        fft_size = 2048
        transforms = [stft(value.T, fs=sample_rate, nperseg=fft_size, noverlap=1536,
                           boundary='zeros', padded=True)[2] for value in (source, first, second)]
        mixture_spec, first_spec, second_spec = transforms
        residual_one = np.mean(np.abs(mixture_spec - first_spec) ** 2, axis=0)
        residual_two = np.mean(np.abs(mixture_spec - second_spec) ** 2, axis=0)
        lead_power = np.mean(np.abs((1 - secondary_weight) * first_spec + secondary_weight * second_spec) ** 2, axis=0)
        # max retains a backing bin supported by either model. Suppression only
        # acts where both residual estimates are weak relative to the lead.
        backing_power = np.maximum(residual_one, residual_two)
        confidence = backing_power / (backing_power + .1 * lead_power + 1e-12)
        confidence = np.clip(uniform_filter(confidence, size=(3, 3)), 0, 1)
        gain = 1 - suppression * (1 - confidence)
        residual_spec = stft(backing.T, fs=sample_rate, nperseg=fft_size, noverlap=1536, boundary='zeros', padded=True)[2]
        reconstructed = istft(residual_spec * gain[None], fs=sample_rate, nperseg=fft_size,
                              noverlap=1536, boundary=True)[1].T
        backing = reconstructed[:len(source)].astype(np.float32)
        lead = source - backing
    discrepancy = lead.astype(np.float64) + backing.astype(np.float64) - source
    return lead.astype(np.float32), backing.astype(np.float32), {
        'algorithm': 'dual_karaoke_shift_ensemble_v1', 'secondary_weight': secondary_weight,
        'backing_suppression': suppression, 'shared_stereo_mask': True,
        'spatial_protection': spatial_protection, **spatial_metadata,
        'reconstruction_max_abs': float(np.max(np.abs(discrepancy))),
        'reconstruction_rms': float(np.sqrt(np.mean(discrepancy ** 2))),
        'model_disagreement_rms': float(np.sqrt(np.mean((first.astype(np.float64) - second) ** 2))),
    }


def ensemble_manifests(section, parameters):
    primary_id = parameters.get('harmony_model_id') or section.get('default_harmony_model')
    models = [model for model in section.get('models', []) if model.get('task') == 'lead_backing']
    primary = next((model for model in models if model['id'] == primary_id), None)
    settings = parameters.get('harmony_settings', {})
    secondary_id = settings.get('secondary_model_id')
    secondary = next((model for model in models if model['id'] == secondary_id), None) if secondary_id else next(
        (model for model in models if primary and model['model_hash'] != primary['model_hash']), None)
    if not primary or not secondary or primary['model_hash'] == secondary['model_hash']:
        raise ValueError('增强和声分离需要两套不同权重的主唱/伴唱模型，请准备模型或选择单模型算法。')
    if primary['sample_rate'] != secondary['sample_rate']:
        raise ValueError('增强和声模型的工作采样率必须一致。')
    return primary, secondary
