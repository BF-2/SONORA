"""Real FFmpeg monitoring and state regression; no neural inference is mocked as audio."""
import copy
import json
import threading
from pathlib import Path
import uuid
import numpy as np
import soundfile as sf
import pytest
from tests.test_core import fixture_project,req,store,scheduler,media,DATA
from tests.test_review_regressions import _job
from services.orchestrator.workflow import describe_workflow
from services.orchestrator.mixing import preview_key


def monitor_project():
    p, source = fixture_project()
    from services.orchestrator.app import attach_import
    background = media.import_audio(store, source['path'], role='instrumental')
    p = attach_import(p['id'], background)['project']
    # Background is the sole enabled input for these DSP monitoring tests.
    return p, background

def position(pid):
    with store.connect() as db:return db.execute('SELECT position FROM cursors WHERE project_id=?',(pid,)).fetchone()[0]

def test_view_changes_preserve_undo_redo_and_current_view():
    p,a=fixture_project();baseline=position(p['id'])
    for patch in ({'playhead':1234},{'view_preferences':{'stage':4,'zoom':3}},{'mode':'expert'}):
        p=req('PATCH',f"/projects/{p['id']}",json={'revision':p['revision'],'patch':patch})
    assert position(p['id'])==baseline
    p=req('PATCH',f"/projects/{p['id']}",json={'revision':p['revision'],'patch':{'track_updates':{p['tracks'][0]['id']:{'gain_db':-6}}}})
    p=req('PATCH',f"/projects/{p['id']}",json={'revision':p['revision'],'patch':{'playhead':5678,'view_preferences':{'stage':2,'zoom':4}}})
    p=req('POST',f"/projects/{p['id']}/history",json={'revision':p['revision'],'direction':'undo'})
    assert p['tracks'][0]['gain_db']==0 and p['playhead']==5678 and p['view_preferences']=={'stage':2,'zoom':4} and p['mode']=='expert'
    p=req('PATCH',f"/projects/{p['id']}",json={'revision':p['revision'],'patch':{'playhead':6789}})
    p=req('POST',f"/projects/{p['id']}/history",json={'revision':p['revision'],'direction':'redo'})
    assert p['tracks'][0]['gain_db']==-6 and p['playhead']==6789

def test_undo_skips_legacy_view_only_checkpoints():
    p,a=fixture_project();p=store.change(p['id'],lambda p:p.update(name='edited'))
    for head in (1,2,3):
        p=store.change(p['id'],lambda p:p.update(playhead=head),history=False);store.checkpoint(p)
    p=store.history(p['id'],'undo',p['revision'])
    assert p['name']=='信号验收' and p['playhead']==3

def test_workflow_is_only_navigation_contract():
    p=req('POST','/projects',json={'name':'setup','task':'speech'})
    w=describe_workflow(p)
    assert w['stages'][0]['recommendation']['id']=='setup' and w['stages'][0]['continuation']['disabled']
    p,a=fixture_project();w=describe_workflow(p)
    assert not w['stages'][0]['continuation']['disabled']
    assert w['stages'][0]['recommendation']['kind']=='navigate'
    assert w['stages'][0]['recommendation']['operation'] is None
    assert w['stages'][2]['continuation']['disabled'] and w['stages'][3]['continuation']['disabled']
    assert all(s['continuation']['kind']=='navigate' for s in w['stages'][:-1])

def render(tracks,region=None):
    path=DATA/'uploads'/('monitor-'+uuid.uuid4().hex+'.wav')
    media.mix(store,tracks,path,range_ticks=region)
    return sf.read(path,always_2d=True)[0],path

def test_ffmpeg_preview_equals_full_mix_crop_with_all_routing_controls():
    p,a=fixture_project();t={**p['tracks'][0],'offset_ticks':4800,'gain_db':-6,'pan':.65,'effects':[{'type':'highpass','frequency':500},{'type':'delay','delay_ms':40,'decay':.2}]}
    muted={**t,'id':'muted','gain_db':12,'mute':True}
    other={**t,'id':'other','gain_db':6,'offset_ticks':-2400}
    solo={**t,'solo':True}
    full,_=render([solo,muted,other]);part,path=render([solo,muted,other],[2400,48000])
    np.testing.assert_allclose(part,full[2400:48000],atol=1e-6)
    assert len(part)==45600 and abs(part[:,0]).max()<abs(part[:,1]).max()
    flat,_=render([{**t,'gain_db':0,'pan':0,'effects':[],'offset_ticks':0}],[0,48000])
    gained,_=render([{**t,'pan':0,'effects':[],'offset_ticks':0}],[0,48000])
    np.testing.assert_allclose(gained,flat*10**(-6/20),atol=1e-6)
    assert not np.allclose(part[:40000],flat[:40000])
    levels=media.meter_levels(path)
    assert len(levels['peaks_dbfs'])==19 and len(levels['peaks_dbfs'][0])==2

def test_all_muted_preview_is_silence_and_respects_range():
    p,a=fixture_project();samples,_=render([{**p['tracks'][0],'mute':True}],[12000,36000])
    assert samples.shape==(24000,2) and np.max(np.abs(samples))==0

def test_preview_does_not_extend_effect_tails_past_full_mix_timeline():
    p,a=fixture_project();path=DATA/'uploads'/('tail-'+uuid.uuid4().hex+'.wav')
    sf.write(path,np.full((24000,2),.2,dtype=np.float32),48000,subtype='FLOAT')
    short=media.commit(store,path,kind='dsp',role='vocal')
    tracks=[{**p['tracks'][0],'artifact_id':short['id'],'effects':[{'type':'delay','delay_ms':200,'decay':.5}]}]
    full,_=render(tracks);preview,_=render(tracks,[0,48000])
    np.testing.assert_allclose(preview[:len(full)],full,atol=1e-7)
    assert len(full)==24000 and np.max(np.abs(preview[24000:]))==0

