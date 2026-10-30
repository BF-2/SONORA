import json
import os
from pathlib import Path
import tempfile
import time
import uuid

os.environ.setdefault('SONORA_DATA',tempfile.mkdtemp(prefix='sonora-test-'))
os.environ.setdefault('SONORA_SESSION_TOKEN','test-token-not-a-production-secret')
from fastapi.testclient import TestClient
from services.orchestrator.app import app,store,scheduler
from services.orchestrator import media
from services.orchestrator.config import DATA
import numpy as np
import soundfile as sf

client=TestClient(app)
AUTH={'Authorization':'Bearer '+os.environ['SONORA_SESSION_TOKEN']}

def req(method,path,**kwargs):
    response=client.request(method,'/api/v1'+path,headers=AUTH,**kwargs)
    assert response.status_code<400,response.text
    return response.json()

def fixture_project():
    p=req('POST','/projects',json={'name':'信号验收','task':'speech'})
    sr=44100;n=sr*2+137
    signal=.2*np.sin(np.arange(n)*2*np.pi*440/sr)
    # Exact integer impulse anchors are tested through resampling independently.
    path=DATA/'uploads'/'signal.wav';sf.write(path,np.column_stack([signal,signal*.7]),sr,subtype='FLOAT')
    with path.open('rb') as f: imported=req('POST',f"/projects/{p['id']}/import",files={'file':('signal.wav',f,'audio/wav')})
    return imported['project'],imported['artifact']

def wait_job(job):
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        current=req('GET','/jobs/'+job['id'])
        if current['state'] in ('succeeded','failed','blocked','cancelled'): return current
        time.sleep(.1)
    raise AssertionError('job timeout')

def test_auth_origin_and_path_security():
    assert client.get('/api/v1/projects').status_code==401
    assert client.get('/api/v1/projects',headers={**AUTH,'Origin':'https://evil.example'}).status_code==403
    assert client.get('/api/v1/health',headers={'Host':'evil.example'}).status_code==403
    p=req('POST','/projects',json={'name':'边界'})
    bad=client.post('/api/v1/projects/'+p['id']+'/import-path',headers=AUTH,json={'path':'C:/Windows/win.ini'})
    assert bad.status_code==400

def test_import_preserves_samples_and_peaks():
    p,a=fixture_project()
    assert a['sample_count']==44100*2+137
    assert a['sample_rate']==44100 and a['channels']==2
    assert a['processing_kind']=='decode'
    waveform=req('GET',f"/artifacts/{a['id']}/waveform")
    assert len(waveform['peaks'])<=2048
    assert -15<a['diagnostics']['sample_peak_dbfs']<-13
    assert media.sha256(media.validate_artifact(a))==a['sha256']

def test_history_modes_and_stale_revision():
    p,a=fixture_project();start=p['revision']
    changed=req('PATCH',f"/projects/{p['id']}",json={'revision':start,'patch':{'mode':'expert','playhead':12345,'selection':{'start':2345,'end':67890}}})
    assert changed['playhead']==12345 and changed['mode']=='expert'
    assert client.patch('/api/v1/projects/'+p['id'],headers=AUTH,json={'revision':start,'patch':{'mode':'quick'}}).status_code==409
    undo=req('POST',f"/projects/{p['id']}/history",json={'direction':'undo','revision':changed['revision']})
    assert undo['mode']=='quick'
    redo=req('POST',f"/projects/{p['id']}/history",json={'direction':'redo','revision':undo['revision']})
    assert redo['playhead']==12345 and redo['selection']['start']==2345

def test_locked_edit_recipe_roundtrip_and_fake_candidate_rejected():
    p,a=fixture_project()
    edits=[{'id':'edit1','start_tick':1000,'end_tick':3000,'operation':'transpose','semitones':12,'locked':True}]
    p=req('PATCH',f"/projects/{p['id']}",json={'revision':p['revision'],'patch':{'pitch_edits':edits}})
    recipe=req('GET',f"/projects/{p['id']}/recipe")
    assert 'path' not in recipe['project']['assets'][0]
    restored=req('POST','/projects/restore',files={'file':('recipe.json',json.dumps(recipe).encode(),'application/json')})
    assert restored['pitch_edits']==edits and restored['assets'][0]['sha256']==a['sha256']
    bad=client.patch('/api/v1/projects/'+p['id'],headers=AUTH,json={'revision':p['revision'],'patch':{'candidates':[{'id':'fake','artifact_id':a['id']}]}})
    assert bad.status_code==400

