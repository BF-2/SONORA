from __future__ import annotations
import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
import time
import re
from typing import Any
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.responses import Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from . import media
from .config import DATA, ROOT, TIMEBASE, TOKEN, ORIGINS, resources, scoped_path
from .scheduler import Scheduler
from .store import Store, Conflict, now, uid
from .editing import freeze_locks
from .midi import read_midi,write_midi

app=FastAPI(title='声序 SONORA',version='1.0.0',docs_url=None,redoc_url=None,openapi_url=None)
store=Store(); scheduler=Scheduler(store)
registry_lock=threading.RLock()

@app.middleware('http')
async def guard(request:Request,call_next):
    host=request.headers.get('host','').split(':')[0].lower()
    if host not in ('127.0.0.1','localhost','testserver'):
        return JSONResponse({'detail':'Host 不在本地服务允许列表。'},403)
    origin=request.headers.get('origin')
    if origin and origin not in ORIGINS:
        return JSONResponse({'detail':'Origin 不在本地服务允许列表。'},403)
    if request.url.path.startswith('/api/') and request.url.path!='/api/v1/health' and request.method!='OPTIONS':
        supplied=request.headers.get('authorization','')
        if not TOKEN or not secrets.compare_digest(supplied,'Bearer '+TOKEN):
            return JSONResponse({'detail':'需要当前会话凭据，请从声序启动器打开。'},401)
    if request.method in ('POST','PUT','PATCH'):
        try: length=int(request.headers.get('content-length','0'))
        except ValueError: return JSONResponse({'detail':'无效请求长度。'},400)
        limit=512*1024*1024 if '/import' in request.url.path or '/restore' in request.url.path else 4*1024*1024
        if length>limit: return JSONResponse({'detail':'文件或请求超过大小限制。'},413)
    result=await call_next(request)
    result.headers['X-Content-Type-Options']='nosniff'
    result.headers['Referrer-Policy']='no-referrer'
    if request.url.path.startswith('/api/'): result.headers['Cache-Control']='no-store'
    return result

app.add_middleware(CORSMiddleware,allow_origins=list(ORIGINS),allow_methods=['GET','POST','PATCH','OPTIONS'],allow_headers=['Authorization','Content-Type','Last-Event-ID','Range'],expose_headers=['Content-Disposition','Content-Range'])

@app.exception_handler(KeyError)
async def missing(_request,exc): return JSONResponse({'detail':'工程或产物不存在。'},404)
@app.exception_handler(Conflict)
async def conflict(_request,exc): return JSONResponse({'detail':str(exc)},409)
@app.exception_handler(ValueError)
async def invalid(_request,exc): return JSONResponse({'detail':str(exc)},400)
@app.exception_handler(OSError)
async def io_failure(_request,exc): return JSONResponse({'detail':'文件访问失败，请检查路径、剩余空间与权限。','code':str(exc.errno)},400)

@app.get('/api/v1/health')
def health(): return {'name':'SONORA','protocol_version':1,'status':'ready'}

@app.get('/api/v1/schema')
def schema(): return app.openapi()

