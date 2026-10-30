import json
import threading
import numpy as np
import soundfile as sf
from tests.test_core import store,fixture_project,req,wait_job
from services.orchestrator.editing import compose,freeze_locks,note_edits
from services.orchestrator import media
from services.orchestrator.config import DATA

def test_comp_keeps_timeline_and_blends_without_gap():
    p,a=fixture_project();folder=DATA/'jobs'/'comp-test';folder.mkdir(exist_ok=True)
    x=np.ones((48000*2,2),dtype=np.float32)*.1
    f1=folder/'a.wav';f2=folder/'b.wav';sf.write(f1,x,48000,subtype='FLOAT');sf.write(f2,x*3,48000,subtype='FLOAT')
    a1=media.commit(store,f1,kind='dsp');a2=media.commit(store,f2,kind='dsp')
    candidates=[{'id':'a','artifact_id':a1['id'],'source_artifact_id':a['id']},{'id':'b','artifact_id':a2['id'],'source_artifact_id':a['id']}]
    path,inputs,seams=compose(store,candidates,'a',[{'candidate_id':'b','start_tick':24000,'end_tick':72000,'fade_ms':20}],folder,threading.Event())
    out,sr=sf.read(path)
    assert len(out)==96000 and sr==48000
    assert abs(out[30000,0]-.3)<1e-6 and abs(out[10000,0]-.1)<1e-6
    assert np.max(abs(np.diff(out[:,0])))<.001
    assert seams==[24000,72000] and len(inputs)==2

def test_lock_snapshot_is_preserved_across_new_analysis():
    p,a=fixture_project();path=DATA/'uploads'/'pitch.json'
    data={'sample_rate':16000,'sample_count':16000,'hop':160,'frame_center_offset':0,'f0_hz':[220.]*100,'voiced':[True]*100,'native_score':[None]*100}
    path.write_text(json.dumps(data),encoding='utf-8');pitch=media.commit(store,path,kind='dsp',role='pitch')
    p['assets'].append(pitch)
    patch={'pitch_edits':[{'id':'frozen','start_tick':4800,'end_tick':9600,'operation':'transpose','semitones':12,'locked':True}]}
    freeze_locks(p,patch,store)
    assert patch['pitch_edits'][0]['snapshot']['f0_hz']==[440.]*10
    from pitch.engine import apply_edits
    data['f0_hz']=[330.]*100
    target=apply_edits(data,patch['pitch_edits'])
    assert np.allclose(target['f0_hz'][10:20],[440.]*10,atol=1e-9)
    assert target['f0_hz'][0]==330.

def test_stems_export_equal_lengths_and_explicit_semantics():
    import uuid
    p,a=fixture_project()
    store.change(p['id'],lambda q:q['tracks'][0].update(role='vocal',gain_db=-3))
    p=req('GET','/projects/'+p['id'])
    job=req('POST',f"/projects/{p['id']}/jobs",json={'revision':p['revision'],'operation':'export_stems','idempotency_key':uuid.uuid4().hex,'parameters':{'semantics':'post_fader_fx','format':'wav'}})
    done=wait_job(job);assert done['state']=='succeeded',done
    p=req('GET','/projects/'+p['id']);e=p['exports'][-1]
    assert e['sample_rate']==48000
    assert 'post_fader_fx' in e['bus_export_semantics']
    out,sr=sf.read(e['path']);assert len(out)==round(a['sample_count']*48000/a['sample_rate'])

def test_future_recipe_and_unconfirmed_ripple_are_rejected():
    from tests.test_core import client,AUTH
    future={'format':'sonora-recipe','schema_version':99,'project':{}}
    assert client.post('/api/v1/projects/restore',headers=AUTH,files={'file':('f.json',json.dumps(future),'application/json')}).status_code==400
    p,_=fixture_project()
    assert client.patch('/api/v1/projects/'+p['id'],headers=AUTH,json={'revision':p['revision'],'patch':{'lyrics':[{'id':'l','start':0,'end':48000,'text':'x','timing_policy':'ripple_edit'}]}}).status_code==400

def test_midi_tempo_mapping_and_roundtrip():
    from services.orchestrator.midi import read_midi,write_midi
    from io import BytesIO
    import mido
    song=mido.MidiFile(ticks_per_beat=480);track=mido.MidiTrack();song.tracks.append(track)
    track.extend([mido.MetaMessage('set_tempo',tempo=500000,time=0),mido.Message('note_on',note=60,velocity=80,time=480),mido.Message('note_off',note=60,velocity=0,time=480),mido.MetaMessage('set_tempo',tempo=1000000,time=0),mido.Message('note_on',note=67,velocity=90,time=0),mido.Message('note_off',note=67,velocity=0,time=480)])
    out=BytesIO();song.save(file=out);notes=read_midi(out.getvalue())
    assert [(n['start'],n['end']) for n in notes]==[(24000,48000),(48000,96000)]
    restored=read_midi(write_midi(notes))
    assert [(n['start'],n['end'],n['pitch_midi_float']) for n in restored]==[(24000,48000,60),(48000,96000,67)]

def test_cancellation_stops_worker_tree_without_partial_commit():
    import sys,time,pytest
    folder=DATA/'jobs'/'cancellation-test';folder.mkdir(exist_ok=True)
    marker=folder/'finished.txt';script=folder/'sleeping_worker.py'
    child_code=f"import time;from pathlib import Path;time.sleep(1.5);Path({str(marker)!r}).write_text('unexpected')"
    script.write_text(f"import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c',{child_code!r}])\ntime.sleep(30)\n",encoding='utf-8')
    cancel=threading.Event();timer=threading.Timer(.3,cancel.set);timer.start()
    with pytest.raises(media.Cancelled):media.run([sys.executable,script],cancel,folder/'worker.log')
    timer.join();time.sleep(1.6)
    assert not marker.exists()

def test_excerpt_selection_export_uses_global_timeline():
    import uuid
    p,a=fixture_project();a['timeline_offset_ticks']=48000;store.put('artifacts',a)
    p=store.change(p['id'],lambda q:q.update(assets=[a],selection={'start':60000,'end':84000}))
    j=req('POST',f"/projects/{p['id']}/jobs",json={'revision':p['revision'],'operation':'export','idempotency_key':uuid.uuid4().hex,'parameters':{'artifact_id':a['id'],'selection_only':True,'format':'wav'}})
    done=wait_job(j);assert done['state']=='succeeded',done
    p=req('GET','/projects/'+p['id']);e=p['exports'][-1]
    assert abs(e['duration']-.5)<.0001 and e['source_offset_ticks']==60000
