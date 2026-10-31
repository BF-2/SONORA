"""Targeted specialist-patch integration checks, using declared DSP state fixtures.

No neural model is loaded. Synthetic candidate audio is processing_kind=dsp;
the comparison tests inspect wiring, not acoustic quality or neural execution.
"""
import copy
import importlib
import json
from pathlib import Path
import sqlite3
import sys
import threading
import uuid
import pytest

@pytest.mark.parametrize('user_changed',[False,True])
def test_pipeline_own_upstream_refresh_does_not_masquerade_as_user_edit(monkeypatch,user_changed):
    from tests.test_core import fixture_project,store,scheduler,media,DATA
    from tests.test_review_regressions import _job
    from services.orchestrator.store import uid
    import soundfile as sf
    import numpy as np
    project,source=fixture_project()
    def sound(role):
        path=DATA/'uploads'/(uid('pipeline_state')+'.wav');sf.write(path,np.ones(source['sample_count'])*.03,source['sample_rate'],subtype='FLOAT')
        return media.commit(store,path,kind='dsp',inputs=[source['id']],role=role,name='DSP state fixture '+role)
    old_vocal,old_backing=sound('vocal'),sound('instrumental')
    previous=_job(project,operation='separate');scheduler.attach(previous,[old_vocal,old_backing])
    project=store.change(project['id'],lambda p:p.update(task='cover',parameters={**p['parameters'],'model_id':'state-only-model'}))
    job=_job(project,operation='pipeline',parameters=copy.deepcopy(project['parameters']))
    vocal,backing=sound('vocal'),sound('instrumental');converted=sound('converted')
    pitch={'id':uid('pitch'),'stem_role':'pitch','media_type':'application/json','input_artifact_ids':[vocal['id']]}
    # Store an actual bounded JSON analysis fixture; no model output is claimed.
    path=DATA/'uploads'/(uid('pitch')+'.json');path.write_text(json.dumps({'sample_rate':16000,'sample_count':16000,'hop':160,'frame_center_offset':0,'f0_hz':[220.]*100,'voiced':[True]*100}),encoding='utf-8')
    pitch=media.commit(store,path,kind='dsp',inputs=[vocal['id']],role='pitch')
    def engine(name,*args,**kwargs):
        if name=='separation':
            if user_changed:store.change(project['id'],lambda p:p.update(last_user_audio_edit='new-user-edit'))
            return [vocal,backing]
        if name=='pitch':return [pitch]
        return [converted]
    monkeypatch.setattr(scheduler,'engine',engine);monkeypatch.setattr(scheduler,'refresh_pitch_target',lambda j,a,*args:a)
    monkeypatch.setattr(scheduler,'model',lambda _: {'model_hash':'f'*64})
    monkeypatch.setattr(scheduler,'export',lambda *args,**kwargs:[])
    if user_changed:
        with pytest.raises(ValueError,match='工程已修改'):scheduler.execute(job,threading.Event())
        assert store.get('projects',project['id'])['selected_candidate_id'] is None
    else:
        scheduler.execute(job,threading.Event())
        current=store.get('projects',project['id']);assert current['selected_candidate_id']
        mix=next(a for a in reversed(current['assets']) if a['stem_role']=='mix')
        assert converted['id'] in mix['input_artifact_ids'] and vocal['id'] not in mix['input_artifact_ids']

import numpy as np
import pytest
import soundfile as sf

from tests.test_core import AUTH, DATA, client, fixture_project, media, req, scheduler, store
from tests.test_review_regressions import _job
from services.orchestrator.model_selection import select_model, clear_selection, resolve_binding
from services.orchestrator.rvc_service import normalized_job_parameters, quick_manifest
from services.orchestrator.workflow import describe_workflow
from workers.rvc_parameters import pitch_analysis_parameters, pitch_parameter_subset
from workers.pitch.engine import apply_target_parameters, shift_pitch_to_project


