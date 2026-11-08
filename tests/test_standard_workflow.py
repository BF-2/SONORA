"""Production-state integration with declared DSP fixtures and real FFmpeg mixing.

Neural execution is covered separately by scripts/verify_standard_workflow.py.
"""
import copy
import json
import threading
import uuid
from pathlib import Path
import numpy as np
import pytest
import soundfile as sf
from tests.test_core import fixture_project, store, scheduler, media, DATA, req, client, AUTH
from tests.test_review_regressions import _job
from services.orchestrator.production import active_version, accept, routed_tracks
from services.orchestrator.store import Store
from workers.rvc_parameters import pitch_analysis_parameters, parameter_hash


def audio_fixture(source, role, gain=1):
    x, sr = sf.read(source['path'], always_2d=True)
    path = DATA / 'uploads' / (uuid.uuid4().hex + '.wav')
    sf.write(path, x * gain, sr, subtype='FLOAT')
    return media.commit(store, path, kind='dsp', inputs=[source['id']], role=role,
                        name='DSP fixture ' + role, extra={'fixture_kind': 'synthetic_signal'})


def separated_project():
    p, raw = fixture_project()
    voice = audio_fixture(raw, 'vocal', .8)
    background = audio_fixture(raw, 'instrumental', .2)
    scheduler.attach(_job(p, operation='separate'), [voice, background])
    p = store.get('projects', p['id'])
    main = audio_fixture(voice, 'lead_vocal', .6)
    follower = audio_fixture(voice, 'backing_vocal', .4)
    scheduler.attach(_job(p, operation='separate_harmony'), [main, follower])
    return store.get('projects', p['id']), raw, main, follower


def patch_project(p, changes):
    p = store.get('projects', p['id'])
    return req('PATCH', '/projects/' + p['id'], json={'revision': p['revision'], 'patch': changes})


def pitch_action(p, source, action, identifier=None):
    p = store.get('projects', p['id'])
    return req('POST', f'/projects/{p["id"]}/pitch-versions/{action}', json={'revision': p['revision'],
        'source_artifact_id': source['id'], 'version_id': identifier})


def pitch_fixture(p, source, part):
    count = round(source['sample_count'] / source['sample_rate'] * 16000)
    frames = (count + 159) // 160
    f0 = [220. if i % 10 < 7 else None for i in range(frames)]
    params = pitch_analysis_parameters({})
    data = {'sample_rate': 16000, 'sample_count': count, 'hop': 160, 'frame_center_offset': 0,
            'f0_hz': f0, 'voiced': [f is not None for f in f0], 'analysis_parameters': params,
            'analysis_parameters_hash': parameter_hash(params), 'estimator': 'declared-dsp-fixture'}
    path = DATA / 'uploads' / (uuid.uuid4().hex + '.json')
    path.write_text(json.dumps(data), encoding='utf-8')
    artifact = media.commit(store, path, kind='dsp', inputs=[source['id']], role='pitch',
                            extra={'voice_part': part, 'analysis_parameters': params})
    scheduler.attach(_job(p, operation='pitch', parameters={'voice_part': part}), [artifact], False)
    return artifact


def declared_candidate(p, source, name='DSP-only test candidate'):
    """Fixture for testing acceptance/routing; never claim a neural inference."""
    output = audio_fixture(source, 'converted')
    def add(q):
        q['assets'].append(output)
        q['candidates'].append({'id': 'candidate_' + uuid.uuid4().hex, 'name': name,
            'source_artifact_id': source['id'], 'artifact_id': output['id'], 'generation_depth': 1,
            'state': 'succeeded', 'review_status': 'pending', 'selected': False, 'model_id': 'test-dsp-only',
            'voice_part': source.get('stem_role') if source.get('stem_role') in ('lead_vocal', 'backing_vocal') else None,
            'parameters': {}, 'notes': '', 'fixture_kind': 'synthetic_signal'})
    p = store.change(p['id'], add)
    return p, p['candidates'][-1]


def review(p, c, action):
    p = store.get('projects', p['id'])
    return req('POST', f'/projects/{p["id"]}/candidates/{c["id"]}/review', json={'revision': p['revision'], 'action': action})


