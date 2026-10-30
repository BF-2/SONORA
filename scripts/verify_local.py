"""Run real local models through the production API. No external uploads."""
import os
import sys
from pathlib import Path
import json
import time
import uuid
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
os.environ.setdefault('SONORA_SESSION_TOKEN','integration-'+uuid.uuid4().hex)
from fastapi.testclient import TestClient
from services.orchestrator.app import app,store
from services.orchestrator import media
from services.orchestrator.config import resources
client=TestClient(app);headers={'Authorization':'Bearer '+os.environ['SONORA_SESSION_TOKEN']}
result_dir=ROOT/'tests/results/integration';result_dir.mkdir(parents=True,exist_ok=True)

def api(method,path,**kwargs):
    response=client.request(method,'/api/v1'+path,headers=headers,**kwargs)
    if response.status_code>=400: raise RuntimeError(response.text)
    return response.json()

def run(task,path,name,role='original'):
    p=api('POST','/projects',json={'name':name,'task':task})
    p=api('POST',f"/projects/{p['id']}/import-path",json={'path':str(path),'role':role})['project']
    models=resources()['models'];model=next(m for m in models if m['id']=='twilight_sparkle_pony_singing_r18m_20260914')
    if task=='speech': model=next(m for m in models if m['id']=='twilight_sparkle_pony_speech_fim_v1')
    job=api('POST',f"/projects/{p['id']}/jobs",json={'revision':p['revision'],'operation':'pipeline','idempotency_key':uuid.uuid4().hex,'parameters':{'model_id':model['id'],'index_rate':0,'pitch_algorithm':'rmvpe','protect':.33,'name':name}})
    print(json.dumps({'project':p['id'],'job':job['id'],'stage':'submitted'},ensure_ascii=True),flush=True)
    deadline=time.monotonic()+600;last=None
    while time.monotonic()<deadline:
        job=api('GET','/jobs/'+job['id'])
        if (job['state'],job['stage'])!=last:
            last=(job['state'],job['stage']);print(json.dumps({'state':job['state'],'stage':job['stage']},ensure_ascii=True),flush=True)
        if job['state'] in ('failed','blocked','cancelled'): raise RuntimeError(json.dumps(job['error'],ensure_ascii=True))
        if job['state']=='succeeded': break
        time.sleep(.5)
    else: raise TimeoutError('Pipeline timed out')
    p=api('GET',f"/projects/{p['id']}")
    evidence={'project_id':p['id'],'job_id':job['id'],'task':task,'source':str(path),'source_hash':media.sha256(path),'candidate_count':len(p['candidates']),'exports':p['exports'],'artifacts':[{k:a.get(k) for k in ('id','sha256','stem_role','processing_kind','sample_rate','sample_count','channels','job_id','model_hash','input_artifact_ids','requested_f0_hash','consumed_f0_hash')} for a in p['assets']]}
    (result_dir/(task+'.json')).write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'task':task,'export':p['exports'][-1]['path'],'duration':p['exports'][-1]['duration']},ensure_ascii=True),flush=True)
    return evidence

if __name__=='__main__':
    song=ROOT.parent/'AllAboutUS.mp3';short=result_dir/'song-private-12s.wav'
    media.run([media.binary('ffmpeg'),'-v','error',*media.safe_input(song),'-ss','35','-t','12','-c:a','pcm_f32le','-y',str(short)])
    results=[run('cover',short,'SONORA 歌曲链路验收 · 12秒私有素材'),run('speech',ROOT.parent/'datasets/_pony_speech_mirror_probe/00000.wav','SONORA 语音链路验收')]
    (result_dir/'summary.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