class FixtureCatalog:
    """Pure metadata fixture. Writes a tiny row to expose nested SQLite write locks."""
    def __init__(self):
        self.calls=[]
        with store.connect() as db:db.execute('CREATE TABLE IF NOT EXISTS specialist_catalog_calls(id INTEGER PRIMARY KEY,payload TEXT)')
        self.models={}
        for letter,epoch in [('a',200),('b',225),('c',250)]:
            self.models['model-'+letter]={
                'id':'model-'+letter,'canonical_id':'canonical-'+letter,'model_artifact_id':'model-'+letter,
                'model_hash':letter*64,'kind':'inference_export','status':'ready','name':'DSP state metadata '+letter,
                'voice_id':'fixture-voice','run_id':'fixture-run','epoch':epoch,'sample_rate':40000,
                'content_dim':768,'architecture':'RVC-v2','f0_conditioned':True,
                'speaker_ids':[0],'speaker_ids_trained':[0],'speaker_metadata_status':'checkpoint',
                'embedder_model':'contentvec','embedder_verified':True,'index_id':None,'index_path':None,'index_hash':None,
                'index_match':{'status':'unknown','reasons':['unit fixture has no retrieval'],'evidence':[]},
            }
    def get_model(self,identifier,**kwargs):return copy.deepcopy(self.models[identifier])
    def model_indices(self,identifier):return {'indices':[],'selected_index_id':None}
    def resolve_model(self,identifier,index_id=None):
        value=self.get_model(identifier);self.calls.append((identifier,index_id))
        # timeout=0 makes an accidental call inside Store.change fail immediately.
        with sqlite3.connect(store.path,timeout=0) as db:db.execute('INSERT INTO specialist_catalog_calls(payload) VALUES(?)',(identifier,))
        return value
    def record_selection(self,model_id,project_id=None,track_id=None,clip_id=None,index_id=None,speaker_id=0,preset_id=None):
        model=self.resolve_model(model_id,index_id)
        return {'id':'selection-'+uuid.uuid4().hex,'model_artifact_id':model_id,'model_hash':model['model_hash'],
            'project_id':project_id,'track_id':track_id,'clip_id':clip_id,'index_id':None,'index_hash':None,
            'speaker_id':speaker_id,'preset_id':preset_id,'created_at':'2026-10-31T00:00:00Z'}


def _select(catalog,project,model_id,scope='project',**extra):
    return select_model(store,catalog,project['id'],{
        'revision':project['revision'],'idempotency_key':uuid.uuid4().hex,'scope':scope,'model_id':model_id,'index_id':'',**extra,
    })['project']


def _audio(source,name,value=.1,length=None,offset=0,part=None,extra=None):
    count=length if length is not None else round(source['sample_count']*48000/source['sample_rate'])
    path=DATA/'uploads'/('specialist-'+uuid.uuid4().hex+'.wav')
    sf.write(path,np.full((count,2),value,dtype=np.float32),48000,subtype='FLOAT')
    return media.commit(store,path,kind='dsp',inputs=[source['id']],role='converted',name=name,
        extra={'fixture_kind':'DSP_state_fixture_not_model_inference','timeline_offset_ticks':offset,'voice_part':part,**(extra or {})})


