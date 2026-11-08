"""Harmony integration regression tests with isolated synthetic DSP fixtures.

Engine stubs in this file exercise state transitions only. They never produce
model_inference artifacts and do not constitute neural separation/conversion QA.
"""
import copy
import importlib
import json
from pathlib import Path
import sys
import threading
import uuid

import numpy as np
import pytest
import soundfile as sf

from tests.test_core import AUTH, DATA, client, media, req, scheduler, store
from tests.test_review_regressions import _job
from services.orchestrator.store import uid
from services.orchestrator.voices import resolve_part, sync_part_tracks
from services.orchestrator.production import accept, routed_tracks
from workers.rvc_parameters import pitch_analysis_parameters,parameter_hash
from workers.pitch.engine import apply_target_parameters


def _import(project, role, frequency=220, payload=None):
    if payload is None:
        path = DATA / 'uploads' / (uid('harmony_fixture') + '.wav')
        sr = 16000
        signal = (.08 * np.sin(np.arange(sr) * 2 * np.pi * frequency / sr)).astype(np.float32)
        sf.write(path, signal, sr, subtype='PCM_16')
        payload = path.read_bytes()
    result = req('POST', f"/projects/{project['id']}/import", files={
        'file': (role + '.wav', payload, 'audio/wav'),
    }, data={'role': role})
    return result['project'], result['artifact'], payload


def _parts(identical=False):
    project = req('POST', '/projects', json={'name': 'Harmony state unit fixture', 'task': 'cover'})
    project, vocal, _ = _import(project, 'vocal', 175)
    project, lead, payload = _import(project, 'lead_vocal', 230)
    project, backing, _ = _import(project, 'backing_vocal', 350, payload if identical else None)
    project, instrumental, _ = _import(project, 'instrumental', 110)
    project = req('PATCH', f"/projects/{project['id']}", json={
        'revision': project['revision'],
        'patch': {'voice_parts': {
            'lead_vocal': {'parameters': {'model_id': 'unit-lead-model', 'transpose': 0}},
            'backing_vocal': {'parameters': {'model_id': 'unit-backing-model', 'transpose': -2}},
        }},
    })
    return project, {'vocal': vocal, 'lead_vocal': lead, 'backing_vocal': backing, 'instrumental': instrumental}


def _output(source, part, frequency=440):
    path = DATA / 'uploads' / (uid('harmony_state_output') + '.wav')
    sr = 16000
    sf.write(path, .06 * np.sin(np.arange(sr) * 2 * np.pi * frequency / sr), sr, subtype='FLOAT')
    return media.commit(store, path, kind='dsp', inputs=[source['id']], role='converted',
                        name='DSP fixture only, no model inference', extra={'voice_part': part})


def _candidate(project, source, part):
    output = _output(source, part, 440 if part == 'lead_vocal' else 660)
    candidate = {
        'id': uid('candidate'), 'name': 'Unit fixture ' + part, 'voice_part': part,
        'source_artifact_id': source['id'], 'part_source_artifact_id': source['id'],
        'artifact_id': output['id'], 'model_id': 'unit-model', 'model_manifest_hash': None,
        'generation_depth': 1, 'state': 'succeeded', 'selected': False, 'parameters': {}, 'fixture_kind': 'dsp_only',
    }
    def add(p):
        p['assets'].append(output)
        p['candidates'].append(candidate)
        p['tracks'].append({
            'id': uid('track'), 'candidate_id': candidate['id'], 'name': candidate['name'], 'role': 'converted', 'voice_part': part,
            'artifact_id': output['id'], 'gain_db': 0, 'pan': 0, 'mute': True,
            'solo': False, 'offset_ticks': 0, 'effects': [],
        })
        accept(p, candidate['id'])
    return store.change(project['id'], add), candidate


def _with_pitches(project, sources):
    artifacts = []
    for part in ('lead_vocal', 'backing_vocal'):
        path = DATA / 'uploads' / (uid('pitch_fixture') + '.json')
        path.write_text(json.dumps({'sample_rate': 16000, 'sample_count': 16000,
            'hop': 160, 'frame_center_offset': 0, 'f0_hz': [220] * 100,
            'voiced': [True] * 100}), encoding='utf-8')
        artifacts.append(media.commit(store, path, kind='dsp', inputs=[sources[part]['id']],
                                       role='pitch', extra={'voice_part': part,'worker_metadata':{'analysis_parameters':pitch_analysis_parameters({})}}))
    return store.change(project['id'], lambda p: p['assets'].extend(artifacts))