def test_preview_cache_tracks_parameters_and_never_adopts_or_checkpoints():
    p,a=monitor_project();baseline=position(p['id']);region=[0,24000]
    job=_job(p,operation='mix_preview',parameters={'range_ticks':region})
    first=scheduler.mix_preview(job,threading.Event())[0]
    after=store.get('projects',p['id'])
    assert after['assets']==p['assets'] and after['revision']==p['revision']
    assert position(p['id'])==baseline and after['tracks']==p['tracks'] and after['selected_candidate_id']==p['selected_candidate_id']
    assert first['stem_role']=='mix_preview' and first['sample_count']==24000 and first['track_meters']
    second=scheduler.mix_preview(_job(after,operation='mix_preview',parameters={'range_ticks':region}),threading.Event())[0]
    assert first['id']==second['id']
    changed=copy.deepcopy(after);next(t for t in changed['tracks'] if t['id']==changed['background_track_id'])['gain_db']=-12
    assert preview_key(changed,region)!=preview_key(after,region)
    third=scheduler.mix_preview(_job(changed,operation='mix_preview',parameters={'range_ticks':region}),threading.Event())[0]
    assert third['sha256']!=first['sha256'] and third['diagnostics']['rms_dbfs']==pytest.approx(first['diagnostics']['rms_dbfs']-12,abs=.05)

def test_import_and_upstream_audio_invalidate_mix_without_losing_exports():
    p,a=monitor_project();mixed=scheduler.mix_job(_job(p,operation='mix'),threading.Event())[0]
    p=store.get('projects',p['id']);assert not next(n for n in p['graph'] if mixed['id'] in n['artifact_refs'])['state']=='stale'
    with Path(a['path']).open('rb') as f:result=req('POST',f"/projects/{p['id']}/import",files={'file':('another.wav',f,'audio/wav')},data={'role':'instrumental'})
    p=result['project'];assert next(n for n in p['graph'] if mixed['id'] in n['artifact_refs'])['state']=='stale'
    p=store.change(p['id'],lambda p:[n.update(state='succeeded') for n in p['graph'] if n['type']=='mix'])
    stem={**a,'id':'derived-'+uuid.uuid4().hex,'stem_role':'vocal'};store.put('artifacts',stem)
    scheduler.attach(_job(p,operation='separate'),[stem])
    p=store.get('projects',p['id']);assert next(n for n in p['graph'] if mixed['id'] in n['artifact_refs'])['state']=='stale'

def test_same_window_measurement_contains_real_loudness_and_true_peak():
    p,a=fixture_project();result=req('POST',f"/artifacts/{a['id']}/measure",json={'range_ticks':[0,48000]})
    assert result['duration']==pytest.approx(1) and result['sample_range']==[0,44100]
    assert result['measurement_succeeded'] and result['true_peak_dbfs'] is not None and result['integrated_lufs'] is not None
    assert result['rms_dbfs']<result['sample_peak_dbfs']
    assert req('POST',f"/artifacts/{a['id']}/measure",json={'range_ticks':[0,48000]})==result

@pytest.mark.parametrize('codec,subtype',[('pcm_s16le','PCM_16'),('pcm_s24le','PCM_24'),('pcm_f32le','FLOAT')])
def test_export_selected_bit_depth(codec,subtype):
    p,a=fixture_project();job=_job(p,parameters={'artifact_id':a['id'],'format':'wav','wav_subtype':codec})
    scheduler.export(job,threading.Event());export=store.get('projects',p['id'])['exports'][-1]
    assert export['codec']==codec and sf.info(export['path']).subtype==subtype
    assert export['measurements']['true_peak_dbfs'] is not None

def test_workflow_exposes_active_job_without_requiring_project_edit():
    p,a=fixture_project();job=_job(p,operation='pitch',parameters={})
    workflow=req('GET',f"/projects/{p['id']}/workflow")
    assert workflow['stages'][2]['state']=='running' and workflow['stages'][2]['recommendation']['disabled']
    job['state']='cancelled';store.put('jobs',job)
    after=req('GET',f"/projects/{p['id']}/workflow")
    assert after['revision']==workflow['revision'] and after['stages'][2]['state']!='running'

def test_pitch_worker_uses_private_writable_temp_and_numba_cache(monkeypatch):
    from tests.test_specialist_integration import _pitch
    p,a=fixture_project();job=_job(p,operation='pitch',parameters={'f0_min':71})
    def run(args,cancel,log,env,cwd):
        assert Path(env['NUMBA_CACHE_DIR']).is_relative_to(DATA)
        assert Path(env['TEMP']).is_relative_to(DATA/'jobs'/job['id']) and env['TMP']==env['TEMP']
        import tempfile
        with tempfile.NamedTemporaryFile(dir=env['NUMBA_CACHE_DIR']):pass
        request=json.loads(Path(args[args.index('--request')+1]).read_text(encoding='utf-8'))
        path,raw,_=_pitch(a,request['parameters'],Path(request['output_dir'])/'fixture.json')
        Path(args[args.index('--response')+1]).write_text(json.dumps({'ok':True,'outputs':[{'path':str(path),'role':'pitch','processing_kind':'dsp','sample_rate':16000,'sample_count':raw['sample_count'],'channels':1}],
            'metadata':{'analysis_parameters':raw['analysis_parameters']}}),encoding='utf-8')
    monkeypatch.setattr(media,'run',run)
    result=scheduler.engine('pitch',job,a,threading.Event())
    assert result[0]['processing_kind']=='dsp' and scheduler.pitch_matches(result[0],job['parameters'])
