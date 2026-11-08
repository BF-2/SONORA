"""Deterministic offline vocal restoration and stereo room DSP (48 kHz).

Run in a cancellable child process. No database access, model loading, or downloads.
"""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
import numpy as np
import soundfile as sf
from scipy import signal, ndimage, interpolate

VERSION = 'vocal-space-1.1'
SR = 48000
NFFT = 4096
HOP = 480
EPS = 1e-12


def number(value, low, high):
    if isinstance(value, bool): raise ValueError('效果参数必须为数值。')
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'效果参数必须在 {low}–{high} 范围内。')
    return value


def native_effect(fx):
    return fx.get('type') in ('vocal_restore', 'vocal_space') or (fx.get('type') == 'reverb' and fx.get('algorithm', 'legacy') == 'diffuse-v1')


def settings(fx):
    typ = fx.get('type')
    if typ == 'vocal_restore':
        return {'strength': number(fx.get('strength', 100), 0, 150)}
    if typ not in ('vocal_space', 'reverb'): raise ValueError('未知空间效果。')
    if typ == 'reverb' and fx.get('algorithm', 'legacy') not in ('legacy', 'diffuse-v1'):
        raise ValueError('未知混响算法版本。')
    is_c = typ == 'vocal_space'
    return {
        'wet': (10 ** (-21 / 20) * number(fx.get('strength', 100), 0, 350) / 100 if is_c else number(fx.get('wet', .15), 0, .5)),
        'room_seconds': number(fx.get('room_seconds', .4 if is_c else .65), .1, 3),
        'predelay_ms': number(fx.get('predelay_ms', 18), 0, 100),
        'width': number(fx.get('width', 1), 0, 1.5),
        'lowcut_hz': number(fx.get('lowcut_hz', 220), 40, 1200),
        'damping_hz': number(fx.get('damping_hz', 6200), 1500, 16000),
        'ducking': number(fx.get('ducking', .3), 0, .8),
    }