def test_part_patch_delta_preserves_sibling_and_accepted_finals():
    project, sources = _parts()
    project, lead_candidate = _candidate(project, sources['lead_vocal'], 'lead_vocal')
    project, backing_candidate = _candidate(project, sources['backing_vocal'], 'backing_vocal')
    before_backing = copy.deepcopy(project['voice_parts']['backing_vocal'])
    changed = req('PATCH', f"/projects/{project['id']}", json={'revision': project['revision'],
        'patch': {'voice_parts': {'lead_vocal': {'parameters': {'transpose': 3}}}}})
    assert changed['voice_parts']['lead_vocal']['parameters']['transpose'] == 3
    assert changed['voice_parts']['lead_vocal']['parameters']['model_id'] == 'unit-lead-model'
    assert changed['final_list'] == project['final_list']
    assert changed['voice_parts']['backing_vocal'] == before_backing
    lead_entry = next(f for f in changed['final_list'] if f['candidate_id'] == lead_candidate['id'])
    cleared = req('DELETE', f"/projects/{project['id']}/final-list/{lead_entry['id']}", json={'revision': changed['revision']})
    active = {t['artifact_id'] for t in routed_tracks(cleared) if not t['mute']}
    assert sources['lead_vocal']['id'] not in active
    assert backing_candidate['artifact_id'] in active
    assert sources['vocal']['id'] not in active


def test_cross_part_candidate_selection_is_rejected_without_mutation():
    project, sources = _parts()
    project, backing = _candidate(project, sources['backing_vocal'], 'backing_vocal')
    response = client.patch(f"/api/v1/projects/{project['id']}", headers=AUTH, json={
        'revision': project['revision'], 'patch': {'voice_parts': {'lead_vocal': {'selected_candidate_id': backing['id']}}},
    })
    assert response.status_code == 400
    assert req('GET', f"/projects/{project['id']}") == project


def test_empty_part_edits_never_inherit_global_model_or_pitch_edits():
    project, _ = _parts()
    project['parameters'].update(model_id='wrong-global-model', transpose=12, device='cpu')
    project['pitch_edits'] = [{'id': 'global-edit', 'operation': 'transpose', 'semitones': 12,
                               'start_tick': 0, 'end_tick': 48000}]
    _, _, resolved = resolve_part(project, 'lead_vocal')
    assert resolved['model_id'] == 'unit-lead-model'
    assert resolved['transpose'] == 0 and resolved['pitch_edits'] == []
    assert resolved['device'] == 'cpu'


@pytest.mark.parametrize('part', ['lead_vocal', 'backing_vocal'])
def test_routing_change_invalidates_mix_but_not_voice_inference(part):
    project, sources = _parts()
    project, accepted = _candidate(project, sources[part], part)
    final_track = project['final_list'][-1]['track_id']
    def graph(p):
        p['graph'] = [
            {'id': 'voice-node', 'type': 'convert', 'voice_part': 'lead_vocal', 'state': 'succeeded'},
            {'id': 'mix-node', 'type': 'mix', 'state': 'succeeded'},
            {'id': 'export-node', 'type': 'export', 'state': 'succeeded'},
        ]
    project = store.change(project['id'], graph)
    changed = req('PATCH', f"/projects/{project['id']}", json={'revision': project['revision'],
        'patch': {'track_updates': {final_track: {'mute': True}}}})
    assert next(n for n in changed['graph'] if n['id'] == 'voice-node')['state'] == 'succeeded'
    assert next(n for n in changed['graph'] if n['id'] == 'mix-node')['state'] == 'stale'
    assert next(n for n in changed['graph'] if n['id'] == 'export-node')['state'] == 'stale'