@app.get('/api/v1/capabilities')
def capabilities():
    config=resources()
    installed=bool(config.get('models'))
    tested_path=ROOT/'tests/results/workers/verification.json'
    tested=json.loads(tested_path.read_text(encoding='utf-8')) if tested_path.exists() else None
    engines=[{'id':'ffmpeg','name':'FFmpeg · 解码与后期','status':'ready','capabilities':{'decode':True,'export':['wav','flac','mp3'],'effects':['highpass','lowpass','compressor','delay','limiter']},'test_status':'signal_tests'},
             {'id':'separation','name':'MelBand RoFormer · 人声分离','status':'ready' if config.get('separation',{}).get('models') else 'unavailable','reason':'按实际权重配置映射人声与伴奏；不保证主唱/和声可分开','models':config.get('separation',{}).get('models',[]),'capabilities':{'stem_separation':True,'local_segment_render':False}},
             {'id':'pitch','name':'独立音高服务','status':'ready' if installed else 'unavailable','capabilities':{'algorithms':['rmvpe','fcpe','crepe','crepe-tiny','pyin','harvest','dio'],'external_f0':True},'test_status':'installed; see engine verification'},
             {'id':'rvc','name':'Applio / RVC','status':'ready' if installed and os.environ.get('SONORA_DISABLE_MODEL_WORKERS')!='1' else 'unavailable','reason':'真实外部 F0 条件；模型音质仍需人工试听','capabilities':{'voice_conversion':True,'singing_conversion':True,'external_f0':True,'streaming':False,'svs':False,'local_segment_render':False},'test_status':'see per-model manifest'},
             {'id':'svs','name':'DiffSinger · Nishiren Gard AI','status':'ready' if config.get('svs',{}).get('models') else 'unavailable','reason':'已接入日语假名/显式音素 + 音符的锁定窗口合成。其他语言请使用新导唱路线。','default_model':config.get('svs',{}).get('default_model'),'models':[{k:v for k,v in m.items() if k!='components'} for m in config.get('svs',{}).get('models',[])],'capabilities':{'svs':bool(config.get('svs',{}).get('models')),'languages':['ja'],'notes':True,'phoneme_timing':True,'timing_policy':['locked_window'],'max_phrase_seconds':60}},
             {'id':'realtime','name':'实时会话','status':'unavailable','reason':'尚未配置与验证流式执行器和音频设备路由。','capabilities':{'streaming':False}}]
    return {'protocol_version':1,'engines':engines,'device':{'name':'本地执行器','inference':'CPU / CUDA 按执行器实测','max_parallel_gpu_jobs':1},'verification':tested}

@app.get('/api/v1/models')
def models(): return {'models':resources().get('models',[])}

@app.post('/api/v1/models/import')
def import_model(body:dict):
    path=scoped_path(body.get('path',''))
    if path.suffix.lower()!='.pth' or path.stat().st_size>2*1024**3: raise ValueError('首发支持不超过 2 GiB 的 RVC .pth；压缩包和任意代码不直接加载。')
    config=resources(); python=config.get('applio',{}).get('python')
    if not python: raise ValueError('缺少 RVC 安全探测环境。')
    result=subprocess.run([python,str(ROOT/'workers/rvc/probe_model.py'),str(path)],capture_output=True,text=True,encoding='utf-8',timeout=90,creationflags=media.NO_WINDOW,env={**os.environ,'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1','TORCH_FORCE_WEIGHTS_ONLY_LOAD':'1'})
    if result.returncode: raise ValueError('安全模型探测失败：'+result.stderr[-500:])
    probe=json.loads(result.stdout)[0]
    if not probe.get('ok'): raise ValueError('模型不兼容或无法受限加载：'+str(probe.get('error','unknown')))
    digest=probe['model_hash'];dest=ROOT/'resources/models/imported'/digest[:16]/path.name; dest.parent.mkdir(parents=True,exist_ok=True)
    if not dest.exists():
        try: os.link(path,dest)
        except OSError: shutil.copyfile(path,dest)
    manifest={**probe,'id':'imported_'+digest[:16],'name':path.stem,'path':str(dest),'origin_path':str(path),'license':'unconfirmed','code_license':'MIT (Applio)','engine_revision':config['applio']['revision']}
    index=scoped_path(body['index_path']) if body.get('index_path') else path.with_suffix('.index')
    if index and index.is_file():
        scoped_path(index);index_dest=dest.parent/index.name
        if not index_dest.exists():
            try: os.link(index,index_dest)
            except OSError: shutil.copyfile(index,index_dest)
        manifest.update(index_path=str(index_dest),index_hash=media.sha256(index_dest))
    with registry_lock:
        config=resources();config['models']=[m for m in config['models'] if m['id']!=manifest['id']]+[manifest]
        cfg=ROOT/'engine-envs/resources.json';tmp=cfg.with_suffix('.tmp');tmp.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(tmp,cfg)
    return manifest