def read48(path):
    x, sr = sf.read(path, dtype='float32', always_2d=True)
    if not np.isfinite(x).all(): raise ValueError('效果输入包含非有限采样。')
    if sr != SR:
        g = math.gcd(sr, SR); x = signal.resample_poly(x, SR // g, sr // g, axis=0)
    return x


def smooth(x, samples):
    return ndimage.uniform_filter1d(x, max(1, int(samples)), mode='nearest')


def room(x, fx):
    p = settings(fx)
    if p['wet'] == 0 or not np.any(x): return x.copy(), {'bypassed': True}
    # Keep the direct stereo signal intact; crossfeed only the reflection input.
    count = int(SR * (p['room_seconds'] * 1.5 + p['predelay_ms'] / 1000 + .032))
    tt = np.arange(count) / SR; predelay = p['predelay_ms'] / 1000
    rng = np.random.default_rng(7305); wet = []
    for ch in range(2):
        ir = rng.normal(0, 1, count) * np.exp(-6.9078 * np.maximum(tt - predelay - .022, 0) / p['room_seconds'])
        ir *= np.clip((tt - predelay - .007) / .04, 0, 1)
        ir = signal.sosfilt(signal.butter(2, [p['lowcut_hz'], p['damping_hz']], btype='bandpass', fs=SR, output='sos'), ir)
        ir *= .45 / np.sqrt(np.sum(ir ** 2) + EPS)
        for ms, amp in zip(([5, 19, 41, 61] if ch == 0 else [11, 25, 49, 71]), [.50, .32, .22, .14]):
            idx = int((ms + p['predelay_ms']) * SR / 1000)
            if idx < count: ir[idx] += amp
        drive = .85 * x[:, ch] + .15 * x[:, 1 - ch]
        wet.append(signal.oaconvolve(drive, ir)[:len(x)])
    wet = np.stack(wet, axis=1)
    wet *= np.sqrt(np.mean(x.astype(float) ** 2) / (np.mean(wet ** 2) + EPS)) * p['wet']
    mid = wet.mean(axis=1); side = (wet[:, 0] - wet[:, 1]) * .5 * p['width']
    wet = np.column_stack([mid + side, mid - side])
    # Stereo-linked detector avoids image pumping and responds to anti-phase input.
    env = np.sqrt(smooth(np.mean(x ** 2, axis=1), 4801) + EPS)
    duck = 1 - p['ducking'] * np.clip(env / (np.percentile(env, 85) + EPS), 0, 1)
    wet *= duck[:, None]
    y = x + wet
    # Constant RMS compensation prevents a louder preset masquerading as quality.
    gain = np.sqrt(np.mean(x.astype(float) ** 2) / (np.mean(y ** 2) + EPS))
    return (y * gain).astype(np.float32), {'settings': p, 'level_compensation_db': float(20 * np.log10(gain + EPS)), 'tail_policy': 'preserve source duration'}


def spectral_blocks(x, frames):
    padded = np.pad(x, (NFFT // 2, NFFT + NFFT // 2))
    win = signal.windows.hann(NFFT, sym=False).astype(np.float32)
    for start in range(0, frames, 1500):
        end = min(frames, start + 1500)
        segment = padded[start * HOP:(end - 1) * HOP + NFFT]
        windows = np.lib.stride_tricks.sliding_window_view(segment, NFFT)[::HOP]
        yield start, end, np.fft.rfft(windows * win, axis=1).T


def restore(x, reference, pitch, offset, strength=100, pitch_offset=None):
    if strength == 0 or not np.any(x): return x.copy(), {'bypassed': True}
    n = len(x); frames = math.ceil(n / HOP) + 1; times = np.arange(frames) * HOP / SR
    ref = np.zeros(n, dtype=np.float32); pos = round(offset * SR)
    src_start = max(0, pos); dst_start = max(0, -pos); count = min(n - dst_start, len(reference) - src_start)
    if count > 0: ref[dst_start:dst_start + count] = reference[src_start:src_start + count].mean(axis=1)
    target = pitch.get('target_layer') or pitch
    raw = np.nan_to_num(np.asarray(target['f0_hz'], dtype=float))
    pt = (np.arange(len(raw)) * target['hop'] + target.get('frame_center_offset', 0)) / target['sample_rate']
    # Target F0 is on the project/source timeline, while audio is asset-relative.
    pitch_origin = pitch_offset if pitch.get('target_layer') and pitch_offset is not None else offset
    f0 = np.interp(times + pitch_origin, pt, raw, left=0, right=0)
    eg = np.zeros(frames); es = np.zeros(frames); diffs = np.zeros((4, frames))
    f = np.fft.rfftfreq(NFFT, 1 / SR)
    ranges = [(180, 350), (350, 700), (700, 1500), (1500, 3500)]
    for (start, end, G), (_, _, S) in zip(spectral_blocks(x.mean(axis=1), frames), spectral_blocks(ref, frames)):
        P = abs(G) ** 2; Q = abs(S) ** 2
        eg[start:end] = 10 * np.log10(P.sum(axis=0) + EPS); es[start:end] = 10 * np.log10(Q.sum(axis=0) + EPS)
        for k, (lo, hi) in enumerate(ranges):
            ix = (f >= lo) & (f < hi)
            diffs[k, start:end] = 10 * np.log10(Q[ix].sum(axis=0) + EPS) - 10 * np.log10(P[ix].sum(axis=0) + EPS) - (es[start:end] - eg[start:end])
    active = (f0 > 60) & (eg > eg.max() - 32) & (es > es.max() - 40)
    if active.sum() < 10: raise ValueError('人声厚度修复没有足够的对齐有声参考，请使用原转换候选。')
    med = np.median(diffs[:, active], axis=1)
    anchors = np.clip(med * [.60, .50, .30, .30], [-1, -1, -1.2, -1.5], [4, 2, 1, 1])
    hz = np.array([1, 70, 120, 240, 480, 1000, 2200, 4200, 8000, 24000.])
    base = interpolate.PchipInterpolator(np.log(hz), [0, 0, anchors[0] * .2, *anchors, 0, 0, 0])(np.log(np.maximum(f, 1)))
    gate = ndimage.gaussian_filter1d(active.astype(float), 2.5)
    dynamic = np.clip(ndimage.gaussian_filter1d(diffs[0] - med[0], 12) * .20, -1, 1)
    lowshape = np.exp(-.5 * (np.log2(np.maximum(f, 1) / 270) / .65) ** 2)
    win = signal.windows.hann(NFFT, sym=False).astype(np.float32)
    output = np.zeros((frames * HOP + NFFT, 2), dtype=np.float32); norm = np.zeros(len(output), dtype=np.float32)
    for ch in range(2):
        for start, end, G in spectral_blocks(x[:, ch], frames):
            pitch_local = f0[None, start:end]
            floor = 1 / (1 + np.exp(np.clip(-(f[:, None] - .72 * pitch_local) / (25 + .10 * pitch_local), -40, 40)))
            gain = np.clip((base[:, None] + lowshape[:, None] * dynamic[None, start:end]) * floor * gate[None, start:end], -2, 5)
            frames_out = np.fft.irfft(G * 10 ** (gain * strength / 100 / 20), n=NFFT, axis=0).T * win
            for k, frame in enumerate(frames_out, start):
                output[k * HOP:k * HOP + NFFT, ch] += frame
                if ch == 0: norm[k * HOP:k * HOP + NFFT] += win ** 2
    tone = (output / np.maximum(norm[:, None], 1e-8))[NFFT // 2:NFFT // 2 + n]
    et = np.zeros(frames)
    for start, end, T in spectral_blocks(tone.mean(axis=1), frames): et[start:end] = 10 * np.log10((abs(T) ** 2).sum(axis=0) + EPS)
    delta = ndimage.gaussian_filter1d(es - et, 18)
    delta = np.clip(.45 * (delta - np.median(delta[active])), -2, 2) * gate * strength / 100
    tone *= np.interp(np.arange(n) / SR, times, 10 ** (delta / 20))[:, None]
    silent = smooth((np.max(abs(x), axis=1) < 1e-10).astype(np.float32), 4801) > .99999
    tone[silent] = 0
    scale = np.sqrt(np.mean(x.astype(float) ** 2) / (np.mean(tone.astype(float) ** 2) + EPS))
    return (tone * scale).astype(np.float32), {'anchors_db': anchors.tolist(), 'strength': strength, 'reference_audio_mixed': False, 'formant_sharpening': False, 'level_compensation_db': float(20 * np.log10(scale + EPS))}


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--request', required=True); args = parser.parse_args()
    req = json.loads(Path(args.request).read_text(encoding='utf-8'))
    x = read48(req['input']); fx = req['effect']; p = settings(fx)
    if x.shape[1] != 2: raise ValueError('混音效果输入必须已转换为立体声。')
    if fx['type'] == 'vocal_restore':
        ref = read48(req['reference']); pitch = json.loads(Path(req['pitch']).read_text(encoding='utf-8'))
        y, info = restore(x, ref, pitch, req.get('reference_offset_seconds', 0), p['strength'], req.get('pitch_offset_seconds'))
    else: y, info = room(x, fx)
    if len(y) != len(x) or not np.isfinite(y).all(): raise ValueError('效果处理输出无效。')
    sf.write(req['output'], y, SR, subtype='FLOAT')
    Path(req['output'] + '.json').write_text(json.dumps({'version': VERSION, **info}, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__': main()