def _pitch(source,parameters,output=None):
    analysis=pitch_analysis_parameters(parameters)
    count=round(source['sample_count']*16000/source['sample_rate'])
    raw={'sample_rate':16000,'sample_count':count,'hop':160,'frame_center_offset':0,
         'f0_hz':[220.]*(count//160+1),'voiced':[True]*(count//160+1),
         'analysis_parameters':analysis,'estimator':parameters.get('pitch_algorithm','rmvpe'),'version':'unit-dsp-fixture'}
    target=apply_target_parameters(shift_pitch_to_project(raw,parameters.get('source_timeline_offset_ticks',0)),pitch_parameter_subset(parameters))
    path=output or DATA/'uploads'/('specialist-pitch-'+uuid.uuid4().hex+'.json')
    path.write_text(json.dumps({**raw,'target_layer':target}),encoding='utf-8')
    return path,raw,target


def _attach_candidates(project,source):
    base=_audio(source,'full DSP base',.1)
    preview=_audio(source,'short DSP preview',.5,length=24000,offset=24000)
    candidates=[]
    for name,artifact,model_id in [('base',base,'model-a'),('preview',preview,'model-b')]:
        candidates.append({'id':'unit-'+name+'-'+uuid.uuid4().hex,'name':name,'source_artifact_id':source['id'],
            'artifact_id':artifact['id'],'model_id':model_id,'model_manifest_hash':model_id[-1]*64,
            'generation_depth':1,'parameters':{'model_id':model_id,'range_ticks':[24000,48000]} if name=='preview' else {'model_id':model_id},
            'state':'unit_fixture','selected':name=='base','voice_part':None})
    def add(p):
        p['assets'] += [base,preview];p['candidates'] += candidates;p['selected_candidate_id']=candidates[0]['id']
        for t in p['tracks']:t['mute']=True
        for candidate,artifact in zip(candidates,[base,preview]):
            p['tracks'].append({'id':'track-'+uuid.uuid4().hex,'artifact_id':artifact['id'],'candidate_id':candidate['id'],
                'name':candidate['name'],'role':'converted','gain_db':0,'pan':0,'mute':candidate is candidates[1],
                'solo':False,'offset_ticks':artifact['timeline_offset_ticks'],'effects':[]})
    return store.change(project['id'],add),candidates,base,preview


def test_track_clip_bindings_pass_only_runtime_pins_and_clear_to_parent():
    project,source=fixture_project();catalog=FixtureCatalog();track_id=project['tracks'][0]['id']
    project=_select(catalog,project,'model-a')
    project=_select(catalog,project,'model-b','track',track_id=track_id)
    inherited,selected_source=resolve_binding(project,project['parameters'],source['id'])
    assert inherited['model_id']=='model-b' and selected_source==source['id']
    normalized_job_parameters(inherited,catalog.get_model('model-b'))
    assert not {'id','project_id','created_at'} & set(inherited)
    project=_select(catalog,project,'model-c','clip',track_id=track_id,clip_id='explicit-clip',range_ticks=[12000,36000])
    clipped,_=resolve_binding(project,project['parameters'],source['id'])
    assert clipped['model_id']=='model-c' and clipped['clip_id']=='explicit-clip'
    assert clipped['range_ticks']==[12000,36000]
    normalized_job_parameters(clipped,catalog.get_model('model-c'))
    project=clear_selection(store,project['id'],{'revision':project['revision'],'idempotency_key':uuid.uuid4().hex,'scope':'clip','clip_id':'explicit-clip'})['project']
    assert resolve_binding(project,project['parameters'],source['id'])[0]['model_id']=='model-b'
    project=clear_selection(store,project['id'],{'revision':project['revision'],'idempotency_key':uuid.uuid4().hex,'scope':'track','track_id':track_id})['project']
    assert resolve_binding(project,project['parameters'],source['id'])[0]['model_id']=='model-a'


def test_track_version_switch_invalidates_downstream_mix_preserves_pitch():
    project,source=fixture_project();catalog=FixtureCatalog()
    output=_audio(source,'existing converted DSP');mixed=_audio(source,'existing final DSP');mixed['stem_role']='mix';store.put('artifacts',mixed)
    def graph(p):
        p['assets'] += [output,mixed]
        p['graph']=[{'id':'pitch','type':'pitch','state':'succeeded','input_refs':[source['id']],'artifact_refs':[]},
                    {'id':'convert','type':'convert','state':'succeeded','input_refs':[source['id']],'artifact_refs':[output['id']]},
                    {'id':'mix','type':'mix','state':'succeeded','input_refs':[output['id']],'artifact_refs':[mixed['id']]}]
    project=store.change(project['id'],graph)
    project=_select(catalog,project,'model-b','track',track_id=project['tracks'][0]['id'])
    assert next(n for n in project['graph'] if n['id']=='pitch')['state']=='succeeded'
    assert next(n for n in project['graph'] if n['id']=='convert')['state']=='stale'
    assert next(n for n in project['graph'] if n['id']=='mix')['state']=='stale'


def test_readonly_workflow_does_not_resolve_or_write_catalog():
    project,source=fixture_project();catalog=FixtureCatalog()
    project=_select(catalog,project,'model-a')
    before=list(catalog.calls)
    value=describe_workflow(project,quick_manifest(catalog,project['parameters']))
    assert value['stages'][3]['recommendation']['id']=='preview'
    assert catalog.calls==before
    assert project['parameters']['model_id']=='model-a'  # Never silently replaced by canonical/default ID.


def test_adopt_preview_preserves_outside_audio_and_rejects_auto_adopt_after_track_move():
    project,source=fixture_project();project,candidates,base,preview=_attach_candidates(project,source)
    job=_job(project,operation='adopt_preview',parameters={'candidate_id':candidates[1]['id'],'base_candidate_id':candidates[0]['id'],'fade_ms':20})
    def move(p):p['tracks'][0]['offset_ticks']=4800
    store.change(project['id'],move)
    outputs=scheduler.adopt_preview(job,threading.Event())
    current=store.get('projects',project['id'])
    assert current['selected_candidate_id']==candidates[0]['id'], 'An assembly against old track timing must remain an unselected version'
    assert current['candidates'][-1]['selected'] is False
    result,sr=sf.read(outputs[0]['path'],always_2d=True)
    assert sr==48000 and len(result)==base['sample_count']
    np.testing.assert_allclose(result[:24000],.1,rtol=0,atol=1e-7)
    np.testing.assert_allclose(result[48000:],.1,rtol=0,atol=1e-7)
    np.testing.assert_allclose(result[26000:46000],.5,rtol=0,atol=1e-7)


def test_same_source_hash_pitch_cache_rebinds_part_and_refreshes_target(monkeypatch):
    module=importlib.import_module('services.orchestrator.scheduler');project,source=fixture_project()
    lead={**copy.deepcopy(source),'id':'lead-'+uuid.uuid4().hex,'stem_role':'lead_vocal'}
    backing={**copy.deepcopy(source),'id':'back-'+uuid.uuid4().hex,'stem_role':'backing_vocal'}
    store.put('artifacts',lead);store.put('artifacts',backing)
    project=store.change(project['id'],lambda p:p['assets'].extend([lead,backing]))
    root=DATA/('specialist-worker-'+uuid.uuid4().hex);(root/'workers/pitch').mkdir(parents=True)
    (root/'workers/pitch/run.py').write_text('# numerical protocol fixture\n')
    calls=[]
    def protocol(args,*unused,**kwargs):
        request=json.loads(Path(args[3]).read_text(encoding='utf-8'));calls.append(request)
        path,raw,target=_pitch(source,request['parameters'],Path(request['output_dir'])/'pitch.json')
        Path(args[5]).write_text(json.dumps({'ok':True,'outputs':[{'path':str(path),'role':'pitch','processing_kind':'dsp','engine':'unit-pitch','analysis_parameters':raw['analysis_parameters']}],
            'metadata':{'analysis_parameters':raw['analysis_parameters'],'target_hash':target['target_hash']}}),encoding='utf-8')
    outputs=[]
    with monkeypatch.context() as context:
        context.setattr(module,'ROOT',root);context.setattr(module,'resources',lambda:{'pitch':{'python':sys.executable}})
        context.setattr(media,'run',protocol)
        for original,part,transpose in [(lead,'lead_vocal',0),(backing,'backing_vocal',0),(backing,'backing_vocal',12)]:
            job=_job(project,operation='pitch',parameters={'voice_part':part,'pitch_algorithm':'rmvpe','transpose':transpose})
            outputs.append(scheduler.engine('pitch',job,original,threading.Event())[0])
    assert len(calls)==1
    assert outputs[1]['voice_part']=='backing_vocal'
    assert backing['id'] in outputs[1]['input_artifact_ids']
    second=json.loads(Path(outputs[1]['path']).read_text());third=json.loads(Path(outputs[2]['path']).read_text())
    assert second['f0_hz']==third['f0_hz']
    assert third['target_layer']['f0_hz'][0]==pytest.approx(second['target_layer']['f0_hz'][0]*2)


def test_changed_detection_controls_never_choose_old_pitch():
    project,source=fixture_project();parameters={'pitch_algorithm':'rmvpe','f0_min':50}
    path,raw,target=_pitch(source,parameters)
    pitch=media.commit(store,path,kind='dsp',inputs=[source['id']],role='pitch',extra={'analysis_parameters':raw['analysis_parameters']})
    project=store.change(project['id'],lambda p:p['assets'].append(pitch))
    job=_job(project,operation='convert',parameters=parameters)
    assert scheduler.choose_pitch(job,source)['id']==pitch['id']
    assert scheduler.choose_pitch(job,source,{**parameters,'transpose':12})['id']==pitch['id']
    assert scheduler.choose_pitch(job,source,{**parameters,'f0_min':70}) is None
    with pytest.raises(ValueError,match='旧分析'):
        scheduler.choose_pitch(job,source,{**parameters,'f0_min':70,'pitch_artifact_id':pitch['id']})


@pytest.mark.parametrize('scope',['project','track','clip'])
def test_pitch_binding_clear_matches_workflow_and_keeps_locked_edits(monkeypatch,scope):
    app_module=importlib.import_module('services.orchestrator.app');catalog=FixtureCatalog()
    project,source=fixture_project();old_params={'pitch_algorithm':'rmvpe','f0_min':50}
    path,raw,_=_pitch(source,old_params)
    pitch=media.commit(store,path,kind='dsp',inputs=[source['id']],role='pitch',extra={'analysis_parameters':raw['analysis_parameters']})
    lock={'id':'absolute-lock','start_tick':0,'end_tick':4800,'operation':'set_hz','hz':440,'locked':True,
          'snapshot':{'sample_rate':16000,'hop':160,'frame_center_offset':0,'f0_hz':[440.]*10}}
    project=store.change(project['id'],lambda p:(p['assets'].append(pitch),p['pitch_edits'].append(lock)))
    extra={} if scope=='project' else {'track_id':project['tracks'][0]['id']}
    if scope=='clip':extra.update(clip_id='pitch-clip',range_ticks=[0,48000])
    project=_select(catalog,project,'model-a',scope,**extra)
    def change(values):
        return {'parameters':{**project['parameters'],**values}} if scope=='project' else {'model_parameter_updates':{scope+'s':{extra.get('clip_id',extra.get('track_id')):values}}}
    with monkeypatch.context() as context:
        context.setattr(app_module,'catalog',catalog)
        project=req('PATCH',f"/projects/{project['id']}",json={'revision':project['revision'],'patch':change({'f0_min':70,'pitch_artifact_id':pitch['id']})})
        workflow=req('GET',f"/projects/{project['id']}/workflow")
        assert workflow['pitch_analysis']['state']=='stale'
        assert workflow['stages'][2]['recommendation']['operation']=='pitch'
        assert workflow['stages'][3]['recommendation']['id']=='refresh_pitch_preview'
        project=req('PATCH',f"/projects/{project['id']}",json={'revision':project['revision'],'patch':change({'pitch_artifact_id':None})})
        effective,_=resolve_binding(project,project['parameters'],source['id'])
        assert effective['pitch_artifact_id'] is None and project['pitch_edits']==[lock]
        assert scheduler.choose_pitch(_job(project,parameters=effective),source) is None
        assert Path(pitch['path']).is_file() and pitch['id'] in [a['id'] for a in project['assets']]
        assert req('GET',f"/projects/{project['id']}/workflow")['pitch_analysis']['binding']=='automatic'


def test_stale_pitch_retry_resets_pinned_track_without_mutating_failed_snapshot(monkeypatch):
    from tests.test_core import wait_job
    from services.orchestrator.rvc_service import StalePitchBinding
    project,source=fixture_project();track_id=project['tracks'][0]['id']
    project=store.change(project['id'],lambda p:p.update(model_bindings={'tracks':{track_id:{'selection':{},'parameters':{'pitch_artifact_id':'old-curve'}}}}))
    params={'track_id':track_id,'f0_min':70,'transpose':12,'range_ticks':[0,48000],'pitch_artifact_id':'old-curve'}
    def execute(job,cancel):
        if job['parameters'].get('pitch_artifact_id'):raise StalePitchBinding('音高分析参数已改变，指定曲线是旧分析')
        assert job['parameters']['f0_min']==70 and job['parameters']['transpose']==12
        return []
    with monkeypatch.context() as context:
        context.setattr(scheduler,'execute',execute)
        failed=wait_job(scheduler.submit(project['id'],{'revision':project['revision'],'idempotency_key':uuid.uuid4().hex,'operation':'convert','input_artifact_id':source['id'],'parameters':params}))
        assert failed['error']['code']=='stale_pitch_binding'
        assert '重新检测并重试' in failed['error']['recovery']
        retry=req('POST',f"/jobs/{failed['id']}/retry",json={'reset_pitch_binding':True})
        assert wait_job(retry)['state']=='succeeded'
        assert store.get('jobs',failed['id'])['parameters']['pitch_artifact_id']=='old-curve'
        assert store.get('projects',project['id'])['model_bindings']['tracks'][track_id]['parameters']['pitch_artifact_id']=='old-curve'


def test_remove_old_preview_is_reversible_and_preserves_audio_and_adopted_result():
    project,source=fixture_project();project,candidates,base,preview=_attach_candidates(project,source)
    hashes={a['id']:media.sha256(a['path']) for a in (source,base,preview)}
    result=req('PATCH',f"/projects/{project['id']}",json={'revision':project['revision'],'patch':{'candidates':[{'id':candidates[1]['id'],'archived':True}]}})
    assert len(result['candidates'])==2 and result['candidates'][1]['archived']
    assert result['selected_candidate_id']==candidates[0]['id']
    assert candidates[1]['id'] not in [o['id'] for o in describe_workflow(result)['stages'][3]['outputs']]
    rejected=client.patch('/api/v1/projects/'+project['id'],headers=AUTH,json={'revision':result['revision'],'patch':{'candidates':[{'id':candidates[0]['id'],'archived':True}]}})
    assert rejected.status_code==400 and '正在被采用' in rejected.text
    restored=req('POST',f"/projects/{project['id']}/history",json={'revision':result['revision'],'direction':'undo'})
    assert not restored['candidates'][1].get('archived')
    assert {a['id']:media.sha256(a['path']) for a in (source,base,preview)}==hashes


def test_compare_versions_uses_one_source_pitch_range_seed_and_no_auto_adopt(monkeypatch):
    project,source=fixture_project();catalog=FixtureCatalog();project=_select(catalog,project,'model-a')
    params={**project['parameters'],'model_ids':['model-a','model-b'],'range_ticks':[12000,36000],'seed':73,'index_rate':0}
    job=_job(project,operation='compare_versions',parameters=params);job['input_artifact_id']=source['id'];calls=[]
    def engine(engine_name,job,input_audio,cancel,pitch=None,parameters=None):
        assert input_audio['id']==source['id']
        if engine_name=='pitch':
            path,raw,target=_pitch(source,parameters)
            return [media.commit(store,path,kind='dsp',inputs=[source['id']],role='pitch',extra={'analysis_parameters':raw['analysis_parameters']})]
        assert engine_name=='rvc';calls.append((input_audio['id'],pitch['id'],copy.deepcopy(parameters)))
        payload=json.loads(Path(pitch['path']).read_text());proof=payload['target_layer']['target_hash']
        return [_audio(source,'Version wiring DSP fixture',length=24000,offset=12000,
            extra={'model_snapshot':catalog.get_model(parameters['model_id']),'requested_f0_hash':proof,'consumed_f0_hash':'fixture-grid-'+proof,
                   'effective_parameters':{'seed':parameters['seed']}})]
    with monkeypatch.context() as context:
        context.setattr(scheduler,'catalog',catalog);context.setattr(scheduler,'engine',engine)
        scheduler.compare_versions(job,threading.Event())
    assert len(calls)==2 and calls[0][0:2]==calls[1][0:2]
    assert [call[2]['model_id'] for call in calls]==['model-a','model-b']
    assert all(call[2]['seed']==73 and call[2]['range_ticks']==[12000,36000] for call in calls)
    current=store.get('projects',project['id'])
    assert current['selected_candidate_id'] is None
    assert current['comparisons'][-1]['state']=='succeeded'
    assert len(current['comparisons'][-1]['candidate_ids'])==2
    assert all(c['source_artifact_id']==source['id'] and not c['selected'] for c in current['candidates'])


def test_model_selection_api_is_idempotent_and_workflow_is_readonly(monkeypatch):
    app_module=importlib.import_module('services.orchestrator.app');catalog=FixtureCatalog()
    project,source=fixture_project()
    body={'revision':project['revision'],'idempotency_key':uuid.uuid4().hex,
          'scope':'track','track_id':project['tracks'][0]['id'],'model_id':'model-b','index_id':''}
    with monkeypatch.context() as context:
        context.setattr(app_module,'catalog',catalog)
        first=req('POST',f"/projects/{project['id']}/model-selection",json=body)
        calls=len(catalog.calls)
        duplicate=req('POST',f"/projects/{project['id']}/model-selection",json=body)
        assert duplicate['selection']==first['selection']
        assert duplicate['project']['revision']==first['project']['revision']
        assert len(catalog.calls)==calls
        workflow=req('GET',f"/projects/{project['id']}/workflow")
        assert workflow['effective_parameters']['model_id']=='model-b'
        assert workflow['binding_origin']=='track'
        assert len(catalog.calls)==calls
        cleared=req('POST',f"/projects/{project['id']}/model-selection/clear",json={
            'revision':first['project']['revision'],'idempotency_key':uuid.uuid4().hex,
            'scope':'track','track_id':body['track_id'],
        })
        assert not cleared['project']['model_bindings']['tracks']
        assert len(catalog.calls)==calls


def test_project_default_version_selection_leaves_existing_voice_parts_unchanged():
    project,source=fixture_project();catalog=FixtureCatalog()
    parts={}
    artifacts=[]
    for part in ('lead_vocal','backing_vocal'):
        artifact={**copy.deepcopy(source),'id':part+'-'+uuid.uuid4().hex,'stem_role':part}
        store.put('artifacts',artifact);artifacts.append(artifact)
        parts[part]={'source_artifact_id':artifact['id'],'mode':'convert',
            'parameters':{'model_id':'model-a','model_hash':'a'*64,'index_id':None,'index_rate':0,'transpose':3 if part=='lead_vocal' else -2},
            'pitch_edits':[],'notes':[],'selected_candidate_id':None}
    def add(p):p['assets'].extend(artifacts);p['voice_parts']=copy.deepcopy(parts)
    project=store.change(project['id'],add)
    updated=_select(catalog,project,'model-b')
    assert updated['parameters']['model_id']=='model-b'
    assert updated['voice_parts']==parts
    from services.orchestrator.voices import resolve_part
    binding,selected,effective=resolve_part(updated,'lead_vocal')
    inherited,source_id=resolve_binding(updated,effective,selected['id'])
    assert inherited['model_id']=='model-a' and inherited['transpose']==3
    assert source_id==parts['lead_vocal']['source_artifact_id']