@pytest.mark.parametrize('edited_part', ['lead_vocal', 'backing_vocal'])
def test_inflight_lead_conversion_preserves_sibling_and_newer_own_edits(monkeypatch, edited_part):
    project, sources = _parts()
    project, backing = _candidate(project, sources['backing_vocal'], 'backing_vocal')
    _, source, params = resolve_part(project, 'lead_vocal')
    job = _job(project, operation='convert', parameters=params)
    changed = req('PATCH', f"/projects/{project['id']}", json={'revision': project['revision'],
        'patch': {'voice_parts': {edited_part: {'parameters': {'transpose': 7}}}}})
    output = _output(source, 'lead_vocal')
    with monkeypatch.context() as context:
        context.setattr(scheduler, 'engine', lambda *args, **kwargs: [output])
        context.setattr(scheduler, 'model', lambda _: {'model_hash': 'f' * 64})
        scheduler.convert(job, source, threading.Event(), pitch={'id': 'unit-pitch', 'input_artifact_ids': [source['id']]})
    current = req('GET', f"/projects/{project['id']}")
    assert current['voice_parts'][edited_part]['parameters']['transpose'] == 7
    assert any(f['candidate_id'] == backing['id'] for f in current['final_list'])
    assert next(t for t in current['tracks'] if t['artifact_id'] == backing['artifact_id'])['mute'] is False
    if edited_part == 'lead_vocal':
        assert current['voice_parts']['lead_vocal']['selected_candidate_id'] == changed['voice_parts']['lead_vocal']['selected_candidate_id']
        assert current['candidates'][-1]['selected'] is False
    else:
        assert current['voice_parts']['lead_vocal']['selected_candidate_id'] is None
        assert current['candidates'][-1]['review_status'] == 'pending'


@pytest.mark.parametrize('stop_kind', ['cancel', 'failure'])
def test_partial_batch_keeps_committed_lead_when_backing_stops(monkeypatch, stop_kind):
    project, sources = _parts()
    project = _with_pitches(project, sources)
    job = _job(project, operation='convert_parts', parameters={'parts': ['lead_vocal', 'backing_vocal']})
    token = threading.Event()
    real_convert = scheduler.convert
    def engine_stub(engine, job, source, cancel, pitch=None, parameters=None):
        assert engine == 'rvc'
        if parameters['voice_part'] == 'backing_vocal':
            raise RuntimeError('unit fixture: second voice failed')
        return [_output(source, 'lead_vocal')]
    def convert_and_cancel(*args, **kwargs):
        result = real_convert(*args, **kwargs)
        if stop_kind == 'cancel': token.set()
        return result
    with monkeypatch.context() as context:
        context.setattr(scheduler, 'engine', engine_stub)
        context.setattr(scheduler, 'model', lambda _: {'model_hash': 'f' * 64})
        context.setattr(scheduler, 'convert', convert_and_cancel)
        with pytest.raises(media.Cancelled if stop_kind == 'cancel' else RuntimeError):
            scheduler.convert_parts(job, token)
    current = req('GET', f"/projects/{project['id']}")
    assert len(current['candidates']) == 1
    assert current['candidates'][0]['voice_part'] == 'lead_vocal'
    assert current['voice_parts']['lead_vocal']['selected_candidate_id'] is None
    assert current['candidates'][0]['review_status'] == 'pending' and not current['final_list']
    assert current['voice_parts']['backing_vocal']['source_artifact_id'] == sources['backing_vocal']['id']
    assert current['voice_parts']['backing_vocal']['selected_candidate_id'] is None


def test_stale_separation_pair_does_not_discard_newer_voice_edits(monkeypatch):
    project, sources = _parts()
    job = _job(project, operation='separate_harmony')
    job['input_artifact_id'] = sources['vocal']['id']
    changed = req('PATCH', f"/projects/{project['id']}", json={'revision': project['revision'],
        'patch': {'voice_parts': {'lead_vocal': {'pitch_edits': [
            {'id': 'manual-during-render', 'operation': 'transpose', 'semitones': 5, 'start_tick': 0, 'end_tick': 24000},
        ]}}}})
    outputs = []
    for part in ('lead_vocal', 'backing_vocal'):
        artifact = _output(sources['vocal'], part, 540 if part == 'lead_vocal' else 720)
        artifact['stem_role'] = part
        store.put('artifacts', artifact)
        outputs.append(artifact)
    with monkeypatch.context() as context:
        context.setattr(scheduler, 'engine', lambda *args, **kwargs: outputs)
        scheduler.execute(job, threading.Event())
    current = req('GET', f"/projects/{project['id']}")
    assert current['voice_parts'] == changed['voice_parts']