@app.get('/api/v1/presets')
def presets(): return {'presets':[{'id':'natural','name':'自然对白','version':1,'parameters':{'transpose':0,'index_rate':0,'protect':0.33,'pitch_algorithm':'rmvpe'},'explanation':'保留原始音高，不默认吸附音阶。'},{'id':'singing','name':'贴近原唱','version':1,'parameters':{'transpose':0,'index_rate':0.5,'protect':0.33,'pitch_algorithm':'rmvpe'},'explanation':'需要检索索引；先检查分离后主唱。'},{'id':'quick','name':'快速试听','version':1,'parameters':{'transpose':0,'index_rate':0,'protect':0.33,'pitch_algorithm':'fcpe'},'explanation':'短选区试听，实际速度以本机记录为准。'}]}

@app.get('/api/v1/projects')
def projects(): return {'projects':store.all('projects')}

@app.post('/api/v1/projects')
def create_project(body:dict):
    name=str(body.get('name') or '未命名工程')[:120]; task=body.get('task','speech')
    if task not in ('speech','cover','rewrite'): raise ValueError('无效的制作任务。')
    return store.new_project(name,task)

@app.get('/api/v1/projects/{pid}')
def project(pid:str): return store.get('projects',pid)

def check_structure(value,depth=0):
    if depth>16: raise ValueError('工程结构嵌套过深。')
    if isinstance(value,dict):
        if len(value)>1000: raise ValueError('工程对象过大。')
        for k,v in value.items():
            if k in ('__proto__','constructor','prototype'): raise ValueError('不允许的对象键。')
            check_structure(v,depth+1)
    elif isinstance(value,list):
        if len(value)>20000: raise ValueError('编辑列表过长。')
        for v in value: check_structure(v,depth+1)
    elif isinstance(value,float): media.bounded(value,-1e15,1e15)
    elif isinstance(value,str) and len(value)>100000: raise ValueError('文本超长。')

def validate_patch(p,patch):
    allowed={'name','task','selection','playhead','mode','parameters','pitch_edits','notes','lyrics','tracks','candidates','selected_candidate_id','warnings'}
    if set(patch)-allowed: raise ValueError('不可直接修改产物、图结构或工程身份。')
    check_structure(patch)
    if patch.get('mode',p['mode']) not in ('quick','detail','expert'): raise ValueError('无效编辑模式。')
    if patch.get('task',p['task']) not in ('speech','cover','rewrite'): raise ValueError('无效任务类型。')
    selection=patch.get('selection',p['selection'])
    if not isinstance(selection,dict) or not all(isinstance(selection.get(k),int) for k in ('start','end')) or selection['start']<0 or selection['end']<selection['start']: raise ValueError('选区必须为非负整数 tick 且结束不早于开始。')
    if 'playhead' in patch and (not isinstance(patch['playhead'],int) or patch['playhead']<0): raise ValueError('播放头必须是整数 tick。')
    for edit in patch.get('pitch_edits',[]):
        if edit.get('operation') not in ('transpose','set_hz','unvoiced'): raise ValueError('不支持的音高编辑。')
        if not all(isinstance(edit.get(k),int) for k in ('start_tick','end_tick')) or edit['end_tick']<=edit['start_tick'] or edit['start_tick']<0: raise ValueError('音高区间无效。')
        if 'hz' in edit: media.bounded(edit['hz'],20,2000)
        if 'semitones' in edit: media.bounded(edit['semitones'],-36,36)
    for lyric in patch.get('lyrics',[]):
        if lyric.get('timing_policy','locked_window')!='locked_window': raise ValueError('当前只启用锁定窗口；全轨 ripple 编辑尚未验证。')
        if not all(isinstance(lyric.get(k),int) for k in ('start','end')) or lyric['end']<=lyric['start'] or lyric['start']<0: raise ValueError('歌词必须有有效时间窗口。')
    for note in patch.get('notes',[]):
        if not all(isinstance(note.get(k),int) for k in ('start','end')) or note['end']<=note['start'] or note['start']<0: raise ValueError('音符必须有有效整数 tick 时间窗口。')
        media.bounded(note.get('pitch_midi_float'),24,108)
    owned={a['id'] for a in p['assets']}
    for t in patch.get('tracks',[]):
        if t.get('artifact_id') not in owned: raise ValueError('轨道引用未知产物。')
        media.bounded(t.get('gain_db',0),-96,24);media.bounded(t.get('pan',0),-1,1);media.effects_filter(t.get('effects',[]))
        if not isinstance(t.get('offset_ticks',0),int): raise ValueError('轨道偏移必须为整数 tick。')
    params=patch.get('parameters',{})
    for key,lo,hi in [('transpose',-36,36),('index_rate',0,1),('protect',0,.5),('volume_envelope',0,1),('segment_size',32,2048),('overlap',1,16)]:
        if key in params: media.bounded(params[key],lo,hi)
    if 'candidates' in patch:
        old={c['id']:c for c in p['candidates']}
        for c in patch['candidates']:
            original=old.get(c.get('id'))
            if not original: raise ValueError('候选只能由成功推理创建。')
            for key in ('source_artifact_id','artifact_id','model_id','generation_depth'):
                if c.get(key)!=original.get(key): raise ValueError('候选推理来源不可编辑。')

