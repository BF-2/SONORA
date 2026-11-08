"""Verify the running authenticated local service, no token printed."""
from pathlib import Path
import json,time,uuid,sys
import httpx
ROOT=Path(__file__).resolve().parents[1]
session=json.loads((ROOT/'runtime/ui-session.json').read_text(encoding='utf-8'))
client=httpx.Client(base_url=session['endpoint']+'/api/v1',headers={'Authorization':'Bearer '+session['token']},timeout=120)
def call(method,path,**kwargs):
    r=client.request(method,path,**kwargs);r.raise_for_status();return r.json()
def wait(job):
    for _ in range(180):
        j=call('GET','/jobs/'+job['id'])
        if j['state'] in ('failed','blocked','cancelled'):raise RuntimeError(str(j['error']))
        if j['state']=='succeeded':return j
        time.sleep(.5)
    raise TimeoutError('job exceeded 90 seconds')
p=call('POST','/projects',json={'name':'SONORA 日语新词合成验收','task':'rewrite'})
notes=[{'id':'note'+str(i),'start':round((.5+i*.5)*48000),'end':round((1+i*.5)*48000),'pitch_midi_float':60 if i<2 else 67,'reference_source':'manual','lyric':lyric,'phonemes':''} for i,lyric in enumerate('きらきら')]
p=call('PATCH','/projects/'+p['id'],json={'revision':p['revision'],'patch':{'notes':notes,'selection':{'start':0,'end':144000}}})
j=wait(call('POST',f"/projects/{p['id']}/jobs",json={'revision':p['revision'],'idempotency_key':uuid.uuid4().hex,'operation':'svs','parameters':{'duration_ticks':144000,'language':'ja','steps':10}}))
p=call('GET','/projects/'+p['id']);a=next(a for a in p['assets'] if a['stem_role']=='synthesized')
assert a['sample_count']==132300 and a['sample_rate']==44100
j2=wait(call('POST',f"/projects/{p['id']}/jobs",json={'revision':p['revision'],'idempotency_key':uuid.uuid4().hex,'operation':'export','parameters':{'artifact_id':a['id'],'model_converted':True,'format':'wav','name':'SONORA 日语新词演唱 · きらきら'}}))
p=call('GET','/projects/'+p['id'])
evidence={'project_id':p['id'],'job_id':j['id'],'artifact':a,'exports':p['exports'],'real_svs':True,'language_scope':'ja','subjective_intelligibility':'requires_user_listening'}
out=ROOT/'tests/results/integration/svs-api.json';out.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'svs_api':'passed','samples':a['sample_count'],'duration':3.0,'project_id':p['id']},ensure_ascii=True))