def test_export_crop_idempotency_provenance_and_no_overwrite():
    p,a=fixture_project()
    p=req('PATCH',f"/projects/{p['id']}",json={'revision':p['revision'],'patch':{'selection':{'start':1001,'end':49001}}})
    body={'revision':p['revision'],'idempotency_key':uuid.uuid4().hex,'operation':'export','parameters':{'artifact_id':a['id'],'format':'wav','name':'signal','selection_only':True}}
    first=req('POST',f"/projects/{p['id']}/jobs",json=body)
    duplicate=req('POST',f"/projects/{p['id']}/jobs",json=body)
    assert first['id']==duplicate['id']
    done=wait_job(first);assert done['state']=='succeeded',done
    p=req('GET',f"/projects/{p['id']}");e=p['exports'][-1]
    with sf.SoundFile(e['path']) as result: assert len(result)==44100 and result.channels==2
    body.update(revision=p['revision'],idempotency_key=uuid.uuid4().hex)
    assert wait_job(req('POST',f"/projects/{p['id']}/jobs",json=body))['state']=='succeeded'
    p=req('GET',f"/projects/{p['id']}");assert p['exports'][-1]['path']!=e['path']
    body.update(revision=p['revision'],idempotency_key=uuid.uuid4().hex);body['parameters']['model_converted']=True
    assert wait_job(req('POST',f"/projects/{p['id']}/jobs",json=body))['state']=='failed'

def test_mix_signal_sum_no_auto_original_double_count():
    p,a=fixture_project();output=DATA/'uploads'/'mix.wav'
    media.mix(store,[{'artifact_id':a['id'],'gain_db':-6.020599913279624,'pan':0}],output)
    mixed,sr=sf.read(output,always_2d=True)
    assert sr==48000 and len(mixed)==round(a['sample_count']*48000/44100)
    assert .099<float(np.max(abs(mixed)))<.101

def test_resampling_noninteger_crop_anchor():
    sr=44100;n=sr*4+137;signal=np.zeros(n,dtype=np.float32);anchor=110251;signal[anchor]=.8
    original=DATA/'uploads'/'anchor.wav';sf.write(original,signal,sr,subtype='FLOAT')
    path16=DATA/'uploads'/'anchor16.wav';path48=DATA/'uploads'/'anchor48.wav'
    for source,out,rate in [(original,path16,16000),(path16,path48,48000)]:
        media.run([media.binary('ffmpeg'),'-v','error',*media.safe_input(source),'-ar',str(rate),'-c:a','pcm_f32le','-y',str(out)])
    result,_=sf.read(path48);assert abs(np.argmax(abs(result))-round(anchor*48000/sr))<=2
    artifact=media.commit(store,path48,kind='dsp');cropped=DATA/'uploads'/'cropped.wav'
    start=12347;media.crop(artifact,cropped,start,150000)
    cut,_=sf.read(cropped);assert abs(np.argmax(abs(cut))-(round(anchor*48000/sr)-start))<=2

def test_disabled_model_worker_cannot_succeed():
    p,a=fixture_project()
    os.environ['SONORA_DISABLE_MODEL_WORKERS']='1'
    try:
        job=req('POST',f"/projects/{p['id']}/jobs",json={'revision':p['revision'],'idempotency_key':uuid.uuid4().hex,'operation':'convert'})
        done=wait_job(job);assert done['state']=='failed' and not done['artifact_ids']
    finally: os.environ.pop('SONORA_DISABLE_MODEL_WORKERS',None)

def test_corrupt_cache_not_validated():
    p,a=fixture_project()
    broken={**a,'sha256':'0'*64}
    import pytest
    with pytest.raises(ValueError): media.validate_artifact(broken)

def test_playlists_and_remote_media_rejected():
    p=req('POST','/projects',json={'name':'不访问远程'})
    response=client.post(f"/api/v1/projects/{p['id']}/import",headers=AUTH,files={'file':('remote.m3u',b'#EXTM3U\nhttps://example.com/audio.mp3','audio/x-mpegurl')})
    assert response.status_code==400
    response=client.post(f"/api/v1/projects/{p['id']}/import",headers=AUTH,files={'file':('remote.wav',b'#EXTM3U\nhttps://example.com/audio.mp3','audio/wav')})
    assert response.status_code==400

def test_event_replay_and_integer_boundaries():
    p,a=fixture_project()
    e1=store.event('test.event',data={'ok':True});e2=store.event('test.event2',data={'ok':True})
    replay=req('GET',f"/events?after={e1['sequence']}")
    assert e2['sequence'] in [e['sequence'] for e in replay['events']]
    bad=client.patch(f"/api/v1/projects/{p['id']}",headers=AUTH,json={'revision':p['revision'],'patch':{'selection':{'start':.1,'end':1000}}})
    assert bad.status_code==400
