"""Isolated real-engine acceptance; API acceptance clicks are simulated, not listening.

Uses local prepared weights and a short local song excerpt. No production DB writes.
"""
import os
import sys
import json
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = Path(os.environ.get('SONORA_WORKFLOW_VERIFY_DIR', str(ROOT / 'runtime' / 'standard-workflow-acceptance')))
os.environ['SONORA_DATA'] = str(OUT / 'data')
os.environ['SONORA_SESSION_TOKEN'] = 'isolated-workflow-' + uuid.uuid4().hex

from fastapi.testclient import TestClient
import numpy as np
import soundfile as sf
from services.orchestrator.app import app, store, catalog
from services.orchestrator import media
from services.orchestrator.config import binary, TIMEBASE
from services.orchestrator.store import Store


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    source = Path(os.environ.get('SONORA_WORKFLOW_AUDIO', 'D:/phpstudy_pro/WWW/video/video/BREAK FROM THE LINE (feat. Taylor Louderman & Natalie Weiss) by Joey Contreras.mp3'))
    if not source.is_file():
        raise RuntimeError('Set SONORA_WORKFLOW_AUDIO to a local song with lead and backing vocals.')
    excerpt = OUT / 'input-12s.wav'
    media.run([binary('ffmpeg'), '-v', 'error', '-ss', '90', *media.safe_input(source), '-t', '12', '-ar', '48000', '-ac', '2', '-c:a', 'pcm_f32le', '-y', str(excerpt)])
    report = {'passed': False, 'human_listening': 'pending; acceptance actions below test state transitions only',
              'production_database_modified': False, 'source_sha256': media.sha256(excerpt), 'jobs': [], 'checks': []}
    try:
        with TestClient(app) as client:
            def api(method, path, **kwargs):
                response = client.request(method, '/api/v1' + path, headers={'Authorization': 'Bearer ' + os.environ['SONORA_SESSION_TOKEN']}, **kwargs)
                if response.status_code >= 400:
                    raise AssertionError(response.text)
                return response.json()

            p = api('POST', '/projects', json={'name': '标准流程 · 真实引擎技术验收（听感待人工）', 'task': 'cover'})
            pid = p['id']

            def current():
                return api('GET', '/projects/' + pid)

            def patch(changes):
                return api('PATCH', '/projects/' + pid, json={'revision': current()['revision'], 'patch': changes})

            def job(operation, parameters=None, source_id=None, expected='succeeded'):
                p = current()
                j = api('POST', f'/projects/{pid}/jobs', json={'revision': p['revision'], 'idempotency_key': uuid.uuid4().hex,
                    'operation': operation, 'input_artifact_id': source_id, 'parameters': parameters or {}})
                deadline = time.monotonic() + 1200
                while time.monotonic() < deadline:
                    j = api('GET', '/jobs/' + j['id'])
                    if j['state'] in ('succeeded', 'failed', 'blocked', 'cancelled'):
                        break
                    time.sleep(.3)
                report['jobs'].append({k: j.get(k) for k in ('id', 'operation', 'state', 'error', 'artifact_ids')})
                (OUT / 'progress.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                assert j['state'] == expected, j
                print('REAL_WORKFLOW', operation, j['state'], j['id'], flush=True)
                return j

            def pitch_action(action, source_id, version_id=None):
                return api('POST', f'/projects/{pid}/pitch-versions/{action}', json={'revision': current()['revision'],
                    'source_artifact_id': source_id, 'version_id': version_id})

            def review(candidate, action):
                return api('POST', f'/projects/{pid}/candidates/{candidate["id"]}/review', json={'revision': current()['revision'], 'action': action})

            p = api('POST', f'/projects/{pid}/import-path', json={'path': str(excerpt), 'role': 'original'})['project']
            job('separate', {'separation_settings': {'segment_size': 256, 'overlap_step_seconds': 2.5}}, p['assets'][0]['id'])
            voice = next(a for a in current()['assets'] if a['stem_role'] == 'vocal')
            job('separate_harmony', {'harmony_settings': {'segment_size': 256, 'overlap_step_seconds': 2}}, voice['id'])
            p = current()
            sources = {role: next(a for a in p['assets'] if a['stem_role'] == role) for role in ('instrumental', 'lead_vocal', 'backing_vocal')}
            hashes = {role: media.sha256(a['path']) for role, a in sources.items()}
            assert all(a['locked'] for a in sources.values())
            assert all(t['locked'] for t in p['tracks'] if t['artifact_id'] in {a['id'] for a in sources.values()})
            for part in ('lead_vocal', 'backing_vocal'):
                job('pitch', {'voice_part': part, 'pitch_algorithm': 'rmvpe'}, sources[part]['id'])
            follower = sources['backing_vocal']['id']
            versions = []
            for i, shift in enumerate((-2, 1, 3)):
                if i:
                    pitch_action('create', follower)
                p = patch({'voice_parts': {'backing_vocal': {'pitch_edits': [{'id': 'edit_' + str(i), 'start_tick': 0,
                    'end_tick': 12 * TIMEBASE, 'operation': 'transpose', 'semitones': shift, 'locked': False}]}}})
                versions.append(p['active_pitch_versions'][follower])
            assert len(set(versions)) == 3
            pitch_action('select', follower, versions[0])
            assert current()['voice_parts']['backing_vocal']['pitch_edits'][0]['semitones'] == -2
            pitch_action('select', follower, versions[2])
            report['checks'].append('two-level neural separation; three locked sources; three independent Follower F0 versions')
            models = api('GET', '/catalog/models')['models']
            model = next(m for m in models if m.get('provider') == 'haysay')
            manifest = catalog.resolve_model(model['id'])
            params = {'model_id': model['id'], 'index_id': manifest['index_id'], 'index_rate': .5,
                      'pitch_algorithm': 'rmvpe', 'full_source': True, 'ignore_clip_bindings': True}
            for part in ('lead_vocal', 'backing_vocal'):
                patch({'voice_parts': {part: {'parameters': params}}})
                job('convert', {'voice_part': part}, sources[part]['id'])
                p = current(); c = p['candidates'][-1]
                assert c['review_status'] == 'pending' and not c['selected'] and not c['preview_only']
                assert c['f0_version_id'] == p['active_pitch_versions'][sources[part]['id']]
                pitch = next(a for a in p['assets'] if a['id'] == c['pitch_artifact_id'])
                assert sources[part]['id'] in pitch['input_artifact_ids']
                assert c['requested_f0_hash'] and c['consumed_f0_hash'] and c['checkpoint_id']
                review(c, 'accept')
            separation_jobs = [j['id'] for j in report['jobs'] if j['operation'].startswith('separate')]
            # Worker failure preserves all sources, candidates and accepted finals.
            job('convert', {'voice_part': 'lead_vocal', 'model_id': 'missing-workflow-test-checkpoint'}, sources['lead_vocal']['id'], expected='failed')
            job('convert', {'voice_part': 'lead_vocal'}, sources['lead_vocal']['id'])
            rejected = current()['candidates'][-1]
            review(rejected, 'retry')
            patch({'voice_parts': {'lead_vocal': {'parameters': {'protect': .3}}}})
            job('convert', {'voice_part': 'lead_vocal'}, sources['lead_vocal']['id'])
            review(current()['candidates'][-1], 'accept')
            review(rejected, 'discard')
            p = current(); assert len(p['final_list']) == 3
            assert len(p['separation_runs']) == 2
            assert separation_jobs == [j['id'] for j in report['jobs'] if j['operation'].startswith('separate')]
            assert all(media.sha256(a['path']) == hashes[role] for role, a in sources.items())
            entry = p['final_list'][-1]
            patch({'track_updates': {entry['track_id']: {'name': 'Main 第二版 · 技术验收', 'gain_db': -9, 'pan': .2, 'offset_ticks': 2400}}})
            job('mix')
            p = current(); mixed = next(a for a in reversed(p['assets']) if a['stem_role'] == 'mix')
            assert mixed['sample_rate'] == 48000 and mixed['channels'] == 2 and mixed['sample_count'] == 12 * TIMEBASE + 2400
            assert len([t for t in mixed['mix_track_snapshot'] if t['role'] == 'instrumental']) == 1
            assert len(mixed['mix_track_snapshot']) == 4
            assert mixed['diagnostics']['sample_peak_dbfs'] <= -.99
            job('export', {'artifact_id': mixed['id'], 'format': 'wav', 'name': '标准流程技术验收_听感待确认'})
            p = current(); output = p['exports'][-1]
            info = sf.info(output['path']); assert info.frames == mixed['sample_count'] and info.channels == 2 and info.samplerate == 48000
            recipe = api('GET', f'/projects/{pid}/recipe')
            restored = api('POST', '/projects/restore', files={'file': ('project.json', json.dumps(recipe).encode(), 'application/json')})
            assert restored['final_list'] == p['final_list'] and restored['pitch_versions'] == p['pitch_versions']
            reopened = Store(store.path).get('projects', pid)
            assert reopened['final_list'] == p['final_list'] and reopened['active_pitch_versions'] == p['active_pitch_versions']
            report.update(passed=True, project_id=pid, restored_project_id=restored['id'], export=output, source_hashes=hashes,
                          final_list=p['final_list'], f0_versions=[{k: v[k] for k in ('id', 'name', 'revision', 'source_artifact_id')} for v in p['pitch_versions']])
            report['checks'] += ['Main/Follower consume their own F0; exact model/checkpoint lineage',
                'failed/rejected generation retries without separation; previous finals preserved',
                'three accepted entries; independent names/gain/pan/offset; -1dBFS protected stereo mix',
                'WAV export 48kHz stereo 12.05s; recipe and SQLite reopening preserve state']
    finally:
        (OUT / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('REAL_WORKFLOW_PASS', str(OUT / 'verification.json'), flush=True)


if __name__ == '__main__':
    main()
