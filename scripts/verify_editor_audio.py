"""Real API acceptance for erased-F0 silence and cached dual-model separation."""
import json
import os
import sys
import time
import uuid
from pathlib import Path
import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'runtime/editor-followup'
DATA = ROOT / 'runtime/standard-workflow-acceptance/data'
os.environ['SONORA_DATA'] = str(DATA)
os.environ['SONORA_SESSION_TOKEN'] = uuid.uuid4().hex
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
from services.orchestrator.app import app, store
from services.orchestrator import media
from workers.pitch.voicing import mute_unpitched, normalize_voicing_settings


def main():
    browser = json.loads((OUT / 'browser-verification.json').read_text(encoding='utf-8'))
    assert browser['passed']
    with TestClient(app) as client:
        def api(method, path, **kwargs):
            response = client.request(method, '/api/v1' + path, headers={'Authorization': 'Bearer ' + os.environ['SONORA_SESSION_TOKEN']}, **kwargs)
            assert response.status_code < 400, response.text
            return response.json()
        recipe = OUT / 'browser-project.sonora.json'
        restored = api('POST', '/projects/restore', files={'file': ('audio-test.json', recipe.read_bytes(), 'application/json')})
        pid = restored['id']
        def current():return api('GET', '/projects/' + pid)
        def run(operation, source, params):
            job = api('POST', f'/projects/{pid}/jobs', json={'revision': current()['revision'], 'idempotency_key': uuid.uuid4().hex,
                'operation': operation, 'input_artifact_id': source, 'parameters': params})
            for _ in range(1200):
                job = api('GET', '/jobs/' + job['id'])
                if job['state'] in ('succeeded', 'failed', 'blocked', 'cancelled'):break
                time.sleep(.25)
            assert job['state'] == 'succeeded', job.get('error')
            print(operation, 'PASS', job['id'], flush=True)
            return job
        p = current(); binding = p['voice_parts']['backing_vocal']
        source = next(a for a in p['assets'] if a['id'] == binding['source_artifact_id'])
        before = media.sha256(source['path'])
        job = run('convert', source['id'], {'voice_part': 'backing_vocal', 'full_source': True})
        candidate = current()['candidates'][-1]
        artifact = store.get('artifacts', candidate['artifact_id'])
        audio, rate = sf.read(artifact['path'], always_2d=True)
        target = json.loads((DATA / 'jobs' / job['id'] / 'rvc-backing_vocal/requested-f0.json').read_text(encoding='utf-8'))
        settings = normalize_voicing_settings(binding['parameters'].get('voicing_settings'))
        mask, gate = mute_unpitched(np.ones_like(audio), rate, target, artifact.get('timeline_offset_ticks', 0) / 48000, **settings)
        assert np.any(mask == 0) and np.max(np.abs(audio[mask == 0])) == 0
        assert np.max(np.abs(audio)) > .001
        first, end = [round(tick * rate / 48000) for tick in browser['erase_ticks']]
        assert np.max(np.abs(audio[first:end])) == 0
        assert len(audio) == round(source['sample_count'] * rate / source['sample_rate'])
        assert candidate['f0_version_id'] == p['active_pitch_versions'][source['id']]
        assert any(item['engine'] == 'target-f0-voicing-gate' for item in artifact['worker_metadata']['processing_chain'])
        assert media.sha256(source['path']) == before
        api('POST', f'/projects/{pid}/pitch-versions/create', json={'revision': current()['revision'], 'source_artifact_id': source['id']})
        off_settings = {**settings, 'enabled': False}
        api('PATCH', f'/projects/{pid}', json={'revision': current()['revision'], 'patch': {
            'voice_parts': {'backing_vocal': {'parameters': {'voicing_settings': off_settings}}}}})
        off_job = run('convert', source['id'], {'voice_part': 'backing_vocal', 'full_source': True})
        off_candidate = current()['candidates'][-1]
        off_artifact = store.get('artifacts', off_candidate['artifact_id'])
        off_audio, off_rate = sf.read(off_artifact['path'], always_2d=True)
        assert off_rate == rate and off_audio.shape == audio.shape
        assert off_candidate['requested_f0_hash'] == candidate['requested_f0_hash']
        assert off_candidate['f0_version_id'] != candidate['f0_version_id']
        assert off_candidate['parameters']['voicing_settings'] == off_settings
        assert np.max(np.abs(off_audio[first:end])) > 1e-7
        assert off_artifact['sha256'] != artifact['sha256']
        repeat_job = run('convert', source['id'], {'voice_part': 'backing_vocal', 'full_source': True})
        repeated = store.get('artifacts', current()['candidates'][-1]['artifact_id'])
        assert repeated['sha256'] == off_artifact['sha256']
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM events WHERE json_extract(body,'$.type')='cache.hit' AND json_extract(body,'$.job_id')=?", (repeat_job['id'],)).fetchone()[0] > 0
        report = {'passed': True, 'project_id': pid, 'rvc_job_id': job['id'], 'candidate_id': candidate['id'],
            'audio_path': artifact['path'], 'sample_rate': rate, 'frames': len(audio), 'erase_ticks': browser['erase_ticks'],
            'erased_max_abs': 0, 'eligible_unpitched_max_abs': 0, 'gate': gate, 'source_hash_preserved': True,
            'off_job_id': off_job['id'], 'off_settings': off_settings, 'off_erased_region_peak': float(np.max(np.abs(off_audio[first:end]))),
            'independent_edit_gate_verified': True, 'rvc_cache_separates_gate_settings': True, 'identical_gate_cache_hit': True}
        voice = next(a for a in p['assets'] if a['stem_role'] == 'vocal')
        params = {'harmony_settings': {'segment_size': 256, 'overlap_step_seconds': 1, 'separation_algorithm': 'ensemble', 'spatial_protection': .8}}
        first_job = run('separate_harmony', voice['id'], params)
        first_artifact = store.get('artifacts', first_job['artifact_ids'][0])
        assert len(first_artifact['worker_metadata']['ensemble_models']) == 2
        second_job = run('separate_harmony', voice['id'], params)
        assert first_job['artifact_ids'] == second_job['artifact_ids']
        with store.connect() as db:
            hit = db.execute("SELECT COUNT(*) FROM events WHERE json_extract(body,'$.type')='cache.hit' AND json_extract(body,'$.job_id')=?", (second_job['id'],)).fetchone()[0]
        assert hit > 0
        report.update(ensemble_api=True, second_separation_cache_hit=True, separation_jobs=[first_job['id'], second_job['id']],
            ensemble_models=first_artifact['worker_metadata']['ensemble_models'])
        (OUT / 'audio-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':main()
