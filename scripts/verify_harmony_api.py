"""Explicit real-model acceptance on the user's 1:29–1:55 test excerpt."""
from pathlib import Path
import json,time,uuid,math,os
import httpx
ROOT=Path(__file__).resolve().parents[1]
connection=json.loads((ROOT/'runtime/harmony-qa-session.json').read_text(encoding='utf-8'))
client=httpx.Client(base_url=connection['endpoint']+'/api/v1',headers={'Authorization':'Bearer '+connection['token']},timeout=120)
OUT=ROOT/'tests/results/harmony/user-89-115/api';OUT.mkdir(parents=True,exist_ok=True)

def api(method,path,**kwargs):
    response=client.request(method,path,**kwargs)
    if response.status_code>=400:raise RuntimeError(str(response.status_code)+' '+response.text)
    return response.json()

def wait(job):
    previous=None;start=time.monotonic()
    for _ in range(1200):
        job=api('GET','/jobs/'+job['id'])
        stage=(job['state'],job['stage'])
        if stage!=previous:print(json.dumps({'job':job['id'],'state':stage[0],'stage':stage[1]},ensure_ascii=True),flush=True);previous=stage
        if job['state'] in ('failed','blocked','cancelled'):raise RuntimeError(str(job.get('error')))
        if job['state']=='succeeded':return {**job,'test_elapsed_seconds':time.monotonic()-start}
        time.sleep(.5)
    raise TimeoutError('Job exceeded acceptance limit')

def run_job(pid,operation,parameters=None):
    project=api('GET','/projects/'+pid)
    return wait(api('POST',f'/projects/{pid}/jobs',json={'revision':project['revision'],'idempotency_key':uuid.uuid4().hex,'operation':operation,'parameters':parameters or {}}))

if __name__=='__main__':
    models={'lead_vocal':os.environ.get('SONORA_VERIFY_LEAD_MODEL_ID', 'haysay_twilight_sparkle'),'backing_vocal':os.environ.get('SONORA_VERIFY_BACKING_MODEL_ID', 'haysay_fluttershy')}
    if not all(models.values()): raise RuntimeError('Set replacement SONORA_VERIFY_LEAD_MODEL_ID and SONORA_VERIFY_BACKING_MODEL_ID before real-model acceptance.')
    project=api('POST','/projects',json={'name':'BREAK FROM THE LINE · 1:29–1:55 · 主唱/和声','task':'cover'});pid=project['id']
    imported=api('POST',f'/projects/{pid}/import-path',json={'path':str(OUT.parent/'source.wav'),'role':'original'})
    print(json.dumps({'project_id':pid,'source_duration':26}),flush=True)
    jobs=[run_job(pid,'separate'),run_job(pid,'separate_harmony')]
    p=api('GET','/projects/'+pid)
    p=api('PATCH','/projects/'+pid,json={'revision':p['revision'],'patch':{'parameters':{**p['parameters'],'active_voice_part':'lead_vocal'},'voice_parts':{part:{'mode':'convert','parameters':{'model_id':model,'pitch_algorithm':'rmvpe','transpose':0,'index_rate':0,'protect':.33}} for part,model in models.items()}}})
    for part in models:
        jobs.append(run_job(pid,'pitch',{'voice_part':part}))
        jobs.append(run_job(pid,'convert',{'voice_part':part}))
        current=api('GET','/projects/'+pid);candidate=current['candidates'][-1]
        api('POST',f"/projects/{pid}/candidates/{candidate['id']}/review",json={'revision':current['revision'],'action':'accept'})
    jobs.append(run_job(pid,'mix'))
    p=api('GET','/projects/'+pid);parts=p['voice_parts'];assert set(parts)==set(models)
    assets={a['id']:a for a in p['assets']};selected=[];evidence={}
    for part in models:
        binding=parts[part];candidate=next(c for c in reversed(p['candidates']) if c.get('voice_part')==part and c.get('review_status')=='accepted')
        assert candidate['voice_part']==part and candidate['model_id']==models[part]
        assert candidate['source_artifact_id']==binding['source_artifact_id']
        converted=assets[candidate['artifact_id']];assert converted['processing_kind']=='model_inference' and converted['voice_part']==part
        assert converted['sample_count']==converted['sample_rate']*26
        pitch=next(assets[ref] for ref in converted['input_artifact_ids'] if assets[ref]['stem_role']=='pitch')
        assert pitch['voice_part']==part and binding['source_artifact_id'] in pitch['input_artifact_ids']
        assert converted['requested_f0_hash'] and converted['consumed_f0_hash']
        selected.append(converted['id']);evidence[part]={'source':assets[binding['source_artifact_id']],'candidate':candidate,'converted':converted,'pitch':pitch}
    mix=next(a for a in reversed(p['assets']) if a['stem_role']=='mix')
    assert mix['sample_count']==26*48000 and mix['channels']==2
    roles=[assets[ref]['stem_role'] for ref in mix['input_artifact_ids']]
    assert roles.count('converted')==2 and roles.count('instrumental')==1 and len(roles)==3,roles
    assert set(selected)<=set(mix['input_artifact_ids'])
    # Preserve unnormalized float stems; leave explicit user-visible fader headroom
    # for integer delivery instead of silently clipping or limiting the model output.
    peak=max([mix['diagnostics']['sample_peak_dbfs']]+[assets[t['artifact_id']]['diagnostics']['sample_peak_dbfs']+t['gain_db'] for t in p['tracks'] if not t['mute'] and t['role']!='mix'])
    attenuation=-math.ceil(max(0,peak+1.5)*10)/10
    if attenuation<0:
        tracks=[{**t,'gain_db':t['gain_db']+attenuation} if not t['mute'] and t['role']!='mix' else t for t in p['tracks']]
        p=api('PATCH','/projects/'+pid,json={'revision':p['revision'],'patch':{'tracks':tracks}})
        jobs.append(run_job(pid,'mix'))
        p=api('GET','/projects/'+pid);mix=next(a for a in reversed(p['assets']) if a['stem_role']=='mix')
    jobs.append(run_job(pid,'export',{'artifact_id':mix['id'],'format':'wav','model_converted':True,'name':'BREAK FROM THE LINE 89-115 · 主唱和声分别转换 · 试听混音'}))
    jobs.append(run_job(pid,'export_stems',{'semantics':'post_fader_fx','format':'wav'}))
    p=api('GET','/projects/'+pid)
    report={'project_id':pid,'source_user_interval':[89,115],'jobs':[{k:j.get(k) for k in ('id','operation','state','test_elapsed_seconds')} for j in jobs],
            'parts':evidence,'mix':mix,'exports':p['exports'],'mix_input_roles':roles,'independent_sources_models_f0_verified':True,'uniform_fader_headroom_db':attenuation,
            'human_listening':'pending; acceptance clicks simulated for testing','quality_scope':'Separate lead/backing model estimates; manual listening still required for leakage and dominant pitch in multi-voice harmony.'}
    (OUT/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'result':'passed','project_id':pid,'duration':26,'exports':[e['path'] for e in p['exports']]},ensure_ascii=True),flush=True)
