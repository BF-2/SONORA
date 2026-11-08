"""Compare real single/dual Karaoke inference using local audio and known stems."""
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]

OUT = ROOT / 'runtime/editor-followup/harmony'
CONFIG = json.loads((ROOT / 'engine-envs/resources.json').read_text(encoding='utf-8'))


def run(source, name, algorithm):
    directory = OUT / name
    directory.mkdir(parents=True, exist_ok=True)
    request = {'protocol_version': 1, 'job_id': name, 'input_path': str(source), 'output_dir': str(directory),
        'resource_config_path': str(ROOT / 'engine-envs/resources.json'),
        'parameters': {'separation_task': 'lead_backing', 'harmony_model_id': CONFIG['separation']['default_harmony_model'],
            'device': 'cuda:0', 'harmony_settings': {'segment_size': 256, 'overlap_step_seconds': 1,
                'separation_algorithm': algorithm, 'secondary_weight': .5, 'backing_suppression': 0}}}
    (directory / 'request.json').write_text(json.dumps(request), encoding='utf-8')
    scratch=directory/'temp'; scratch.mkdir(exist_ok=True)
    numba=OUT/'numba-cache'; numba.mkdir(exist_ok=True)
    with (directory / 'worker.log').open('wb') as log:
        result = subprocess.run([CONFIG['separation']['python'], str(ROOT / 'workers/separation/run.py'),
            '--request', str(directory / 'request.json'), '--response', str(directory / 'response.json')],
            cwd=ROOT, env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                          'NUMBA_CACHE_DIR':str(numba),'TEMP':str(scratch),'TMP':str(scratch)},
            stdout=log, stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=900)
    response = json.loads((directory / 'response.json').read_text(encoding='utf-8'))
    assert result.returncode == 0 and response.get('ok'), response.get('error')
    print(name, 'PASS', response['metadata']['elapsed_seconds'], flush=True)
    return response


def sdr(actual, reference):
    error = np.sum((actual.astype(np.float64) - reference) ** 2)
    return float(10 * np.log10((np.sum(reference ** 2) + 1e-12) / (error + 1e-12)))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    report = {'passed': False, 'comparisons': [], 'listening_review': 'pending'}
    verification = json.loads((ROOT / 'runtime/standard-workflow-acceptance/verification.json').read_text(encoding='utf-8'))
    with sqlite3.connect(ROOT / 'runtime/standard-workflow-acceptance/data/sonora.sqlite') as db:
        p = json.loads(db.execute('SELECT body FROM projects WHERE id=?', (verification['project_id'],)).fetchone()[0])
    song = next(Path(a['path']) for a in p['assets'] if a['stem_role'] == 'vocal')
    one, sr = sf.read(ROOT / 'tests/results/workers/svs-kirakira/synthesized-vocal.wav', always_2d=True, dtype='float32')
    two, other_sr = sf.read(ROOT / 'tests/results/workers/svs-harukana/synthesized-vocal.wav', always_2d=True, dtype='float32')
    assert sr == other_sr == 44100
    lead_mono = one.mean(axis=1)
    backing_mono = np.interp(np.arange(len(lead_mono)) * 1.5, np.arange(len(two)), two.mean(axis=1), right=0)
    fixtures = [('song', song, None, None)]
    for name, pans in [('centered', (1., 1.)), ('wide', (.4, 1.))]:
        lead = np.column_stack([lead_mono, lead_mono]) * .7
        backing = np.column_stack([backing_mono * pans[0], backing_mono * pans[1]]) * .35
        path = OUT / (name + '-known-mixture.wav')
        sf.write(path, lead + backing, sr, subtype='FLOAT')
        sf.write(OUT / (name + '-reference-main.wav'), lead, sr, subtype='FLOAT')
        sf.write(OUT / (name + '-reference-follower.wav'), backing, sr, subtype='FLOAT')
        fixtures.append((name, path, lead, backing))
    for name, source, reference_lead, reference_backing in fixtures:
        original, rate = sf.read(source, always_2d=True)
        pair = {}
        for algorithm in ('single', 'ensemble'):
            response = run(source, name + '-' + algorithm, algorithm)
            lead, out_sr = sf.read(OUT / (name + '-' + algorithm) / 'lead_vocal.wav', always_2d=True)
            backing, _ = sf.read(OUT / (name + '-' + algorithm) / 'backing_vocal.wav', always_2d=True)
            assert out_sr == rate and original.shape == lead.shape == backing.shape
            residual = np.max(np.abs(original - lead - backing))
            assert residual < 1e-6 and np.isfinite(lead).all() and np.isfinite(backing).all()
            if algorithm == 'ensemble':
                assert response['metadata']['inference_passes'] == 6
                assert len({m['model_hash'] for m in response['metadata']['ensemble_models']}) == 2
            pair[algorithm] = {'seconds': response['metadata']['elapsed_seconds'], 'reconstruction_max_abs': float(residual),
                               'metadata': response['metadata']}
            if reference_backing is not None:
                pair[algorithm].update(main_sdr_db=sdr(lead, reference_lead), follower_sdr_db=sdr(backing, reference_backing))
        report['comparisons'].append({'fixture': name, 'results': pair})
        (OUT / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    measured = [c['results']['ensemble']['follower_sdr_db'] - c['results']['single']['follower_sdr_db']
                for c in report['comparisons'] if c['fixture'] != 'song']
    report.update(passed=True, known_fixture_mean_follower_sdr_improvement_db=float(np.mean(measured)),
                  metric_scope='Two known-stem synthetic singing mixtures; song clip has no ground truth.')
    (OUT / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('FOLLOWER_SDR_DELTA_DB', measured, flush=True)


if __name__ == '__main__':
    main()
