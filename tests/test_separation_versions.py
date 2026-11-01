import copy
import threading
import pytest
import numpy as np
import soundfile as sf
from services.orchestrator import media
from services.orchestrator.separation_versions import register, adopt, change_run, list_runs
from services.orchestrator.store import Store


def project_with_source(tmp_path):
    store = Store(tmp_path / 'projects.sqlite')
    project = store.new_project('separation versions', 'cover')
    path = tmp_path / 'source.wav'
    sf.write(path, np.zeros((48000, 2), dtype=np.float32), 48000)
    source = media.commit(store, path, kind='decode', role='original', name='source')
    project['assets'].append(source)
    return project, source


def add_pair(project, source, operation, label):
    roles = ('vocal', 'instrumental') if operation == 'separate' else ('lead_vocal', 'backing_vocal')
    outputs = [{**source, 'id': f'{label}_{role}', 'stem_role': role, 'name': role,
                'input_artifact_ids': [source['id']]} for role in roles]
    job = {'id': f'job_{label}', 'operation': operation, 'parameters': {},
           'project_snapshot': copy.deepcopy(project), 'project_revision': project['revision'], 'input_artifact_id': source['id']}
    register(project, job, outputs)
    return project['separation_runs'][-1], outputs


def test_first_stage_selection_archive_restore_preserves_media(tmp_path):
    project, source = project_with_source(tmp_path)
    first, first_outputs = add_pair(project, source, 'separate', 'first')
    second, _ = add_pair(project, source, 'separate', 'second')
    assert project['selected_separations']['separate'] == second['id']
    with pytest.raises(ValueError, match='正在使用'):
        change_run(project, second['id'], 'archive')
    change_run(project, first['id'], 'archive')
    assert first['archived']
    change_run(project, first['id'], 'restore')
    adopt(project, first['id'])
    assert project['parameters']['source_artifact_id'] == first_outputs[0]['id']
    assert sum(not t['mute'] and t['role'] == 'instrumental' for t in project['tracks']) == 1
    assert len(project['assets']) == 5
    assert list_runs(project)[-1]['selected']


def test_harmony_switch_restores_independent_edits_and_no_duplicate_tracks(tmp_path):
    project, source = project_with_source(tmp_path)
    parent, vocals = add_pair(project, source, 'separate', 'parent')
    first, outputs = add_pair(project, vocals[0], 'separate_harmony', 'parts1')
    project['voice_parts']['lead_vocal']['pitch_edits'] = [{'id':'edit','start_tick':0,'end_tick':4800,'operation':'transpose','semitones':1}]
    project['voice_parts']['backing_vocal']['parameters']['transpose'] = -1
    project['voice_parts']['lead_vocal']['parameters']['pitch_artifact_id'] = 'pitch_old_source'
    second, _ = add_pair(project, vocals[0], 'separate_harmony', 'parts2')
    assert not project['voice_parts']['lead_vocal']['pitch_edits']
    assert not project['voice_parts']['lead_vocal']['parameters'].get('pitch_artifact_id')
    adopt(project, first['id'])
    assert project['voice_parts']['lead_vocal']['pitch_edits'][0]['semitones'] == 1
    assert project['voice_parts']['lead_vocal']['parameters']['pitch_artifact_id'] == 'pitch_old_source'
    assert project['voice_parts']['backing_vocal']['parameters']['transpose'] == -1
    for role in ('lead_vocal','backing_vocal'):
        assert sum(not t['mute'] and t['role'] == role for t in project['tracks']) == 1
    assert project['selected_separations']['separate'] == parent['id']
    change_run(project, second['id'], 'archive')
    assert all(output['id'] in {a['id'] for a in project['assets']} for output in outputs)


def test_upstream_switch_clears_old_routing_and_child_adoption_restores_parent(tmp_path):
    project, source = project_with_source(tmp_path)
    first, vocals = add_pair(project, source, 'separate', 'first')
    child, _ = add_pair(project, vocals[0], 'separate_harmony', 'child')
    second, _ = add_pair(project, source, 'separate', 'second')
    assert project['voice_parts'] == {}
    adopt(project, child['id'])
    assert project['selected_separations']['separate'] == first['id']
    assert project['selected_separations']['separate_harmony'] == child['id']
    assert not next(t for t in project['tracks'] if t['artifact_id'] == second['artifact_ids'][1])['solo']


def test_legacy_graph_read_is_nonmutating(tmp_path):
    project, source = project_with_source(tmp_path)
    run, outputs = add_pair(project, source, 'separate', 'legacy')
    project.pop('separation_runs'); project.pop('selected_separations')
    before = copy.deepcopy(project)
    listed = list_runs(project)
    assert len(listed) == 1 and listed[0]['selected']
    assert set(listed[0]['artifact_ids']) == {a['id'] for a in outputs}
    assert project == before


def test_voice_workflow_matches_shared_execution_device(tmp_path):
    from services.orchestrator.workflow import describe_workflow
    from services.orchestrator.voices import resolve_part
    from workers.rvc_parameters import pitch_analysis_parameters
    project, source = project_with_source(tmp_path)
    _, vocal = add_pair(project, source, 'separate', 'parent')
    _, parts = add_pair(project, vocal[0], 'separate_harmony', 'parts')
    project['parameters']['device'] = 'cuda:0'
    _, lead, effective = resolve_part(project, 'lead_vocal')
    pitch = {**lead, 'id':'pitch_fixture', 'stem_role':'pitch', 'voice_part':'lead_vocal',
             'input_artifact_ids':[lead['id']], 'analysis_parameters':pitch_analysis_parameters(effective)}
    project['assets'].append(pitch)
    assert describe_workflow(project, part='lead_vocal')['pitch_analysis']['state'] == 'ready'
    project['voice_parts']['lead_vocal']['parameters']['f0_min'] = 60
    assert describe_workflow(project, part='lead_vocal')['pitch_analysis']['state'] == 'stale'


def test_reverb_produces_wet_tail_and_preserves_full_duration(tmp_path):
    store = Store(tmp_path / 'mix.sqlite')
    signal = np.zeros((48000,2),dtype=np.float32);signal[1000] = .2
    path = tmp_path / 'impulse.wav';sf.write(path,signal,48000,subtype='FLOAT')
    source = media.commit(store,path,kind='decode',role='vocal')
    track = {'id':'track','artifact_id':source['id'],'role':'vocal','mute':False,'solo':False,'gain_db':0,'pan':0,'offset_ticks':0,
             'effects':[{'type':'reverb','enabled':True,'wet':.2,'room_seconds':.5,'predelay_ms':10}]}
    output = tmp_path / 'mixed.wav'
    media.mix(store,[track],output,threading.Event())
    rendered,rate = sf.read(output)
    assert rate == 48000 and len(rendered) == len(signal)
    assert np.max(np.abs(rendered[1100:30000])) > .00001
    assert np.isfinite(rendered).all()