@app.patch('/api/v1/projects/{pid}')
def patch_project(pid:str,body:dict):
    if not isinstance(body.get('revision'),int): raise ValueError('保存需要工程版本号。')
    patch=body.get('patch',{})
    def mutate(p):
        validate_patch(p,patch)
        freeze_locks(p,patch,store)
        changed=set(patch)&{'parameters','pitch_edits','notes','lyrics','tracks'}
        p.update(copy.deepcopy(patch))
        if changed:
            affected='mix_export' if changed=={'tracks'} else 'conversion_mix_export'
            for node in p['graph']:
                if affected=='conversion_mix_export' and node['type'] in ('convert','pipeline','mix','export') or affected=='mix_export' and node['type'] in ('mix','export'):
                    node['state']='stale';node['invalidation_scope']='whole_node_context_unknown';node['reason']='编辑变更，保留旧版本试听，新版本待渲染'
            p['warnings']=[w for w in p['warnings'] if w.get('code')!='pending_render']+[{'code':'pending_render','message':'新版本待试听：旧产物仍可播放；重新运行受影响节点。','scope':affected}]
        if 'selected_candidate_id' in patch and patch['selected_candidate_id']:
            selected=next((c for c in p['candidates'] if c['id']==patch['selected_candidate_id']),None)
            if not selected: raise ValueError('候选不存在。')
            for c in p['candidates']: c['selected']=c['id']==selected['id']
            for t in p['tracks']:
                if t['role'] in ('converted','vocal','guide','original','mix'): t['mute']=t['artifact_id']!=selected['artifact_id']
    return store.change(pid,mutate,revision=body.get('revision'))

@app.post('/api/v1/projects/{pid}/history')
def history(pid:str,body:dict):
    if body.get('direction') not in ('undo','redo'): raise ValueError('未知历史操作。')
    return store.history(pid,body['direction'],body.get('revision'))

@app.post('/api/v1/projects/{pid}/midi')
async def import_midi(pid:str,file:UploadFile=File(...),revision:int=Form(...)):
    content=await file.read(5*1024*1024+1);notes=read_midi(content)
    return store.change(pid,lambda p:p.update(notes=notes),revision=revision)

@app.get('/api/v1/projects/{pid}/midi')
def export_midi(pid:str):
    p=store.get('projects',pid)
    return Response(write_midi(p['notes']),media_type='audio/midi',headers={'Content-Disposition':'attachment; filename="sonora-notes.mid"'})