@pytest.mark.parametrize('identical', [False, True])
def test_harmony_recipe_roundtrip_preserves_roles_and_part_controls(identical):
    project, sources = _parts(identical)
    project = req('PATCH', f"/projects/{project['id']}", json={'revision': project['revision'],
        'patch': {'voice_parts': {'lead_vocal': {'mode': 'original'}, 'backing_vocal': {'mode': 'mute'}}}})
    recipe = req('GET', f"/projects/{project['id']}/recipe")
    restored = req('POST', '/projects/restore', files={'file': ('harmony.json', json.dumps(recipe).encode(), 'application/json')})
    assert set(restored['voice_parts']) == {'lead_vocal', 'backing_vocal'}
    for part in ('lead_vocal', 'backing_vocal'):
        binding = restored['voice_parts'][part]
        assert binding['mode'] == project['voice_parts'][part]['mode']
        assert binding['parameters'] == project['voice_parts'][part]['parameters']
        artifact = next(a for a in restored['assets'] if a['id'] == binding['source_artifact_id'])
        assert artifact['stem_role'] == part
    if identical:
        assert sources['lead_vocal']['sha256'] == sources['backing_vocal']['sha256']
        assert restored['voice_parts']['lead_vocal']['source_artifact_id'] != restored['voice_parts']['backing_vocal']['source_artifact_id']


def test_pending_harmony_binding_survives_resave_then_original_relink(monkeypatch):
    project, _ = _parts()
    recipe = req('GET', f"/projects/{project['id']}/recipe")
    original = DATA / 'uploads' / (uid('missing_lead_original') + '.wav')
    sf.write(original, np.random.default_rng(82717).uniform(-.1, .1, 16013), 16000, subtype='PCM_16')
    decoded = DATA / 'uploads' / (uid('missing_lead_decoded') + '.wav')
    media.decode(original, decoded)
    digest = media.sha256(decoded)
    old_id = recipe['project']['voice_parts']['lead_vocal']['source_artifact_id']
    for artifact in recipe['project']['assets']:
        if artifact['id'] == old_id:
            artifact.update(sha256=digest, original_hash=media.sha256(original), name='missing lead.wav')
    recipe['project']['voice_parts']['lead_vocal'].update(mode='original')
    recipe['project']['voice_parts']['lead_vocal']['parameters']['transpose'] = 9
    first = req('POST', '/projects/restore', files={'file': ('missing-harmony.json', json.dumps(recipe).encode(), 'application/json')})
    resaved = req('GET', f"/projects/{first['id']}/recipe")
    second = req('POST', '/projects/restore', files={'file': ('resaved-harmony.json', json.dumps(resaved).encode(), 'application/json')})
    app_module = importlib.import_module('services.orchestrator.app')
    from services.orchestrator.config import scoped_path
    with monkeypatch.context() as context:
        context.setattr(app_module, 'scoped_path', lambda path: scoped_path(path, roots=[DATA]))
        linked = req('POST', f"/projects/{second['id']}/relink", json={'hash': digest, 'path': str(original)})['project']
    assert linked['voice_parts']['lead_vocal']['mode'] == 'original'
    assert linked['voice_parts']['lead_vocal']['parameters']['transpose'] == 9
    assert linked['voice_parts']['backing_vocal']['parameters'] == project['voice_parts']['backing_vocal']['parameters']
    assert not linked['missing_media']