def test_two_levels_lock_and_three_f0_versions_are_independent_and_undoable():
    p, raw, main, follower = separated_project()
    assert len(p['separation_runs']) == 2
    assert {t['role'] for t in p['tracks'] if t.get('locked')} >= {'instrumental', 'lead_vocal', 'backing_vocal'}
    before = {a['id']: media.sha256(a['path']) for a in (raw, main, follower)}
    pitch_fixture(p, main, 'lead_vocal')
    p = store.get('projects', p['id']); pitch_fixture(p, follower, 'backing_vocal')
    versions = []
    for i, shift in enumerate((-3, 2, 5)):
        if i:
            p = pitch_action(p, follower, 'create')
        p = patch_project(p, {'voice_parts': {'backing_vocal': {'pitch_edits': [{'id': 'edit_' + str(i),
            'start_tick': 0, 'end_tick': 48000, 'operation': 'transpose', 'semitones': shift, 'locked': False}]}}})
        versions.append(p['active_pitch_versions'][follower['id']])
    assert len(set(versions)) == 3
    assert len([v for v in p['pitch_versions'] if v['source_artifact_id'] == main['id']]) == 1
    assert p['voice_parts']['lead_vocal']['pitch_edits'] == []
    for identifier, shift in zip(versions, (-3, 2, 5)):
        p = pitch_action(p, follower, 'select', identifier)
        assert p['voice_parts']['backing_vocal']['pitch_edits'][0]['semitones'] == shift
    p = pitch_action(p, follower, 'undo', versions[-1])
    assert p['voice_parts']['backing_vocal']['pitch_edits'][0]['semitones'] == 2
    p = pitch_action(p, follower, 'redo', versions[-1])
    assert p['voice_parts']['backing_vocal']['pitch_edits'][0]['semitones'] == 5
    assert all(media.sha256(a['path']) == before[a['id']] for a in (raw, main, follower))
    bad = client.post(f'/api/v1/projects/{p["id"]}/pitch-versions/select', headers=AUTH,
        json={'revision': p['revision'], 'source_artifact_id': main['id'], 'version_id': versions[-1]})
    assert bad.status_code == 400
    altered = copy.deepcopy(p['tracks']); altered[0]['artifact_id'] = main['id']
    assert client.patch(f'/api/v1/projects/{p["id"]}', headers=AUTH, json={'revision': p['revision'], 'patch': {'tracks': altered}}).status_code == 400


def test_main_follower_generation_pins_correct_curve_and_requires_manual_acceptance(monkeypatch):
    p, _, main, follower = separated_project()
    for part, source, shift in [('lead_vocal', main, 1), ('backing_vocal', follower, -3)]:
        p = patch_project(p, {'voice_parts': {part: {'parameters': {'transpose': shift}}}})
        pitch_fixture(p, source, part)
    seen = []
    def engine(name, job, source, cancel, pitch=None, parameters=None):
        assert name == 'rvc', 'Conversion must not separate again or re-extract pinned F0.'
        target = json.loads(Path(pitch['path']).read_text())['target_layer']
        assert target['f0_hz'][8] is None  # preserve unvoiced frames
        assert source['id'] in pitch['input_artifact_ids']
        assert parameters['f0_version_id'] == active_version(job['project_snapshot'], source['id'])['id']
        seen.append((source['id'], target['f0_hz'][0], parameters['f0_version_id']))
        if parameters['voice_part']=='lead_vocal':
            def switch_focus(current):
                current['parameters']['active_voice_part']='backing_vocal'
                current['view_preferences']['generation_step']='input'
            store.change(job['project_id'],switch_focus,history=False)
        output = audio_fixture(source, 'converted')
        output.update(model_snapshot={'model_hash': 'a' * 64, 'model_artifact_id': 'checkpoint_test', 'epoch': 10},
                      requested_f0_hash=target['target_hash'], consumed_f0_hash=target['target_hash'])
        store.put('artifacts', output)
        return [output]
    monkeypatch.setattr(scheduler, 'engine', engine)
    for part, source in [('lead_vocal', main), ('backing_vocal', follower)]:
        p = store.get('projects', p['id'])
        params = {**p['voice_parts'][part]['parameters'], 'voice_part': part, 'model_id': 'test-dsp-only'}
        scheduler.convert(_job(p, operation='convert', parameters=params), source, threading.Event())
        p = store.get('projects', p['id']); candidate = p['candidates'][-1]
        assert candidate['review_status'] == 'pending' and not candidate['selected']
        assert candidate['checkpoint_id'] == 'checkpoint_test'
        assert candidate['f0_version_id'] == p['active_pitch_versions'][source['id']]
        assert not p['final_list']
        if part=='lead_vocal':assert p['view_preferences']['generation_step']=='input'
    assert seen[0][1] == pytest.approx(220 * 2 ** (1 / 12))
    assert seen[1][1] == pytest.approx(220 * 2 ** (-3 / 12))
    assert seen[0][2] != seen[1][2]