@app.post('/api/v1/projects/{pid}/experiments')
def experiments(pid:str,body:dict):
    p=store.get('projects',pid)
    if body.get('revision')!=p['revision']:raise Conflict('工程已更新，请刷新后再实验。')
    variants=body.get('variants',[])
    if not 1<=len(variants)<=8:raise ValueError('单次实验需要 1–8 组参数。')
    allowed={'transpose','index_rate','protect','pitch_algorithm','seed'}
    base_key=body.get('idempotency_key')
    if not base_key or len(base_key)>100:raise ValueError('实验需要有效幂等键。')
    for variant in variants:
        if set(variant)-allowed:raise ValueError('实验包含不支持的变量。')
        validate_patch(p,{'parameters':variant})
    jobs=[]
    # Immutable shared source and base revision, serial resource-budgeted execution.
    with store.lock:
        for i,variant in enumerate(variants):
            jobs.append(public_job(scheduler.submit(pid,{'revision':p['revision'],'operation':'convert','input_artifact_id':body.get('input_artifact_id'),'idempotency_key':base_key+'-'+str(i),'parameters':{**variant,'seed':variant.get('seed',0),'range_ticks':body.get('range_ticks') or [p['selection']['start'],p['selection']['end']]}})))
    return {'jobs':jobs,'comparison':'same_source_fixed_seed_except_explicit_variant','max_concurrency':1}

def attach_import(pid,a):
    def mutate(p):
        p['assets'].append(a)
        role=a['stem_role'];duration=round(a['sample_count']*TIMEBASE/a['sample_rate'])
        p['tracks'].append({'id':uid('track'),'name':a['name'],'role':role,'artifact_id':a['id'],'gain_db':0,'pan':0,'mute':False,'solo':False,'offset_ticks':0,'effects':[]})
        if not p['selection']['end']: p['selection']={'start':0,'end':duration}
        if role=='original' and len(p['assets'])==1 and p['name']=='未命名工程': p['name']=Path(a['name']).stem
    return {'project':store.change(pid,mutate),'artifact':a}

async def upload(file:UploadFile,limit=512*1024*1024):
    suffix=Path(file.filename or '').suffix.lower()
    dest=DATA/'uploads'/(uid('upload')+suffix);total=0
    try:
        with dest.open('wb') as target:
            while chunk:=await file.read(1024*1024):
                total+=len(chunk)
                if total>limit: raise ValueError('文件超过允许大小。')
                target.write(chunk)
        return dest
    except Exception:
        dest.unlink(missing_ok=True);raise

@app.post('/api/v1/projects/{pid}/import')
async def import_file(pid:str,file:UploadFile=File(...),role:str=Form('original')):
    store.get('projects',pid)
    if role not in ('original','vocal','instrumental','guide'): raise ValueError('不支持的素材角色。')
    path=await upload(file)
    try:
        a=await asyncio.to_thread(media.import_audio,store,path,role);a['name']=Path(file.filename or '音频').name;store.put('artifacts',a)
        return attach_import(pid,a)
    finally: path.unlink(missing_ok=True)

@app.post('/api/v1/projects/{pid}/import-path')
def import_path(pid:str,body:dict):
    store.get('projects',pid);role=body.get('role','original')
    if role not in ('original','vocal','instrumental','guide'): raise ValueError('不支持的素材角色。')
    a=media.import_audio(store,scoped_path(body.get('path','')),role)
    return attach_import(pid,a)

@app.post('/api/v1/projects/{pid}/demo')
def demo(pid:str):
    import numpy as np
    import soundfile as sf
    store.get('projects',pid);sr=48000;t=np.arange(sr*8)/sr
    vocal=(.2*np.sin(2*np.pi*220*t)+.05*np.sin(2*np.pi*440*t))*(np.sin(2*np.pi*.75*t)**2)
    backing=.06*np.sin(2*np.pi*110*t)+.04*np.sin(2*np.pi*165*t)
    path=DATA/'uploads'/(uid('demo')+'.wav');sf.write(path,np.column_stack([vocal+backing,vocal+backing]),sr,subtype='FLOAT')
    a=media.commit(store,path,kind='demo',role='original',name='技术合成信号 · 非真实歌声',extra={'fixture_kind':'synthetic_signal'})
    return attach_import(pid,a)