def test_identical_source_cache_hit_creates_truthful_input_alias(monkeypatch):
    module = importlib.import_module('services.orchestrator.scheduler')
    project, sources = _parts()
    original_source = sources['lead_vocal']
    # Two references intentionally have identical audio bytes and equal time maps.
    replacement = {**copy.deepcopy(original_source), 'id': uid('artifact')}
    store.put('artifacts', replacement)
    def add_reference(p):
        p['assets'].append(replacement)
        p['tracks'].append({
            'id': uid('track'), 'name': 'same content new reference', 'role': 'lead_vocal',
            'voice_part': 'lead_vocal', 'artifact_id': replacement['id'], 'gain_db': 0,
            'pan': 0, 'mute': True, 'solo': False, 'offset_ticks': 0, 'effects': [],
        })
    project = store.change(project['id'], add_reference)
    root = DATA / uid('isolated_worker_fixture')
    (root / 'workers' / 'pitch').mkdir(parents=True)
    (root / 'workers' / 'pitch' / 'run.py').write_text('# isolated pitch protocol fixture\n', encoding='utf-8')
    calls = []
    def protocol_fixture(args, *unused, **kwargs):
        request = json.loads(Path(args[3]).read_text(encoding='utf-8'))
        calls.append(request)
        output = Path(request['output_dir']) / 'pitch.json'
        raw={'sample_rate': 16000, 'sample_count': 16000,
            'hop': 160, 'frame_center_offset': 0, 'f0_hz': [220] * 100,
            'voiced': [True] * 100, 'fixture_kind': 'not_neural_inference','analysis_parameters':pitch_analysis_parameters(request['parameters'])}
        raw['target_layer']=apply_target_parameters(raw,request['parameters'])
        output.write_text(json.dumps(raw), encoding='utf-8')
        Path(args[5]).write_text(json.dumps({'ok': True, 'outputs': [{
            'path': str(output), 'role': 'pitch', 'processing_kind': 'dsp',
            'engine': 'unit_pitch_protocol',
        }], 'metadata': {'fixture_kind': 'not_neural_inference','analysis_parameters':raw['analysis_parameters']}}), encoding='utf-8')
    params = {'voice_part': 'lead_vocal', 'pitch_algorithm': 'rmvpe'}
    first = _job(project, operation='pitch', parameters=params)
    second = _job(project, operation='pitch', parameters=params)
    with monkeypatch.context() as context:
        context.setattr(module, 'ROOT', root)
        context.setattr(module, 'resources', lambda: {'pitch': {'python': sys.executable}})
        context.setattr(media, 'run', protocol_fixture)
        detected = scheduler.engine('pitch', first, original_source, threading.Event())[0]
        aliased = scheduler.engine('pitch', second, replacement, threading.Event())[0]
    assert len(calls) == 1, 'Equivalent content should reuse its completed numerical analysis'
    assert aliased['id'] != detected['id']
    assert aliased['sha256'] == detected['sha256'] and aliased['path'] == detected['path']
    assert aliased['input_artifact_ids'] == [replacement['id']]
    assert aliased['job_id'] == detected['job_id'] == first['id']
    assert aliased['cache_source_artifact_id'] == detected['id']
    assert aliased['cache_consuming_job_id'] == second['id']
    assert detected['input_artifact_ids'] == [original_source['id']]
    scheduler.attach(second, [aliased], add_tracks=False)
    next_job = _job(store.get('projects', project['id']), operation='convert',
                    parameters={**params, 'pitch_artifact_id': aliased['id']})
    assert scheduler.choose_pitch(next_job, replacement)['id'] == aliased['id']


def test_short_preview_cannot_replace_whole_voice_part(monkeypatch):
    project, sources = _parts()
    _, source, params = resolve_part(project, 'lead_vocal', {'range_ticks': [12000, 24000]})
    job = _job(project, operation='convert', parameters=params)
    path = DATA / 'uploads' / (uid('short_preview_fixture') + '.wav')
    sf.write(path, .08 * np.sin(np.arange(4000) * 2 * np.pi * 440 / 16000), 16000, subtype='FLOAT')
    preview = media.commit(store, path, kind='dsp', inputs=[source['id']], role='converted',
                            name='unit short preview', extra={'voice_part': 'lead_vocal', 'timeline_offset_ticks': 12000})
    with monkeypatch.context() as context:
        context.setattr(scheduler, 'engine', lambda *args, **kwargs: [preview])
        context.setattr(scheduler, 'model', lambda _: {'model_hash': 'f' * 64})
        scheduler.convert(job, source, threading.Event(), pitch={'id': 'unit-pitch', 'input_artifact_ids': [source['id']]})
    current = req('GET', f"/projects/{project['id']}")
    candidate = current['candidates'][-1]
    assert current['voice_parts']['lead_vocal']['selected_candidate_id'] is None
    assert candidate['selected'] is False
    assert next(t for t in current['tracks'] if t['artifact_id'] == source['id'])['mute'] is False
    response = client.patch(f"/api/v1/projects/{project['id']}", headers=AUTH, json={
        'revision': current['revision'],
        'patch': {'voice_parts': {'lead_vocal': {'selected_candidate_id': candidate['id']}}},
    })
    assert response.status_code == 400, 'A short audition cannot silently mute the rest of a full vocal performance'


def test_missing_selected_candidate_restores_audible_source_fallback():
    project, sources = _parts()
    project, candidate = _candidate(project, sources['lead_vocal'], 'lead_vocal')
    recipe = req('GET', f"/projects/{project['id']}/recipe")
    for artifact in recipe['project']['assets']:
        if artifact['id'] == candidate['artifact_id']:
            artifact['sha256'] = '0' * 64  # Explicit unavailable-media reference in this test recipe.
    restored = req('POST', '/projects/restore', files={
        'file': ('missing-candidate.json', json.dumps(recipe).encode(), 'application/json'),
    })
    binding = restored['voice_parts']['lead_vocal']
    assert binding['mode'] == 'convert' and binding['selected_candidate_id'] is None
    raw = next(t for t in restored['tracks'] if t['artifact_id'] == binding['source_artifact_id'])
    assert raw['mute'] is False, 'Clearing a missing selected candidate must reconcile its raw fallback routing'


