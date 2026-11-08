"""Real standard-workflow regression in an isolated DB; acceptance clicks test state, not subjective quality."""
import os
import sys
from pathlib import Path
import json
import time
import uuid
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
os.environ.setdefault('SONORA_DATA',str(ROOT/'runtime/standard-regression/data'))
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
    models=resources()['models']
    model_id=os.environ.get('SONORA_VERIFY_SPEECH_MODEL_ID' if task=='speech' else 'SONORA_VERIFY_COVER_MODEL_ID', 'haysay_twilight_sparkle')
    if not model_id: raise RuntimeError('Set replacement SONORA_VERIFY_COVER_MODEL_ID / SONORA_VERIFY_SPEECH_MODEL_ID before real-model acceptance.')
    model=next(m for m in models if m['id']==model_id)
    p=api('POST','/projects',json={'name':name,'task':task})
    p=api('POST',f"/projects/{p['id']}/import-path",json={'path':str(path),'role':role})['project']
    def run_job(operation,parameters=None,source_id=None):
        current=api('GET',f"/projects/{p['id']}")
        job=api('POST',f"/projects/{p['id']}/jobs",json={'revision':current['revision'],'operation':operation,'input_artifact_id':source_id,'idempotency_key':uuid.uuid4().hex,'parameters':parameters or {}})
        deadline=time.monotonic()+600
        while time.monotonic()<deadline:
            job=api('GET','/jobs/'+job['id'])
            if job['state'] in ('failed','blocked','cancelled'):raise RuntimeError(json.dumps(job['error'],ensure_ascii=True))
            if job['state']=='succeeded':return job
            time.sleep(.4)
        raise TimeoutError(operation+' timed out')
    if task=='cover' and role=='original':
        run_job('separate')
        run_job('separate_harmony')
        targets=['lead_vocal','backing_vocal']
    else:targets=[None]
    for part in targets:
        current=api('GET',f"/projects/{p['id']}")
        source_id=current['voice_parts'][part]['source_artifact_id'] if part else current['parameters']['source_artifact_id']
        params={'model_id':model['id'],'index_rate':.5,'pitch_algorithm':'rmvpe','protect':.33,'full_source':True}
        if part:params['voice_part']=part
        run_job('pitch',{'pitch_algorithm':'rmvpe',**({'voice_part':part} if part else {})},source_id)
        run_job('convert',params,source_id)
        current=api('GET',f"/projects/{p['id']}");candidate=current['candidates'][-1]
        assert candidate['review_status']=='pending' and not candidate['selected']
        api('POST',f"/projects/{p['id']}/candidates/{candidate['id']}/review",json={'revision':current['revision'],'action':'accept'})
    run_job('mix')
    current=api('GET',f"/projects/{p['id']}");mixed=next(a for a in reversed(current['assets']) if a['stem_role']=='mix')
    job=run_job('export',{'artifact_id':mixed['id'],'format':'wav','name':name})
    p=api('GET',f"/projects/{p['id']}")
    evidence={'project_id':p['id'],'job_id':job['id'],'task':task,'source':str(path),'source_hash':media.sha256(path),'human_listening':'pending; acceptance clicks simulated for testing','candidate_count':len(p['candidates']),'final_count':len(p['final_list']),'exports':p['exports'],'artifacts':[{k:a.get(k) for k in ('id','sha256','stem_role','processing_kind','sample_rate','sample_count','channels','job_id','model_hash','input_artifact_ids','requested_f0_hash','consumed_f0_hash')} for a in p['assets']]}
    (result_dir/(task+'.json')).write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'task':task,'export':p['exports'][-1]['path'],'duration':p['exports'][-1]['duration']},ensure_ascii=True),flush=True)
    return evidence

if __name__=='__main__':
    song=ROOT.parent/'AllAboutUS.mp3';short=result_dir/'song-private-12s.wav'
    media.run([media.binary('ffmpeg'),'-v','error',*media.safe_input(song),'-ss','35','-t','12','-c:a','pcm_f32le','-y',str(short)])
    results=[run('cover',short,'SONORA 歌曲链路验收 · 12秒私有素材'),run('speech',ROOT.parent/'datasets/_pony_speech_mirror_probe/00000.wav','SONORA 语音链路验收'),run('rewrite',ROOT.parent/'datasets/_pony_speech_mirror_probe/00000.wav','SONORA 改词导唱路线技术验收',role='guide')]
    (result_dir/'summary.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
