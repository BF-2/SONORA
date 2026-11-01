"""Behavior checks for exact pitch targets, scoped separation and safe archives."""
import copy
import json
import pytest
from services.orchestrator.candidate_archive import archive_older
from workers.separation.options import separation_options

def candidate(i, **changes):
    return dict(id=f'candidate-{i}', artifact_id=f'audio-{i}', source_artifact_id='source', generation_depth=1, **changes)

def test_archive_retains_two_and_protects_usage_favorites_and_restored():
    p={'candidates':[candidate(i) for i in range(9)], 'selected_candidate_id':'candidate-0',
       'tracks':[{'artifact_id':'audio-1','mute':False}], 'parameters':{}, 'voice_parts':{}}
    p['candidates'][2]['favorite']=True;p['candidates'][3]['archive_retained']=True
    assert archive_older(p)==['candidate-4','candidate-5','candidate-6']
    assert len(p['candidates'])==9
    assert not archive_older(p)

def test_archive_scopes_parts_and_preserves_new_comparison_batch():
    p={'candidates':[candidate(i,voice_part='lead_vocal',comparison_id='batch' if i>1 else None) for i in range(6)] +
       [candidate(10,voice_part='backing_vocal')], 'tracks':[]}
    assert archive_older(p)==['candidate-0','candidate-1']

def test_archive_only_on_new_success_commit_and_can_be_undone(tmp_path):
    from services.orchestrator.store import Store
    store=Store(tmp_path/'test.sqlite');p=store.new_project('archive')
    p=store.change(p['id'],lambda p:p['candidates'].extend(candidate(i) for i in range(3)))
    assert not any(c.get('archived') for c in p['candidates'])  # restore/import is not generation
    p=store.change(p['id'],lambda p:p['candidates'].append(candidate(3)),auto_archive=True)
    assert [c['id'] for c in p['candidates'] if c.get('archived')]==['candidate-0','candidate-1']
    p=store.history(p['id'],'undo',p['revision'])
    assert len(p['candidates'])==3 and not any(c.get('archived') for c in p['candidates'])
    p=store.change(p['id'],lambda p:p['view_preferences'].update(auto_archive_candidates=False))
    p=store.change(p['id'],lambda p:p['candidates'].append(candidate(4)),auto_archive=True)
    assert not any(c.get('archived') for c in p['candidates'])

def test_separation_settings_are_scoped_and_fractional_step_survives():
    params={'separation_settings':{'segment_size':256},'harmony_settings':{'segment_size':801,'overlap_step_seconds':.75,'separation_algorithm':'tta','primary_gain':.9}}
    assert separation_options(params)['segment_size']==256
    options=separation_options({**params,'separation_task':'lead_backing'})
    assert options['segment_size']==801 and options['overlap_step_seconds']==.75 and options['primary_gain']==.9

@pytest.mark.parametrize('bad',[{'segment_size':256.5},{'overlap_step_seconds':0},{'primary_gain':float('nan')},{'separation_algorithm':'unknown'},{'cpu_threads':True}])
def test_separation_rejects_invalid_controls(bad):
    with pytest.raises(ValueError):separation_options({'harmony_settings':bad,'separation_task':'lead_backing'})

def test_pitch_preview_matches_render_target_with_offset_notes_filter_and_lock():
    from tests.test_core import fixture_project,req,store,media,DATA
    from workers.pitch.engine import apply_target_parameters,shift_pitch_to_project
    from services.orchestrator.editing import note_edits
    p,source=fixture_project()
    raw={'sample_rate':16000,'sample_count':16000,'hop':160,'frame_center_offset':0,'f0_hz':[220.,230.,None,440.,250.],
         'voiced':[True,True,False,True,True],'curve_layer':'detected'}
    path=DATA/'uploads'/'exact-preview.json';path.write_text(json.dumps(raw),encoding='utf-8')
    pitch=media.commit(store,path,kind='dsp',inputs=[source['id']],role='pitch')
    def add(p):p['assets'].append(pitch);p['tracks'][0]['offset_ticks']=96000
    p=store.change(p['id'],add)
    params={'transpose':2,'filter_radius':1,'f0_autotune':True,'f0_autotune_strength':.7}
    edits=[{'id':'e','start_tick':96000,'end_tick':96960,'operation':'transpose','semitones':1},
           {'id':'lock','start_tick':97440,'end_tick':97920,'operation':'set_hz','hz':330,'locked':True,
            'snapshot':{'sample_rate':16000,'hop':160,'frame_center_offset':32480,'f0_hz':[330.]}}]
    notes=[{'id':'n','start':96000,'end':98400,'pitch_midi_float':60,'reference_source':'manual'}]
    before=copy.deepcopy(store.get('projects',p['id']))
    target=req('POST',f"/projects/{p['id']}/pitch-target",json={'pitch_artifact_id':pitch['id'],'source_artifact_id':source['id'],
        'parameters':params,'pitch_edits':edits,'notes':notes})
    actual=apply_target_parameters(shift_pitch_to_project(raw,96000),{**params,'pitch_edits':note_edits(notes)+edits})
    assert target['f0_hz']==actual['f0_hz'] and target['target_hash']==actual['target_hash']
    assert target['timeline_offset_ticks']==0 and target['f0_hz'][2] is None and target['f0_hz'][3]==330
    assert store.get('projects',p['id'])==before