def test_recipe_keeps_intentional_saved_mute_when_selection_still_exists():
    project, sources = _parts()
    project, candidate = _candidate(project, sources['lead_vocal'], 'lead_vocal')
    tracks = copy.deepcopy(project['tracks'])
    for track in tracks:
        if track['artifact_id'] == candidate['artifact_id']: track['mute'] = True
    project = req('PATCH', f"/projects/{project['id']}", json={
        'revision': project['revision'], 'patch': {'tracks': tracks},
    })
    recipe = req('GET', f"/projects/{project['id']}/recipe")
    restored = req('POST', '/projects/restore', files={
        'file': ('intentionally-muted.json', json.dumps(recipe).encode(), 'application/json'),
    })
    restored_candidate = next(c for c in restored['candidates'] if c['id'] == candidate['id'])
    assert restored_candidate['state'] == 'unverified_restored_media'  # DSP fixture cannot regain neural provenance.
    assert not restored['final_list']
    assert next(t for t in restored['tracks'] if t['artifact_id'] == restored_candidate['artifact_id'])['mute'] is True


def test_cache_pair_reselection_never_enables_duplicate_raw_tracks(monkeypatch):
    project, sources = _parts()
    job = _job(project, operation='separate_harmony')
    job['input_artifact_id'] = sources['vocal']['id']
    with monkeypatch.context() as context:
        context.setattr(scheduler, 'engine', lambda *args, **kwargs: [sources['lead_vocal'], sources['backing_vocal']])
        scheduler.execute(job, threading.Event())
    current = req('GET', f"/projects/{project['id']}")
    for part in ('lead_vocal', 'backing_vocal'):
        active = [t for t in routed_tracks(current) if t.get('voice_part', t['role']) == part and not t['mute']]
        assert not active, 'Locked source stems never bypass acceptance into the mix'


def test_cached_candidates_are_only_mixed_after_individual_acceptance(monkeypatch):
    project, sources = _parts()
    output = _output(sources['lead_vocal'], 'lead_vocal')
    with monkeypatch.context() as context:
        context.setattr(scheduler, 'engine', lambda *args, **kwargs: [output])
        context.setattr(scheduler, 'model', lambda _: {'model_hash': 'f' * 64})
        for _ in range(2):
            current = req('GET', f"/projects/{project['id']}")
            _, source, params = resolve_part(current, 'lead_vocal')
            job = _job(current, operation='convert', parameters=params)
            scheduler.convert(job, source, threading.Event(), pitch={'id': 'unit-pitch', 'input_artifact_ids': [source['id']]})
    current = req('GET', f"/projects/{project['id']}")
    assert len(current['candidates']) == 2
    assert not [t for t in routed_tracks(current) if t.get('voice_part') == 'lead_vocal']
    first, second = current['candidates']
    current = store.change(current['id'], lambda p: accept(p, first['id']))
    current = store.change(current['id'], lambda p: accept(p, first['id']))
    assert len(current['final_list']) == 1
    current = store.change(current['id'], lambda p: accept(p, second['id']))
    assert len(current['final_list']) == 2  # Deliberately accepted two versions of the same source.


def test_recipe_roundtrip_does_not_revalidate_mix_from_before_fader_edit():
    project, _ = _parts()
    mixing = _job(project, operation='mix')
    scheduler.mix_job(mixing, threading.Event())
    project = req('GET', f"/projects/{project['id']}")
    edited = req('PATCH', f"/projects/{project['id']}", json={
        'revision': project['revision'],
        'patch': {'track_updates': {project['background_track_id']: {'gain_db': -6}}},
    })
    assert any(n['state'] == 'stale' for n in edited['graph'] if n['type'] == 'mix')
    recipe = req('GET', f"/projects/{project['id']}/recipe")
    restored = req('POST', '/projects/restore', files={
        'file': ('pending-mix.json', json.dumps(recipe).encode(), 'application/json'),
    })
    exporting = _job(restored, operation='export', parameters={'format': 'wav', 'name': 'stale mix must not export as current'})
    with pytest.raises(ValueError):
        scheduler.export(exporting, threading.Event())
