"""Map the selected target F0's frame support onto the delivered audio timeline."""
import math
import numpy as np

DEFAULT_VOICING_SETTINGS = {'enabled': True, 'min_silence_ms': 0, 'fade_ms': 3}


def normalize_voicing_settings(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - set(DEFAULT_VOICING_SETTINGS):
        raise ValueError('无音高静音配置只能包含 enabled、min_silence_ms、fade_ms。')
    settings = {**DEFAULT_VOICING_SETTINGS, **value}
    if type(settings['enabled']) is not bool:
        raise ValueError('无音高静音开关必须为布尔值。')
    for key, maximum in [('min_silence_ms', 2000), ('fade_ms', 200)]:
        number = settings[key]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not 0 <= number <= maximum:
            raise ValueError(f'{key} 必须为 0–{maximum} 毫秒内的有限数值。')
    return settings


def mute_unpitched(audio, sample_rate, target, timeline_start_seconds, fade_ms=3, min_silence_ms=0, enabled=True):
    settings = normalize_voicing_settings({'enabled': enabled, 'min_silence_ms': min_silence_ms, 'fade_ms': fade_ms})
    metadata = {'kind': 'dsp', 'engine': 'target-f0-voicing-gate', 'version': 2, **settings,
                'total_samples': len(audio), 'timeline_start_seconds': timeline_start_seconds,
                'frame_interval': '[frame_center, next_frame_center)', 'outside_curve': 'silence'}
    if not enabled:
        return np.asarray(audio).copy(), {**metadata, 'unpitched_samples': 0, 'bypassed': True, 'outside_curve': 'preserved'}
    samples = np.asarray(audio, dtype=np.float32)
    f0 = np.asarray([0 if value is None else value for value in target['f0_hz']], dtype=np.float64)
    hop = float(target['hop'])
    pitch_rate = float(target['sample_rate'])
    center = float(target.get('frame_center_offset', 0))
    if hop <= 0 or pitch_rate <= 0 or sample_rate <= 0:
        raise ValueError('Invalid F0 or output timebase')
    if not np.isfinite(samples).all() or not np.isfinite(f0).all():
        raise ValueError('Nonfinite audio or target F0')
    voiced = f0 > 0
    if 'voiced' in target:
        declared = np.asarray(target['voiced'], dtype=bool)
        if declared.shape != voiced.shape:
            raise ValueError('F0 and voiced frame counts differ')
        voiced &= declared
    # Classify complete F0 gaps before cropping the audio. A short preview cut
    # inside a long gap must make the same decision as the full render.
    audible = voiced.copy()
    if min_silence_ms > 0:
        gap_edges = np.diff(np.r_[True, voiced, True].astype(np.int8))
        for first, end in zip(np.flatnonzero(gap_edges == -1), np.flatnonzero(gap_edges == 1)):
            if (end - first) * hop * 1000 < min_silence_ms * pitch_rate - 1e-7:
                audible[first:end] = True
    # The editor uses [frame tick, next frame tick) intervals. Work in runs,
    # avoiding an extra sample-sized time/index array for long songs.
    edges = np.diff(np.r_[False, audible, False].astype(np.int8))
    output = np.zeros_like(samples)
    fade_samples = max(0, round(sample_rate * fade_ms / 1000))
    voiced_samples = 0
    for first, end in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
        start_position = ((center + first * hop) / pitch_rate - timeline_start_seconds) * sample_rate
        end_position = ((center + end * hop) / pitch_rate - timeline_start_seconds) * sample_rate
        run_start = int(np.ceil(start_position - 1e-7))
        run_end = int(np.ceil(end_position - 1e-7))
        start = max(0, run_start)
        stop = min(len(samples), run_end)
        if stop <= start:
            continue
        output[start:stop] = samples[start:stop]
        voiced_samples += stop - start
        ramp_size = min(fade_samples, (run_end - run_start) // 2)
        if ramp_size:
            ramp = np.sin(np.linspace(0, np.pi / 2, ramp_size, endpoint=True)) ** 2
            if samples.ndim > 1:
                ramp = ramp[:, None]
            # Fade only at actual F0 boundaries. Cropping a preview inside a
            # voiced run must give the same samples as a full render crop.
            head_end = min(stop, run_start + ramp_size)
            if head_end > start:
                output[start:head_end] *= ramp[start - run_start:head_end - run_start]
            tail_start = max(start, run_end - ramp_size)
            if stop > tail_start:
                output[tail_start:stop] *= ramp[::-1][tail_start - (run_end - ramp_size):stop - (run_end - ramp_size)]
    return output, {**metadata, 'unpitched_samples': len(samples) - voiced_samples, 'bypassed': False}
