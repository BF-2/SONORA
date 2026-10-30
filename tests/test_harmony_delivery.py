"""Delivery boundaries use generated signals; no mock model success claims."""
import copy
import importlib.util
import json
import uuid
from pathlib import Path
import numpy as np
import soundfile as sf
from tests.test_core import DATA,store,req,wait_job,fixture_project,media,client,AUTH

def test_integer_export_rejects_overload_float_preserves_it():
    p,_=fixture_project();path=DATA/'uploads'/'over.wav';sf.write(path,np.ones(4800)*1.2,48000,subtype='FLOAT')
    a=media.commit(store,path,kind='dsp',role='mix');p=store.change(p['id'],lambda q:q['assets'].append(a))
    def export(parameters):
        current=req('GET','/projects/'+p['id'])
        return wait_job(req('POST',f"/projects/{p['id']}/jobs",json={'revision':current['revision'],'idempotency_key':uuid.uuid4().hex,'operation':'export','parameters':{'artifact_id':a['id'],'format':'wav',**parameters}}))
    assert export({})['state']=='failed'
    assert export({'wav_subtype':'pcm_f32le'})['state']=='succeeded'
    p=req('GET','/projects/'+p['id']);audio,_=sf.read(p['exports'][-1]['path']);assert audio.max()>1.19
    assert p['exports'][-1]['codec']=='pcm_f32le'
    assert p['graph'][-1]['type']=='export' and p['graph'][-1]['state']=='succeeded'
    assert p['graph'][-1]['export_ref']==p['exports'][-1]['id']

def test_track_delta_preserves_new_sibling_and_rejects_identity_changes():
    p,a=fixture_project();track=p['tracks'][0]
    other={**track,'id':'new_sibling','name':'backing','gain_db':-6}
    p=store.change(p['id'],lambda q:q['tracks'].append(other))
    p=req('PATCH','/projects/'+p['id'],json={'revision':p['revision'],'patch':{'track_updates':{track['id']:{'gain_db':-3}}}})
    assert next(t for t in p['tracks'] if t['id']=='new_sibling')['gain_db']==-6
    assert next(t for t in p['tracks'] if t['id']==track['id'])['gain_db']==-3
    bad=client.patch('/api/v1/projects/'+p['id'],headers=AUTH,json={'revision':p['revision'],'patch':{'track_updates':{track['id']:{'artifact_id':'fake'}}}})
    assert bad.status_code==400

def test_pitch_target_uses_project_offset_raw_detection_stays_local(tmp_path,monkeypatch):
    root=Path(__file__).resolve().parents[1]
    spec=importlib.util.spec_from_file_location('pitch_worker_offset_test',root/'workers/pitch/run.py');worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
    raw={'sample_rate':16000,'sample_count':640,'hop':160,'frame_center_offset':0,'f0_hz':[100.,100.,None,100.],'voiced':[True,True,False,True],'estimator':'test-only','model_hash':None,'score_kind':'unavailable','time_map':{'source_rate':16000,'output_rate':16000,'source_offset':0,'output_offset':0}}
    monkeypatch.setattr(worker,'resource_config',lambda _:{});monkeypatch.setattr(worker,'analyze',lambda *args:copy.deepcopy(raw))
    worker.render({'input_path':'not-read-fixture','output_dir':str(tmp_path),'parameters':{'source_timeline_offset_ticks':96000,'pitch_edits':[{'start_tick':96000,'end_tick':96960,'operation':'transpose','semitones':12}]}})
    saved=json.loads((tmp_path/'pitch.json').read_text())
    assert saved['frame_center_offset']==0 and saved['f0_hz']==raw['f0_hz']
    assert saved['target_layer']['frame_center_offset']==32000
    assert saved['target_layer']['f0_hz']==[200.,200.,None,100.]