def test_accept_retry_discard_multiple_finals_and_mixed_rate_timeline(tmp_path):
    p, _, main, follower = separated_project()
    separation_ids = [r['id'] for r in p['separation_runs']]
    for source in (main, follower, main):
        p, c = declared_candidate(p, source)
        p = review(p, c, 'accept')
    assert len(p['final_list']) == 3 and len(routed_tracks(p)) == 4
    p = review(p, c, 'accept'); assert len(p['final_list']) == 3  # idempotent acceptance
    p, rejected = declared_candidate(p, follower)
    p = review(p, rejected, 'retry')
    assert p['view_preferences']['generation_step'] == 'edit'
    p = review(p, rejected, 'discard')
    assert p['candidates'][-1]['review_status'] == 'discarded'
    assert len(p['final_list']) == 3 and [r['id'] for r in p['separation_runs']] == separation_ids
    ids = [f['track_id'] for f in p['final_list']]
    p = patch_project(p, {'track_updates': {ids[0]: {'gain_db': -6, 'pan': -.5, 'offset_ticks': 4800, 'name': 'Main A'},
                                        ids[1]: {'gain_db': 24}, ids[2]: {'mute': True}}})
    job = _job(p, operation='mix')
    output = scheduler.mix_job(job, threading.Event())[0]
    assert output['sample_rate'] == 48000 and output['channels'] == 2
    assert output['sample_count'] == round(main['sample_count'] * 48000 / main['sample_rate']) + 4800
    assert output['diagnostics']['sample_peak_dbfs'] <= -.99
    assert len([t for t in output['mix_track_snapshot'] if t['role'] == 'instrumental']) == 1
    before = store.get('projects', p['id'])
    p = patch_project(p, {'voice_parts': {'lead_vocal': {'parameters': {'transpose': 6}}}})
    assert next(n for n in p['graph'] if output['id'] in n['artifact_refs'])['state'] == 'succeeded'
    scheduler.export(_job(p, operation='export', parameters={'artifact_id': output['id'], 'format': 'wav'}), threading.Event())
    p = store.get('projects', p['id']); export = p['exports'][-1]
    info = sf.info(export['path']); assert (info.frames, info.channels, info.samplerate) == (output['sample_count'], 2, 48000)
    loaded = Store(store.path).get('projects', p['id'])
    assert loaded['final_list'] == p['final_list'] and loaded['pitch_versions'] == p['pitch_versions']
    p = req('DELETE', f'/projects/{p["id"]}/final-list/{p["final_list"][0]["id"]}', json={'revision': p['revision']})
    assert len(p['final_list']) == 2
    assert media.validate_artifact(main).is_file()


def test_dry_input_switch_restores_own_edits_and_does_not_inherit_another_source():
    p, first = fixture_project()
    p = patch_project(p, {'parameters': {**p['parameters'], 'transpose': 5},
                          'pitch_edits': [{'id': 'edit_first', 'start_tick': 0, 'end_tick': 4800, 'operation': 'set_hz', 'hz': 330}]})
    second = audio_fixture(first, 'guide')
    from services.orchestrator.app import attach_import
    p = attach_import(p['id'], second)['project']
    p = patch_project(p, {'parameters': {**p['parameters'], 'source_artifact_id': second['id']}})
    assert p['parameters']['transpose'] == 0 and p['pitch_edits'] == []
    p = patch_project(p, {'parameters': {**p['parameters'], 'transpose': -2}})
    p = patch_project(p, {'parameters': {**p['parameters'], 'source_artifact_id': first['id']}})
    assert p['parameters']['transpose'] == 5 and p['pitch_edits'][0]['hz'] == 330
    assert p['active_pitch_versions'][first['id']] != p['active_pitch_versions'][second['id']]


def test_pipeline_and_unaccepted_export_cannot_bypass_human_review():
    p, source = fixture_project()
    body = {'revision': p['revision'], 'idempotency_key': uuid.uuid4().hex, 'operation': 'pipeline'}
    assert client.post(f'/api/v1/projects/{p["id"]}/jobs', headers=AUTH, json=body).status_code == 400
    p, c = declared_candidate(p, source)
    with pytest.raises(ValueError, match='人工接受'):
        scheduler.export(_job(p, operation='export', parameters={'artifact_id': c['artifact_id']}), threading.Event())
    assert not p['final_list'] and not routed_tracks(p)


def test_rewrite_guide_can_join_finals_with_existing_main_follower_sources():
    p, raw, main, follower = separated_project()
    p=patch_project(p,{'task':'rewrite'})
    guide=audio_fixture(raw,'guide')
    from services.orchestrator.app import attach_import
    p=attach_import(p['id'],guide)['project']
    p=patch_project(p,{'parameters':{**p['parameters'],'active_voice_part':'vocal','source_artifact_id':guide['id']}})
    assert p['pitch_edits']==[] and p['parameters']['source_artifact_id']==guide['id']
    p,c=declared_candidate(p,guide)
    p=review(p,c,'accept')
    assert p['final_list'][0]['candidate_id']==c['id']
    assert {t['role'] for t in routed_tracks(p)}=={'instrumental','converted'}
    assert p['voice_parts']['lead_vocal']['source_artifact_id']==main['id']
    assert p['voice_parts']['backing_vocal']['source_artifact_id']==follower['id']


def test_interruption_preserves_committed_state_and_marks_inflight_jobs_failed(tmp_path):
    from services.orchestrator.scheduler import Scheduler
    local = Store(tmp_path / 'restart.sqlite')
    p = local.new_project('restart')
    p = local.change(p['id'], lambda q: q['warnings'].append({'message': 'completed stage evidence'}))
    local.put('jobs', {'id': 'interrupted', 'project_id': p['id'], 'idempotency_key': 'interrupt', 'state': 'running'})
    Scheduler(local)
    assert local.get('jobs', 'interrupted')['error']['code'] == 'interrupted'
    assert local.get('projects', p['id']) == p