@app.get('/api/v1/projects/{pid}/recipe')
def recipe(pid:str):
    p=copy.deepcopy(store.get('projects',pid))
    pending=p.pop('restore_pending',None)
    if pending and p.get('missing_media'):
        source=pending['source'];mapping=pending['mapping'];missing={m['old_id'] for m in p['missing_media']}
        p['assets'] += [a for a in source['assets'] if a['id'] in missing]
        track_ids={t['id'] for t in p['tracks']}
        p['tracks'] += [{**t,'artifact_id':mapping.get(t['artifact_id'],t['artifact_id'])} for t in source['tracks'] if t['id'] not in track_ids]
        candidate_ids={c['id'] for c in p['candidates']}
        p['candidates'] += [{**c,'artifact_id':mapping.get(c['artifact_id'],c['artifact_id']),'source_artifact_id':mapping.get(c['source_artifact_id'],c['source_artifact_id'])} for c in source['candidates'] if c['id'] not in candidate_ids]
    for a in p['assets']:
        for key in ('path','original_uri','original_snapshot'): a.pop(key,None)
    for e in p['exports']: e.pop('path',None)
    used={c['model_id'] for c in p['candidates']}
    manifests=[{k:v for k,v in m.items() if 'path' not in k} for m in resources().get('models',[]) if m['id'] in used]
    return JSONResponse({'format':'sonora-recipe','schema_version':1,'project':p,'model_manifests':manifests,'media_included':False,'reproducibility':'recipe-rerunnable; cross-device bit equality not promised'},headers={'Content-Disposition':'attachment; filename="sonora-recipe.json"'})

def restored_candidate(candidate,mapping):
    output=store.get('artifacts',mapping[candidate['artifact_id']])
    expected_source=mapping[candidate['source_artifact_id']]
    ancestors=set()
    def walk(a,seen):
        if a['id'] in seen:raise ValueError('产物依赖图含循环。')
        seen=seen|{a['id']};ancestors.add(a['id'])
        children=[store.get('artifacts',ref) for ref in a.get('input_artifact_ids',[])]
        depth=max((walk(child,seen) for child in children),default=0)
        return depth+(1 if a.get('engine')=='rvc' and a.get('processing_kind')=='model_inference' else 0)
    depth=walk(output,set())
    if expected_source not in ancestors: raise ValueError('配方候选来源不符合已提交产物血缘。')
    model=next((m for m in resources().get('models',[]) if m.get('model_hash')==output.get('model_hash')),None)
    return {**candidate,'artifact_id':output['id'],'source_artifact_id':expected_source,'generation_depth':max(1,depth),'model_id':model['id'] if model else 'unresolved','model_manifest_hash':output.get('model_hash'),'state':'succeeded' if depth else 'unverified_restored_media'}

@app.post('/api/v1/projects/restore')
async def restore(file:UploadFile=File(...)):
    content=await file.read(4*1024*1024+1)
    if len(content)>4*1024*1024: raise ValueError('配方过大。')
    value=json.loads(content);check_structure(value)
    if value.get('schema_version')!=1 or value.get('format')!='sonora-recipe': raise ValueError('未知或未来版本工程，拒绝覆盖。')
    source=value['project'];source.pop('restore_pending',None)
    for c in source.get('candidates',[]):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',str(c.get('id',''))): raise ValueError('配方候选标识无效。')
    p=store.new_project(str(source.get('name','恢复工程'))[:120],source.get('task','speech'))
    lookup={a['sha256']:a for a in store.all('artifacts')};mapping={};missing=[]
    assets=[]
    for a in source.get('assets',[]):
        found=lookup.get(a.get('sha256'))
        if found:
            try: media.validate_artifact(found)
            except (ValueError,OSError): found=None
        if found: assets.append(found);mapping[a['id']]=found['id']
        else: missing.append({'hash':a.get('sha256'),'original_hash':a.get('original_hash'),'name':a.get('name'),'old_id':a.get('id')})
    def mutate(project):
        for key in ('selection','playhead','mode','parameters','pitch_edits','notes','lyrics'):
            if key in source: project[key]=source[key]
        project['assets']=assets
        project['tracks']=[{**t,'artifact_id':mapping[t['artifact_id']]} for t in source.get('tracks',[]) if t.get('artifact_id') in mapping]
        project['candidates']=[]
        for c in source.get('candidates',[]):
            if c.get('artifact_id') in mapping and c.get('source_artifact_id') in mapping:
                project['candidates'].append(restored_candidate(c,mapping))
        project['selected_candidate_id']=source.get('selected_candidate_id') if any(c['id']==source.get('selected_candidate_id') for c in project['candidates']) else None
        project['missing_media']=missing
        project['restore_pending']={'source':source,'mapping':mapping}
        model_ids={m['id'] for m in resources().get('models',[])}
        project['warnings']=[{'code':'missing_media','message':f"缺少素材 {m['name']}，请按哈希重新链接。",**m} for m in missing]
        if project['parameters'].get('model_id') and project['parameters']['model_id'] not in model_ids: project['warnings'].append({'code':'missing_model','message':'配方模型缺失，请导入匹配哈希的模型；不会自动替换。'})
        validate_patch(project,{k:project[k] for k in ('selection','playhead','mode','parameters','pitch_edits','notes','lyrics','tracks')})
    return store.change(p['id'],mutate)

