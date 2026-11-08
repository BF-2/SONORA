"""Real production API/worker acceptance for all six installed Hay Say models."""
import os,sys,json,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
OUT=Path(os.environ.get('SONORA_HAYSAY_VERIFY_DIR',str(ROOT/'runtime/haysay-acceptance')))
os.environ['SONORA_DATA']=str(OUT/'data');os.environ['SONORA_SESSION_TOKEN']='haysay-check-'+uuid.uuid4().hex
from fastapi.testclient import TestClient
from services.orchestrator.app import app,store,catalog
import numpy as np,soundfile as sf
def main():
    OUT.mkdir(parents=True,exist_ok=True);src=Path('E:/voice/RVC_pretrained_compare_20261104/sources/zh.wav');a,sr=sf.read(src,dtype='float32');source=OUT/'vocal-6s.wav';sf.write(source,a[15*sr:21*sr],sr,subtype='FLOAT')
    results=[]
    with TestClient(app) as c:
        headers={'Authorization':'Bearer '+os.environ['SONORA_SESSION_TOKEN']}
        def api(method,path,**kw):
            r=c.request(method,'/api/v1'+path,headers=headers,**kw)
            if r.status_code>=400:raise RuntimeError(r.text)
            return r.json()
        models=api('GET','/catalog/models')['models'];assert len(models)==6 and all(m['provider']=='haysay' for m in models)
        for m in models:
            manifest=catalog.resolve_model(m['id']);assert manifest['index_match']['fully_validated'] and manifest['speaker_ids']==[0]
            p=api('POST','/projects',json={'name':m['name']+' · Hay Say 接入验收','task':'speech'})
            p=api('POST',f"/projects/{p['id']}/import-path",json={'path':str(source),'role':'vocal'})['project']
            job=api('POST',f"/projects/{p['id']}/jobs",json={'revision':p['revision'],'operation':'convert','idempotency_key':uuid.uuid4().hex,'parameters':{'model_id':m['id'],'index_id':manifest['index_id'],'index_rate':.5,'pitch_algorithm':'rmvpe','protect':.33,'full_source':True,'ignore_clip_bindings':True}})
            for _ in range(1200):
                job=api('GET','/jobs/'+job['id'])
                if job['state'] in ['succeeded','failed','blocked','cancelled']:break
                time.sleep(.25)
            assert job['state']=='succeeded',job.get('error')
            p=api('GET',f"/projects/{p['id']}");candidate=p['candidates'][-1];artifact=api('GET','/artifacts/'+candidate['artifact_id']);audio,out_sr=sf.read(artifact['path'],dtype='float32')
            assert np.isfinite(audio).all() and np.sqrt(np.mean(audio.astype('float64')**2))>1e-5 and abs(len(audio)/out_sr-6)<.2
            assert artifact['model_hash']==manifest['model_hash']
            actual_index=artifact['worker_metadata']['index_metadata']
            assert actual_index['sha256']==manifest['index_hash'] and actual_index['dimension']==768
            assert artifact['worker_metadata']['effective_parameters']['index_rate']==.5
            result=dict(model_id=m['id'],model_hash=m['model_hash'],index_id=manifest['index_id'],index_hash=manifest['index_hash'],job_id=job['id'],sample_rate=out_sr,seconds=len(audio)/out_sr,artifact_id=artifact['id'],artifact_path=artifact['path'],worker_metadata=artifact.get('worker_metadata'),candidate_parameters=candidate.get('parameters'))
            results.append(result);print('REAL_API_PASS',m['id'],out_sr,round(len(audio)/out_sr,3),flush=True)
    (OUT/'verification.json').write_text(json.dumps(dict(passed=True,models=results,local_audio_only=True,production_projects_modified=False),ensure_ascii=False,indent=2),encoding='utf-8')
if __name__=='__main__':main()
