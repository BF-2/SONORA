"""Regression checks for reviewed state, recovery and untrusted-recipe boundaries.

All media, database rows and test doubles live in test_core's temporary DATA.
The two state/provenance unit tests deliberately do not execute or claim neural
inference; actual neural inference is covered by the separate worker suite.
"""
import copy
import importlib
import json
from pathlib import Path
import threading
import uuid

import numpy as np
import pytest
import soundfile as sf

from tests.test_core import AUTH, DATA, client, fixture_project, media, req, scheduler, store
from services.orchestrator.editing import compose
from services.orchestrator.store import now, uid


def _job(project, operation='export', parameters=None):
    job = dict(
        id=uid('review_job'), schema_version=1, revision=1,
        project_id=project['id'], project_revision=project['revision'],
        operation=operation, idempotency_key=uuid.uuid4().hex,
        state='queued', progress=None, progress_basis='unknown', stage='review test',
        message='test fixture', parameters=parameters or {}, input_artifact_id=None,
        artifact_ids=[], created_at=now(), updated_at=now(), error=None,
        attempts=1, project_snapshot=copy.deepcopy(project),
    )
    store.put('jobs', job)
    (DATA / 'jobs' / job['id']).mkdir(parents=True)
    return job


@pytest.mark.parametrize('candidate_id', ['../../escape', 'E:/voice/escape', r'..\escape'])
def test_recipe_rejects_filesystem_candidate_ids(candidate_id):
    project, artifact = fixture_project()
    recipe = req('GET', f"/projects/{project['id']}/recipe")
    recipe['project']['candidates'] = [{
        'id': candidate_id, 'source_artifact_id': artifact['id'],
        'artifact_id': artifact['id'], 'model_id': 'test-only',
        'generation_depth': 1,
    }]
    project_count = len(req('GET', '/projects')['projects'])
    response = client.post('/api/v1/projects/restore', headers=AUTH, files={
        'file': ('malicious-recipe.json', json.dumps(recipe).encode(), 'application/json'),
    })
    assert response.status_code == 400, response.text
    assert '标识' in response.text
    assert len(req('GET', '/projects')['projects']) == project_count


def test_restored_candidate_cannot_claim_fabricated_inference_provenance():
    project, artifact = fixture_project()
    recipe = req('GET', f"/projects/{project['id']}/recipe")
    recipe['project']['candidates'] = [{
        'id': 'syntactically_valid_candidate', 'source_artifact_id': artifact['id'],
        'artifact_id': artifact['id'], 'model_id': 'fabricated-model',
        'generation_depth': 999, 'state': 'succeeded',
    }]
    restored = req('POST', '/projects/restore', files={
        'file': ('forged-provenance.json', json.dumps(recipe).encode(), 'application/json'),
    })
    candidate = restored['candidates'][0]
    assert candidate['state'] != 'succeeded'
    assert candidate['generation_depth'] != 999
    assert candidate['model_id'] != 'fabricated-model'
    assert candidate['model_manifest_hash'] is None


def test_recipe_repeated_restore_does_not_nest_internal_restore_state():
    project, _ = fixture_project()
    for _ in range(4):
        recipe = req('GET', f"/projects/{project['id']}/recipe")
        assert 'restore_pending' not in recipe['project']
        project = req('POST', '/projects/restore', files={
            'file': ('roundtrip.json', json.dumps(recipe).encode(), 'application/json'),
        })


def test_comp_scratch_paths_ignore_untrusted_candidate_identifiers():
    project, artifact = fixture_project()
    victim = DATA / ('review-must-not-overwrite-' + uuid.uuid4().hex + '.wav')
    marker = b'original-user-bytes-must-survive'
    victim.write_bytes(marker)
    malicious_id = str(victim.with_suffix(''))
    candidates = [
        {'id': malicious_id, 'artifact_id': artifact['id'], 'source_artifact_id': artifact['id']},
        {'id': 'safe_alternate', 'artifact_id': artifact['id'], 'source_artifact_id': artifact['id']},
    ]
    folder = DATA / 'jobs' / uid('comp_review')
    folder.mkdir(parents=True)
    output, _, _ = compose(store, candidates, malicious_id, [
        {'candidate_id': 'safe_alternate', 'start_tick': 4800, 'end_tick': 48000, 'fade_ms': 20},
    ], folder, threading.Event())
    assert victim.read_bytes() == marker
    assert output.resolve().is_relative_to(folder.resolve())
    assert {p.name for p in folder.glob('comp-input-*.wav')} == {'comp-input-0.wav', 'comp-input-1.wav'}
    with sf.SoundFile(output) as audio:
        assert audio.samplerate == 48000
        assert len(audio) == round(artifact['sample_count'] * 48000 / artifact['sample_rate'])


def test_project_and_history_rollback_together(monkeypatch):
    project, _ = fixture_project()
    before = store.get('projects', project['id'])
    original_checkpoint = store._checkpoint

    def fail_after_history_write(db, value):
        original_checkpoint(db, value)
        raise RuntimeError('simulated failure after history insertion')

    with monkeypatch.context() as context:
        context.setattr(store, '_checkpoint', fail_after_history_write)
        with pytest.raises(RuntimeError, match='simulated failure'):
            store.change(project['id'], lambda p: p.update(name='must roll back'), revision=project['revision'])
    assert store.get('projects', project['id']) == before
    with store.connect() as db:
        position = db.execute('SELECT position FROM cursors WHERE project_id=?', (project['id'],)).fetchone()[0]
        checkpoint = json.loads(db.execute('SELECT body FROM history WHERE project_id=? AND position=?', (project['id'], position)).fetchone()[0])
    assert checkpoint == before