@app.post('/api/v1/projects/{pid}/relink')
def relink(pid:str,body:dict):
    p=store.get('projects',pid);missing=next((m for m in p.get('missing_media',[]) if m['hash']==body.get('hash')),None)
    if not missing: raise ValueError('未找到待重新链接的素材。')
    path=scoped_path(body['path'])
    digest=media.sha256(path)
    if digest not in (missing['hash'],missing.get('original_hash')): raise ValueError('素材哈希不匹配，不会替换为同名文件。')
    original=next(x for x in p['restore_pending']['source']['assets'] if x['id']==missing['old_id'])
    if digest==missing.get('original_hash'):
        a=media.import_audio(store,path,original.get('stem_role','original'))
        if a['sha256']!=missing['hash']: raise ValueError('重新解码结果与配方不一致，请选择原有缓存产物或用新素材创建修订。')
    else:
        # Restore exact bytes only. Never accept serialized job provenance from a recipe as a new inference claim.
        temp=DATA/'uploads'/(uid('relink')+path.suffix.lower());shutil.copyfile(path,temp)
        a=media.commit(store,temp,kind='decode',role=original.get('stem_role','original'),name=original['name'],extra={'restored_provenance_unverified':True,'original_hash':original.get('original_hash')})
    def mutate(project):
        project['assets'].append(a);pending=project['restore_pending'];pending['mapping'][missing['old_id']]=a['id'];mapping=pending['mapping'];source=pending['source']
        project['tracks']=[{**t,'artifact_id':mapping[t['artifact_id']]} for t in source.get('tracks',[]) if t.get('artifact_id') in mapping]
        project['candidates']=[]
        for c in source.get('candidates',[]):
            if c.get('artifact_id') in mapping and c.get('source_artifact_id') in mapping:
                try:project['candidates'].append(restored_candidate(c,mapping))
                except ValueError:project['warnings'].append({'code':'restored_candidate_unverified','message':'重新链接的媒体可播放，但缺少原推理血缘，需重新渲染候选。'})
        project['missing_media']=[m for m in project.get('missing_media',[]) if m['old_id']!=missing['old_id']]
        project['warnings']=[w for w in project['warnings'] if w.get('old_id')!=missing['old_id']]
        wanted=source.get('selected_candidate_id')
        if any(c['id']==wanted for c in project['candidates']):project['selected_candidate_id']=wanted
    return {'project':store.change(pid,mutate),'artifact':a}

@app.post('/api/v1/projects/{pid}/jobs')
def start_job(pid:str,body:dict):
    check_structure(body)
    p=store.get('projects',pid);validate_patch(p,{'parameters':body.get('parameters',{})})
    return scheduler.submit(pid,body)

