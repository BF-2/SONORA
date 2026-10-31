"""Consolidated actual API acceptance for version/scoped bindings and legacy state.
Requires the private specialist QA session. No downloads, upgrades or training.
"""
from pathlib import Path
import argparse,copy,hashlib,json,math,os,sqlite3,sys,time,uuid
import httpx
import numpy as np
import soundfile as sf
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
OUT=ROOT/'tests/results/specialist';OUT.mkdir(parents=True,exist_ok=True)
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--run-real',action='store_true');parser.add_argument('--resume',action='store_true');args=parser.parse_args()
    if not args.run_real:raise SystemExit('Use --run-real for explicit model execution')
    conn=json.loads((ROOT/'runtime/specialist-session.json').read_text())
    client=httpx.Client(base_url=conn['endpoint']+'/api/v1',headers={'Authorization':'Bearer '+conn['token']},timeout=120)
    def api(method,path,**kwargs):
        r=client.request(method,path,**kwargs);r.raise_for_status();return r.json()
    def wait(job,allow_fail=False):
        for _ in range(1200):
            j=api('GET','/jobs/'+job['id'])
            if j['state'] in ('succeeded','failed','cancelled','blocked'):
                if not allow_fail and j['state']!='succeeded':raise AssertionError(j)
                return j
            time.sleep(.25)
        raise TimeoutError(job['id'])
    def run(pid,op,params=None,source=None,allow_fail=False):
        p=api('GET','/projects/'+pid);j=api('POST',f'/projects/{pid}/jobs',json={'revision':p['revision'],'idempotency_key':uuid.uuid4().hex,'operation':op,'input_artifact_id':source,'parameters':params or {}})
        result=wait(j,allow_fail);print(json.dumps({'operation':op,'state':result['state'],'job':j['id']}),flush=True);return result
    def patch(pid,values):
        p=api('GET','/projects/'+pid);return api('PATCH','/projects/'+pid,json={'revision':p['revision'],'patch':values})
    def import_project(name,task,path,role):
        if args.resume:
            old=next((p for p in api('GET','/projects')['projects'] if p['name']==name),None)
            if old:return old
        p=api('POST','/projects',json={'name':name,'task':task});return api('POST',f"/projects/{p['id']}/import-path",json={'path':str(path),'role':role})['project']
    models=api('GET','/catalog/models',params={'q':'twilight_sparkle_pony_singing_r18m_20260914','limit':250})['models']
    first=next(m for m in models if m.get('epoch')==200);second=next(m for m in models if m.get('epoch')==225)
    first=api('POST','/catalog/models/'+first['id']+'/validate');second=api('POST','/catalog/models/'+second['id']+'/validate')
    index=next(i for i in api('GET','/catalog/models/'+first['id']+'/indices')['indices'] if i['run_id']==first['run_id'])
    index_id=index['id']
    # Collect actual browser-created comparison and full/partial adoption results.
    dry=next(p for p in api('GET','/projects')['projects'] if p['name']=='专项统一验收 · 多 Epoch 干人声')
    group=next(g for g in dry['comparisons'] if g['state']=='succeeded')
    candidates=[c for c in dry['candidates'] if c.get('comparison_id')==group['id']]
    assert len(candidates)>=2 and len({c['source_artifact_id'] for c in candidates})==1
    assert len({c['pitch_artifact_id'] for c in candidates})==1
    assert len({c['requested_f0_hash'] for c in candidates})==1 and len({c['consumed_f0_hash'] for c in candidates})==1
    assert len({c['model_manifest_hash'] for c in candidates})==len(candidates)
    arrays=[sf.read(api('GET','/artifacts/'+c['artifact_id'])['path'],dtype='float64')[0] for c in candidates]
    assert len({len(a) for a in arrays})==1
    diff=float(np.sqrt(np.mean((arrays[0]-arrays[1])**2)));assert diff>1e-7
    assert any(c.get('assembly') for c in dry['candidates']) and dry['exports']
    report={'comparison':{'project_id':dry['id'],'group':group,'candidate_ids':[c['id'] for c in candidates],'waveform_rms_difference':diff},'dry_export':dry['exports'][-1]}
    # Real track -> clip override consumption, not merely saved API values.
    scoped=import_project('专项统一验收 · 轨道区间版本','speech',ROOT.parent/'demo/audio/original_demo_vocal.wav','vocal');pid=scoped['id'];track=scoped['tracks'][0]
    for scope,model in ([] if args.resume and scoped['candidates'] else [('track',first),('clip',second)]):
        p=api('GET','/projects/'+pid);body={'revision':p['revision'],'idempotency_key':uuid.uuid4().hex,'scope':scope,'model_id':model['id'],'index_id':index_id,'track_id':track['id'],'policy':'preserve_overrides'}
        if scope=='clip':body['range_ticks']=[96000,240000]
        r=api('POST','/projects/'+pid+'/model-selection',json=body)
        if scope=='clip':clip_id=r['selection']['clip_id']
    if args.resume and scoped['candidates']:
        candidate=scoped['candidates'][-1];a=api('GET','/artifacts/'+candidate['artifact_id']);scoped_job=api('GET','/jobs/'+a['job_id']);clip_id=candidate['parameters']['clip_id']
    else:
        patch(pid,{'model_parameter_updates':{'tracks':{track['id']:{'index_rate':.5,'protect':.15,'volume_envelope':.2}},'clips':{clip_id:{'transpose':12}}}})
        scoped_job=run(pid,'convert',source=track['artifact_id']);p=api('GET','/projects/'+pid);candidate=p['candidates'][-1];a=api('GET','/artifacts/'+candidate['artifact_id'])
    eff=a['worker_metadata']['effective_parameters'];assert a['model_hash']==second['model_hash'] and eff['transpose']==12 and eff['volume_envelope']==.2 and eff['protect']==.15
    assert a['sample_count']==a['sample_rate']*3 and a['timeline_offset_ticks']==96000
    assert a['worker_metadata']['retrieval_audit']['calls']>0
    report['scoped_consumption']={'project_id':pid,'job_id':scoped_job['id'],'clip_id':clip_id,'model_hash':a['model_hash'],'effective_parameters':eff,'index':a['worker_metadata']['index_metadata']}
    for scope,target_key,target_id in [('clip','clip_id',clip_id),('track','track_id',track['id'])]:
        current=api('GET','/projects/'+pid);api('POST','/projects/'+pid+'/model-selection/clear',json={'revision':current['revision'],'idempotency_key':uuid.uuid4().hex,'scope':scope,target_key:target_id})
    # Wrong-run index must fail, with no converted result or silent index_rate=0.
    wrong=next(i for i in api('GET','/catalog/models/'+first['id']+'/indices')['indices'] if i['run_id']!=first['run_id'])
    failed=run(pid,'convert',{'model_id':first['id'],'model_hash':first['model_hash'],'index_id':wrong['id'],'index_hash':None,'index_rate':.5,'ignore_clip_bindings':True,'track_id':None},source=track['artifact_id'],allow_fail=True)
    assert failed['state']=='failed';report['incompatible_index']={'job_id':failed['id'],'error':failed['error']}
    # Full song workflow on existing local material; recover clipping by explicit faders.
    song=import_project('专项统一验收 · 歌曲闭环','cover',ROOT/'tests/results/harmony/user-89-115/source.wav','original');sid=song['id']
    patch(sid,{'parameters':{**song['parameters'],'model_id':first['id'],'index_id':None,'index_rate':0,'volume_envelope':.5}})
    pipeline=run(sid,'pipeline',{'format':'wav','name':'专项歌曲闭环'},allow_fail=True)
    p=api('GET','/projects/'+sid);mix=next(a for a in reversed(p['assets']) if a['stem_role']=='mix');models_before=[a['id'] for a in p['assets'] if a['stem_role']=='converted']
    peak=max([mix['diagnostics']['sample_peak_dbfs']]+[next(a for a in p['assets'] if a['id']==t['artifact_id'])['diagnostics']['sample_peak_dbfs']+t['gain_db'] for t in p['tracks'] if not t['mute'] and t['role']!='mix'])
    gain=-math.ceil(max(0,peak+1.5)*10)/10
    if gain:
        patch(sid,{'track_updates':{t['id']:{'gain_db':t['gain_db']+gain} for t in p['tracks'] if not t['mute'] and t['role']!='mix'}});run(sid,'mix')
    p=api('GET','/projects/'+sid);mix=next(a for a in reversed(p['assets']) if a['stem_role']=='mix');run(sid,'export',{'artifact_id':mix['id'],'name':'专项歌曲闭环 · 已留余量','model_converted':True})
    p=api('GET','/projects/'+sid);assert p['exports'][-1]['duration']==26 and p['exports'][-1]['measurements']['true_peak_dbfs']<0
    assert models_before==[a['id'] for a in p['assets'] if a['stem_role']=='converted']
    report['song']={'project_id':sid,'initial_pipeline_state':pipeline['state'],'recovery_without_model_rerun':True,'fader_gain_db':gain,'export':p['exports'][-1]}
    # Cancel a genuinely running worker with a unique seed (avoid an immediate cache hit).
    p=api('GET','/projects/'+sid);source=next(a for a in p['assets'] if a['stem_role']=='vocal');j=api('POST',f'/projects/{sid}/jobs',json={'revision':p['revision'],'idempotency_key':uuid.uuid4().hex,'operation':'convert','input_artifact_id':source['id'],'parameters':{'seed':38291,'model_id':first['id'],'index_rate':0}})
    for _ in range(240):
        j=api('GET','/jobs/'+j['id'])
        if j['state']=='running' and '声音转换' in j['stage']:break
        if j['state'] in ('failed','blocked','succeeded'):raise AssertionError(j)
        time.sleep(.1)
    api('POST','/jobs/'+j['id']+'/cancel',json={});cancelled=wait(j,True);assert cancelled['state']=='cancelled';report['cancelled_worker']=cancelled['id']
    # Snapshot migration: all old projects and IDs remain byte-for-byte stable.
    legacy=OUT/'legacy-migration.sqlite'
    with sqlite3.connect(ROOT/'runtime/sonora.sqlite') as original,sqlite3.connect(legacy) as target:original.backup(target)
    with sqlite3.connect(legacy) as db:before=dict(db.execute('SELECT id,body FROM projects'))
    from services.orchestrator.model_catalog import ModelCatalog
    catalog=ModelCatalog(legacy);catalog.scan(max_files=5000,max_seconds=30)
    configured=json.loads((ROOT/'engine-envs/resources.json').read_text(encoding='utf-8'))['models']
    assert all(catalog.get_model(m['id'])['model_hash']==m['model_hash'] for m in configured)
    with sqlite3.connect(legacy) as db:assert before==dict(db.execute('SELECT id,body FROM projects'))
    report['legacy_migration']={'projects_unchanged':len(before),'legacy_model_ids_unchanged':len(configured),'snapshot':str(legacy)}
    report['passed']=True;(OUT/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'report':str(OUT/'acceptance.json')},ensure_ascii=True),flush=True)
if __name__=='__main__':main()