def test_export_uses_submission_snapshot_not_newer_live_mix():
    project, original = fixture_project()
    job = _job(project, parameters={'format': 'wav', 'name': 'review snapshot export'})
    newer_path = DATA / 'uploads' / (uid('newer_mix') + '.wav')
    sf.write(newer_path, np.zeros(48000, dtype=np.float32), 48000, subtype='FLOAT')
    newer = media.commit(store, newer_path, kind='dsp', role='mix', name='created after enqueue')
    store.change(project['id'], lambda p: p['assets'].append(newer))
    output = scheduler.export(job, threading.Event())
    assert output[0]['id'] == original['id']
    exported = store.get('projects', project['id'])['exports'][-1]
    assert exported['artifact_id'] == original['id']
    with sf.SoundFile(exported['path']) as audio:
        assert len(audio) == original['sample_count']


def test_completed_inference_stage_survives_downstream_failure():
    project, _ = fixture_project()
    job = _job(project, operation='pipeline')
    job['state'] = 'failed'
    store.put('jobs', job)
    # Metadata-only unit fixture; there is no generated model audio in this test.
    artifact = {
        'id': uid('test_only_inference_metadata'), 'processing_kind': 'model_inference',
        'stem_role': 'converted', 'model_hash': 'f' * 64, 'job_id': job['id'],
        'commit_state': 'committed', 'inference_stage_committed': True, 'input_artifact_ids': [],
    }
    assert scheduler.has_conversion(artifact)
    assert not scheduler.has_conversion({**artifact, 'commit_state': 'pending'})
    assert not scheduler.has_conversion({**artifact, 'inference_stage_committed': False})


def test_old_conversion_does_not_overwrite_intervening_track_edits(monkeypatch):
    project, source = fixture_project()
    project['parameters']['model_id'] = 'test-state-only-model'
    project = store.change(project['id'], lambda p: p.update(parameters=project['parameters']))
    job = _job(project, operation='convert', parameters=copy.deepcopy(project['parameters']))
    edited = copy.deepcopy(project['tracks'])
    edited[0].update(gain_db=-9, mute=False, offset_ticks=2400)
    store.change(project['id'], lambda p: p.update(tracks=edited))
    output_path = DATA / 'uploads' / (uid('state_fixture') + '.wav')
    sf.write(output_path, np.zeros(48000, dtype=np.float32), 48000, subtype='FLOAT')
    # DSP stub exists solely to exercise state commit; never labeled model_inference.
    output = media.commit(store, output_path, kind='dsp', role='converted', name='test-only state fixture')
    with monkeypatch.context() as context:
        context.setattr(scheduler, 'engine', lambda *args, **kwargs: [output])
        context.setattr(scheduler, 'model', lambda identifier: {'model_hash': '0' * 64})
        scheduler.convert(job, source, threading.Event(), pitch={'id': 'pitch_state_fixture', 'input_artifact_ids': [source['id']]})
    current = store.get('projects', project['id'])
    old_track = next(t for t in current['tracks'] if t['id'] == edited[0]['id'])
    assert old_track['gain_db'] == -9 and old_track['offset_ticks'] == 2400
    assert old_track['mute'] is False
    assert current['selected_candidate_id'] == project['selected_candidate_id']
    assert current['candidates'][-1]['selected'] is False


def test_relink_original_pcm_restores_track_settings(monkeypatch):
    app_module = importlib.import_module('services.orchestrator.app')
    from services.orchestrator.config import scoped_path
    original_path = DATA / 'uploads' / (uid('original_pcm') + '.wav')
    rng = np.random.default_rng(812 + int(uuid.uuid4().hex[:4], 16))
    sf.write(original_path, rng.uniform(-.1, .1, 22050).astype(np.float32), 22050, subtype='PCM_16')
    decoded_path = DATA / 'uploads' / (uid('expected_decode') + '.wav')
    media.decode(original_path, decoded_path)
    expected_hash = media.sha256(decoded_path)
    project = req('POST', '/projects', json={'name': 'relink regression'})
    recipe = req('GET', f"/projects/{project['id']}/recipe")
    recipe['project']['assets'] = [{
        'id': 'missing_original', 'sha256': expected_hash, 'original_hash': media.sha256(original_path),
        'name': 'source-pcm.wav', 'stem_role': 'original', 'processing_kind': 'decode',
    }]
    recipe['project']['tracks'] = [{
        'id': 'track_preserve', 'name': '原有轨道', 'role': 'original', 'artifact_id': 'missing_original',
        'gain_db': -3, 'pan': .25, 'mute': False, 'solo': False, 'offset_ticks': 1234, 'effects': [],
    }]
    restored = req('POST', '/projects/restore', files={
        'file': ('missing-media.json', json.dumps(recipe).encode(), 'application/json'),
    })
    assert restored['missing_media'] and not restored['tracks']
    with monkeypatch.context() as context:
        # This isolated test's explicit configured media root is its temporary DATA.
        context.setattr(app_module, 'scoped_path', lambda path: scoped_path(path, roots=[DATA]))
        relinked = req('POST', f"/projects/{restored['id']}/relink", json={'hash': expected_hash, 'path': str(original_path)})
    current = relinked['project']
    assert current['missing_media'] == []
    assert not any(w['code'] == 'missing_media' for w in current['warnings'])
    track = current['tracks'][0]
    assert track['gain_db'] == -3 and track['pan'] == .25 and track['offset_ticks'] == 1234
    assert track['id'] == 'track_preserve'
    assert relinked['artifact']['sha256'] == expected_hash