def public_job(j): return {k:v for k,v in j.items() if k!='project_snapshot'}
@app.get('/api/v1/jobs')
def jobs(project_id:str|None=None): return {'jobs':[public_job(j) for j in store.all('jobs') if project_id is None or j['project_id']==project_id]}
@app.get('/api/v1/jobs/{jid}')
def job(jid:str): return public_job(store.get('jobs',jid))
@app.post('/api/v1/jobs/{jid}/cancel')
def cancel_job(jid:str): return public_job(scheduler.cancel(jid))
@app.post('/api/v1/jobs/{jid}/retry')
def retry_job(jid:str):
    original=store.get('jobs',jid)
    if original['state'] not in ('failed','cancelled','blocked'): raise ValueError('此任务不需要重试。')
    p=store.get('projects',original['project_id'])
    return public_job(scheduler.submit(p['id'],{'revision':p['revision'],'operation':original['operation'],'input_artifact_id':original['input_artifact_id'],'parameters':original['parameters'],'idempotency_key':uid('retry')}))

@app.get('/api/v1/events')
def events(after:int=0):
    result=store.events(after);return {'events':result,'last_sequence':result[-1]['sequence'] if result else after}
@app.get('/api/v1/events/stream')
async def stream(request:Request,after:int=0):
    async def generate():
        cursor=max(after,int(request.headers.get('last-event-id','0')))
        while not await request.is_disconnected():
            for e in store.events(cursor):
                cursor=e['sequence'];yield f"id: {cursor}\ndata: {json.dumps(e,ensure_ascii=False)}\n\n"
            yield ': heartbeat\n\n';await asyncio.sleep(1)
    return StreamingResponse(generate(),media_type='text/event-stream')

@app.get('/api/v1/artifacts/{aid}')
def artifact(aid:str): return store.get('artifacts',aid)
@app.get('/api/v1/artifacts/{aid}/audio')
def audio(aid:str):
    a=store.get('artifacts',aid);p=media.validate_artifact(a)
    if not a['media_type'].startswith('audio'): raise ValueError('不是音频产物。')
    return FileResponse(p,media_type='audio/wav',filename=Path(p).name)
@app.get('/api/v1/artifacts/{aid}/waveform')
def waveform(aid:str):
    a=store.get('artifacts',aid);p=media.validate_artifact(a)
    if not a['media_type'].startswith('audio'): raise ValueError('不是音频产物。')
    return json.loads(p.with_suffix('.waveform.json').read_text(encoding='utf-8'))
@app.get('/api/v1/artifacts/{aid}/data')
def data(aid:str):
    a=store.get('artifacts',aid);p=media.validate_artifact(a)
    if a['media_type']!='application/json': raise ValueError('不是结构化分析产物。')
    content=json.loads(p.read_text(encoding='utf-8'));content['timeline_offset_ticks']=a.get('timeline_offset_ticks',0)
    return JSONResponse(content)
@app.get('/api/v1/exports/{eid}/download')
def download(eid:str):
    e=next((e for p in store.all('projects') for e in p['exports'] if e['id']==eid),None)
    if not e: raise KeyError(eid)
    path=Path(e['path']).resolve()
    if not path.is_relative_to((DATA/'exports').resolve()) or media.sha256(path)!=e['sha256']: raise ValueError('导出文件缺失或已变化。')
    return FileResponse(path,filename=e['name'])

@app.get('/api/v1/diagnostics')
def diagnostics(): return {'protocol_version':1,'python':sys.version,'data_root':str(DATA),'pending_jobs':[public_job(j) for j in store.all('jobs') if j['state'] not in ('succeeded','cancelled')],'engines':capabilities()['engines']}

web=ROOT/'apps/web/dist'
if web.exists(): app.mount('/',StaticFiles(directory=str(web),html=True),name='editor')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8765);args=parser.parse_args()
    if not TOKEN: raise SystemExit('SONORA_SESSION_TOKEN is required. Use the SONORA launcher.')
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=args.port,log_level='info',access_log=False)
if __name__=='__main__': main()
