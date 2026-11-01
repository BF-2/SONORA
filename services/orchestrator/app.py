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
from .editing import freeze_locks, note_edits, apply_target_parameters, shift_pitch_to_project
from .midi import read_midi,write_midi
from .catalog_api import make_catalog_router
from .rvc_service import make_rvc_router,quick_manifest,resolve_selection,selection_fields,pin_parameter_delta,builtin_presets,PARAMETER_KEYS,ANALYSIS_KEYS,VIEW_PARAMETER_KEYS
from .workflow import describe_workflow,scoped_context
from .model_selection import select_model,clear_selection,resolve_binding
from workers.rvc_parameters import validate_rvc_parameters
from .rvc_service import clean_audio_available
from .voices import VOICE_PARTS,PART_NAMES,voice_part,new_binding,sync_part_tracks,resolve_part,remap_parts,invalidate_mix,covers_source

app=FastAPI(title='声序 SONORA',version='1.0.0',docs_url=None,redoc_url=None,openapi_url=None)
store=Store();catalog_router=make_catalog_router(store);catalog=catalog_router.catalog;scheduler=Scheduler(store,catalog)
app.include_router(catalog_router)
app.include_router(make_rvc_router(store,catalog))
registry_lock=threading.RLock()

@app.on_event('startup')
def discover_model_versions():
    if os.environ.get('SONORA_SKIP_CATALOG_SCAN')!='1':catalog.ensure_initial_scan(max_files=5000,max_seconds=30)

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
             {'id':'separation','name':'MelBand RoFormer · 人声与声部分离','status':'ready' if config.get('separation',{}).get('models') else 'unavailable','reason':'先提取混合人声，再用专用模型估计主唱与和声；重叠声部仍需试听检查串音。','models':[m for m in config.get('separation',{}).get('models',[]) if m.get('task')!='lead_backing'],'harmony_models':[{**m,'status':'ready' if Path(m['path']).is_file() and Path(m['config_path']).is_file() else 'missing'} for m in config.get('separation',{}).get('models',[]) if m.get('task')=='lead_backing'],'default_harmony_model':config.get('separation',{}).get('default_harmony_model'),'capabilities':{'stem_separation':True,'lead_backing_separation':bool(config.get('separation',{}).get('default_harmony_model')),'local_segment_render':False}},
             {'id':'pitch','name':'独立音高服务','status':'ready' if installed else 'unavailable','capabilities':{'algorithms':['rmvpe','fcpe','crepe','crepe-tiny','pyin','harvest','dio'],'external_f0':True},'test_status':'installed; see engine verification'},
             {'id':'rvc','name':'Applio / RVC','status':'ready' if installed and os.environ.get('SONORA_DISABLE_MODEL_WORKERS')!='1' else 'unavailable','reason':'真实外部 F0 条件；模型音质仍需人工试听','capabilities':{'voice_conversion':True,'singing_conversion':True,'external_f0':True,'streaming':False,'svs':False,'local_segment_render':False},'test_status':'see per-model manifest'},
             {'id':'svs','name':'DiffSinger · Nishiren Gard AI','status':'ready' if config.get('svs',{}).get('models') else 'unavailable','reason':'已接入日语假名/显式音素 + 音符的锁定窗口合成。其他语言请使用新导唱路线。','default_model':config.get('svs',{}).get('default_model'),'models':[{k:v for k,v in m.items() if k!='components'} for m in config.get('svs',{}).get('models',[])],'capabilities':{'svs':bool(config.get('svs',{}).get('models')),'languages':['ja'],'notes':True,'phoneme_timing':True,'timing_policy':['locked_window'],'max_phrase_seconds':60}},
             {'id':'realtime','name':'实时会话','status':'unavailable','reason':'尚未配置与验证流式执行器和音频设备路由。','capabilities':{'streaming':False}}]
    return {'protocol_version':1,'engines':engines,'device':{'name':'本地执行器','inference':'CPU / CUDA 按执行器实测','max_parallel_gpu_jobs':1},'verification':tested,'catalog_scan':catalog.latest_scan()}

@app.get('/api/v1/models')
def models(): return catalog.list_models(limit=250,include_aliases=True)

@app.post('/api/v1/models/import')
def import_model(body:dict):
    results=catalog.import_batch([body.get('path','')]+([body['index_path']] if body.get('index_path') else []))['results']
    if not all(r['ok'] for r in results):raise ValueError('；'.join(r.get('error','') for r in results if not r['ok']))
    identifier=results[0]['model']['id'];catalog.validate(identifier)
    if len(results)>1:return catalog.bind_index(identifier,results[1]['model']['id'])
    return catalog.resolve_model(identifier)

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

@app.post('/api/v1/projects/{pid}/pitch-target')
def preview_pitch_target(pid:str,body:dict):
    """Read-only target preview, using the exact numerical path used by RVC."""
    p=store.get('projects',pid)
    artifact=next((a for a in p['assets'] if a['id']==body.get('pitch_artifact_id') and a.get('stem_role')=='pitch'),None)
    if not artifact:raise ValueError('请选择属于当前工程的音高分析。')
    params=copy.deepcopy(body.get('parameters',{}))
    edits=body.get('pitch_edits',[]);notes=body.get('notes',[])
    validate_patch(p,{'pitch_edits':edits,'notes':notes,'parameters':{k:v for k,v in params.items() if k in PARAMETER_KEYS}})
    source=next((a for a in p['assets'] if a['id']==body.get('source_artifact_id')),None)
    if not source or source['id'] not in artifact.get('input_artifact_ids',[]):raise ValueError('曲线与当前人声来源不一致。')
    track=next((t for t in p['tracks'] if t['artifact_id']==source['id']),None)
    offset=track.get('offset_ticks',0) if track else source.get('timeline_offset_ticks',0)
    raw=json.loads(media.validate_artifact(artifact).read_text(encoding='utf-8'))
    raw.pop('target_layer',None)
    target=apply_target_parameters(shift_pitch_to_project(raw,offset),{**params,'pitch_edits':note_edits(notes)+edits})
    # frame_center_offset is already in project time. Do not add it twice.
    target['timeline_offset_ticks']=0
    return target

@app.post('/api/v1/projects/{pid}/candidates/archive-older')
def archive_old_candidates(pid:str,body:dict):
    from .candidate_archive import archive_older
    return store.change(pid,archive_older,revision=body.get('revision'))

@app.get('/api/v1/projects/{pid}/workflow')
def workflow(pid:str,voice_part:str|None=None):
    p=store.get('projects',pid);_,parameters,_=scoped_context(p,voice_part)
    source,_,binding=scoped_context(p,voice_part)
    try:effective,_=resolve_binding(p,{**parameters,**({'voice_part':voice_part} if binding else {})},source['id'] if source else None)
    except ValueError as error:effective=parameters;binding_error=str(error)
    else:binding_error=None
    view=copy.deepcopy(p)
    if not binding:view['parameters']=effective
    result=describe_workflow(view,quick_manifest(catalog,effective),voice_part,store)
    with store.connect() as db:
        running=[json.loads(row[0]) for row in db.execute("SELECT body FROM jobs WHERE project_id=? AND json_extract(body,'$.state') IN ('queued','preparing','running') ORDER BY rowid DESC",(pid,))]
    stage_for={'separate':1,'separate_harmony':1,'pitch':2,'convert':3,'compare_versions':3,'convert_parts':3,'mix':4,'export':5,'export_stems':5}
    result['active_jobs']=[]
    for job in running:
        part=job['parameters'].get('voice_part')
        if part and part!=voice_part:continue
        result['active_jobs'].append({key:job.get(key) for key in ('id','operation','state','stage','message')})
        index=stage_for.get(job['operation'])
        if index is not None:
            stage=result['stages'][index];stage['state']='running';stage['status_text']='排队中' if job['state']=='queued' else job['stage']
            stage['recommendation']['disabled']=True
    result.update(effective_parameters=effective,binding_origin=effective.get('model_binding_scope','voice_part' if binding else 'project'),binding_error=binding_error)
    return result

@app.post('/api/v1/projects/{pid}/model-selection')
def project_model_selection(pid:str,body:dict):
    check_structure(body);return select_model(store,catalog,pid,body)

@app.post('/api/v1/projects/{pid}/model-selection/clear')
def clear_project_model_selection(pid:str,body:dict):
    check_structure(body);return clear_selection(store,pid,body)

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
    allowed={'name','task','selection','playhead','mode','parameters','pitch_edits','notes','lyrics','tracks','candidates','selected_candidate_id','voice_parts','view_preferences','model_bindings','warnings'}
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
    if 'pitch_artifact_id' in params and params['pitch_artifact_id'] is not None and not isinstance(params['pitch_artifact_id'],str):raise ValueError('分析绑定必须为产物ID或空值。')
    validate_rvc_parameters({k:v for k,v in params.items() if k in PARAMETER_KEYS or k in ('range_ticks','source_timeline_offset_ticks')},clean_audio_available=clean_audio_available())
    if params.get('source_artifact_id') and params['source_artifact_id'] not in owned:raise ValueError('所选输入不属于当前工程。')
    if 'view_preferences' in patch:
        if not isinstance(patch['view_preferences'],dict):raise ValueError('视图偏好必须为对象。')
        if 'auto_archive_candidates' in patch['view_preferences'] and type(patch['view_preferences']['auto_archive_candidates']) is not bool:raise ValueError('自动归档设置必须为布尔值。')
        stage=patch['view_preferences'].get('stage',0)
        if type(stage) is not int or not 0<=stage<=5:raise ValueError('无效阶段偏好。')
    if params.get('active_voice_part','vocal') not in ('vocal',*VOICE_PARTS):raise ValueError('未知的编辑声部。')
    from workers.separation.options import separation_options
    for key,task in [('separation_settings','vocals_instrumental'),('harmony_settings','lead_backing')]:
        if key in params:separation_options({key:params[key],'separation_task':task})
    for key,lo,hi in [('transpose',-36,36),('index_rate',0,1),('protect',0,.5),('volume_envelope',0,1),('segment_size',32,2048),('overlap',1,16)]:
        if key in params: media.bounded(params[key],lo,hi)
    if 'candidates' in patch:
        old={c['id']:c for c in p['candidates']}
        for c in patch['candidates']:
            original=old.get(c.get('id'))
            if not original: raise ValueError('候选只能由成功推理创建。')
            for key in ('source_artifact_id','artifact_id','model_id','generation_depth','voice_part','part_source_artifact_id'):
                if c.get(key)!=original.get(key): raise ValueError('候选推理来源不可编辑。')
    if 'voice_parts' in patch:
        parts=patch['voice_parts']
        if not isinstance(parts,dict) or set(parts)-set(VOICE_PARTS):raise ValueError('声部绑定仅支持主唱与和声。')
        if set(p.get('voice_parts',{}))-set(parts):raise ValueError('不能删除已有声部绑定；请使用静音模式或撤销分离。')
        for part,binding in parts.items():
            if not isinstance(binding,dict) or set(binding)-{'source_artifact_id','mode','parameters','pitch_edits','notes','selected_candidate_id'}:raise ValueError('声部配置字段无效。')
            source=next((a for a in p['assets'] if a['id']==binding.get('source_artifact_id') and a.get('stem_role')==part),None)
            if source is None:raise ValueError('声部源不属于当前工程，或角色不符。')
            if binding.get('mode') not in ('convert','original','mute'):raise ValueError('声部模式必须为转换、保留原声或静音。')
            validate_patch(p,{key:binding.get(key,{} if key=='parameters' else []) for key in ('parameters','pitch_edits','notes')})
            selected=binding.get('selected_candidate_id')
            if selected:
                c=next((c for c in p['candidates'] if c['id']==selected and c.get('voice_part')==part),None)
                origin=next((a for a in p['assets'] if c and a['id']==c.get('part_source_artifact_id',c['source_artifact_id'])),None)
                if not c or not origin or origin['sha256']!=source['sha256']:raise ValueError('候选不属于当前声部源，不能跨声部或跨源采用。')
                if c.get('archived'):raise ValueError('此候选已移除，请先恢复片段。')
                output=next(a for a in p['assets'] if a['id']==c['artifact_id'])
                source_track=next((t for t in p['tracks'] if t['artifact_id']==source['id']),{})
                if not covers_source(output,source,source_track.get('offset_ticks',0)):raise ValueError('短试听不能替换整条声部；请完整渲染后采用。')

@app.patch('/api/v1/projects/{pid}')
def patch_project(pid:str,body:dict):
    if not isinstance(body.get('revision'),int): raise ValueError('保存需要工程版本号。')
    patch=copy.deepcopy(body.get('patch',{}));current=store.get('projects',pid)
    if 'model_bindings' in patch:raise ValueError('请通过模型选择接口或参数增量更新覆盖，不能直接替换版本绑定。')
    if current['revision']!=body['revision']:raise Conflict('工程已更新，请刷新后重试。')
    if 'parameters' in patch:patch['parameters']=pin_parameter_delta(catalog,current['parameters'],patch['parameters'])
    for part,delta in patch.get('voice_parts',{}).items():
        if isinstance(delta,dict) and 'parameters' in delta:
            delta['parameters']=pin_parameter_delta(catalog,current.get('voice_parts',{}).get(part,{}).get('parameters',{}),delta['parameters'])
    def mutate(p):
        if 'candidates' in patch:
            if not isinstance(patch['candidates'],list):raise ValueError('候选更新必须为列表。')
            current_candidates={c['id']:c for c in p['candidates']};seen=set()
            for incoming in patch['candidates']:
                identifier=incoming.get('id')
                if identifier not in current_candidates or identifier in seen:raise ValueError('候选只能由成功推理创建，更新不能重复标识。')
                seen.add(identifier);current_candidate=current_candidates[identifier]
                for key in ('source_artifact_id','artifact_id','model_id','generation_depth','voice_part','part_source_artifact_id'):
                    if key in incoming and incoming[key]!=current_candidate.get(key):raise ValueError('候选推理来源不可编辑。')
                if 'archived' in incoming:
                    if type(incoming['archived']) is not bool:raise ValueError('片段移除状态必须为布尔值。')
                    if incoming['archived']:
                        selected_ids={p.get('selected_candidate_id')}|{b.get('selected_candidate_id') for b in p.get('voice_parts',{}).values()}
                        if identifier in selected_ids:raise ValueError('该候选正在被采用；请先采用另一候选，再移除。')
                        if any(not t.get('mute') and t['artifact_id']==current_candidate['artifact_id'] for t in p['tracks']):raise ValueError('该候选仍用于混音；请先静音对应轨道，再移除。')
                current_candidates[identifier]={**current_candidate,**{key:incoming[key] for key in ('name','notes','favorite','archived') if key in incoming}}
                if 'archived' in incoming and incoming['archived']!=bool(current_candidate.get('archived')):
                    current_candidates[identifier].update(archive_reason='manual' if incoming['archived'] else None,
                        archived_at=now() if incoming['archived'] else None, archive_retained=not incoming['archived'])
            patch['candidates']=[current_candidates[c['id']] for c in p['candidates']]
        scoped_parameter_changes=[]
        if 'model_parameter_updates' in patch:
            updates=patch.pop('model_parameter_updates')
            if not isinstance(updates,dict) or set(updates)-{'tracks','clips'}:raise ValueError('模型参数覆盖范围无效。')
            bindings=copy.deepcopy(p.get('model_bindings',{}))
            for scope,entries in updates.items():
                if not isinstance(entries,dict):raise ValueError('模型参数覆盖必须为对象。')
                for identifier,delta in entries.items():
                    if identifier not in bindings.get(scope,{}) or not isinstance(delta,dict):raise ValueError('模型覆盖不存在。')
                    if set(delta)-PARAMETER_KEYS-{'parameter_sources','preset_id','switch_policy','accepted_preview_id','pitch_artifact_id'}:raise ValueError('通过版本选择器改变模型/索引绑定，参数覆盖不能改变身份。')
                    entry=bindings[scope][identifier];old=entry.get('parameters',{})
                    merged={**old,**delta}
                    if 'parameter_sources' in delta:merged['parameter_sources']={**old.get('parameter_sources',{}),**delta['parameter_sources']}
                    validate_patch(p,{'parameters':merged});entry['parameters']=merged
                    scoped_parameter_changes.append((entry.get('source_artifact_id'),identifier,set(delta)))
            patch['model_bindings']=bindings
        if 'track_updates' in patch:
            updates=patch.pop('track_updates')
            if not isinstance(updates,dict) or set(updates)-{t['id'] for t in p['tracks']}:raise ValueError('轨道更新引用未知轨道。')
            allowed_track={'gain_db','pan','mute','solo','offset_ticks','effects','name'}
            for fields in updates.values():
                if not isinstance(fields,dict) or set(fields)-allowed_track:raise ValueError('轨道增量不能改变来源或声部身份。')
            patch['tracks']=[{**t,**updates.get(t['id'],{})} for t in p['tracks']]
        if 'voice_parts' in patch:
            if not isinstance(patch['voice_parts'],dict):raise ValueError('声部配置必须为对象。')
            merged=copy.deepcopy(p.get('voice_parts',{}))
            for part,delta in patch['voice_parts'].items():
                if part not in VOICE_PARTS or not isinstance(delta,dict):raise ValueError('未知或无效声部配置。')
                prior=merged.get(part,{})
                merged[part]={**prior,**delta}
                if 'parameters' in delta:
                    if not isinstance(delta['parameters'],dict):raise ValueError('声部参数必须为对象。')
                    merged[part]['parameters']={**prior.get('parameters',{}),**delta['parameters']}
            patch['voice_parts']=merged
        validate_patch(p,patch)
        if 'pitch_edits' in patch:freeze_locks(p,patch,store)
        changed_parts=[];routing_parts=[]
        for part,binding in patch.get('voice_parts',{}).items():
            previous=p.get('voice_parts',{}).get(part,{})
            if any(binding.get(k)!=previous.get(k) for k in ('source_artifact_id','parameters','pitch_edits','notes')):changed_parts.append(part)
            if any(binding.get(k)!=previous.get(k) for k in ('source_artifact_id','mode','selected_candidate_id')):routing_parts.append(part)
            view={**p,'pitch_edits':previous.get('pitch_edits',[]),'notes':previous.get('notes',[]),'parameters':binding.get('parameters',{})}
            freeze_locks(view,binding,store,binding['source_artifact_id'])
        changed=set(patch)&{'parameters','pitch_edits','notes','lyrics','tracks'}
        # Switching editor focus is not an audio parameter change.
        parameter_changes={key for key in set(p['parameters'])|set(patch.get('parameters',p['parameters'])) if p['parameters'].get(key)!=patch.get('parameters',p['parameters']).get(key)}
        if changed=={'parameters'} and not (parameter_changes-VIEW_PARAMETER_KEYS-{'accepted_preview_id'}):changed=set()
        p.update(copy.deepcopy(patch))
        if changed or changed_parts or routing_parts or scoped_parameter_changes:p['last_user_audio_edit']=uid('edit')
        for source_id,identifier,keys in scoped_parameter_changes:
            for node in p['graph']:
                if node.get('voice_part'):continue
                affected=source_id in node.get('input_refs',[]) or node.get('parameters',{}).get('model_binding_target')==identifier
                if affected and (node['type'] in ('convert','pipeline') or node['type']=='pitch' and keys&ANALYSIS_KEYS) or node['type'] in ('mix','export','export_stems'):
                    node.update(state='stale',reason='轨道/区间参数变更，旧结果保留供比较')
            p['warnings']=[w for w in p['warnings'] if w.get('code')!='pending_model_parameters']+[{'code':'pending_model_parameters','message':'当前覆盖参数已更新，请重新生成同区间试听。'}]
        if changed_parts:
            for node in p['graph']:
                if node.get('voice_part') in changed_parts or node['type'] in ('mix','export','export_stems'):
                    node.update(state='stale',invalidation_scope='whole_voice_part_context_unknown',reason='该声部参数变更，新版本待试听')
            p['warnings']=[w for w in p['warnings'] if not(w.get('code')=='pending_render' and w.get('voice_part') in changed_parts)]
            p['warnings'] += [{'code':'pending_render','voice_part':part,'message':PART_NAMES[part]+'新版本待试听；另一声部保持当前版本。'} for part in changed_parts]
        if routing_parts:
            sync_part_tracks(p,routing_parts)
            invalidate_mix(p,'主唱/和声的采用候选或模式已变化')
        if changed:
            affected='mix_export' if changed=={'tracks'} else 'conversion_mix_export'
            for node in p['graph']:
                if affected=='conversion_mix_export' and node['type'] in ('convert','pipeline','mix','export') or affected=='mix_export' and node['type'] in ('mix','export'):
                    node['state']='stale';node['invalidation_scope']='whole_node_context_unknown';node['reason']='编辑变更，保留旧版本试听，新版本待渲染'
            p['warnings']=[w for w in p['warnings'] if w.get('code')!='pending_render']+[{'code':'pending_render','message':'新版本待试听：旧产物仍可播放；重新运行受影响节点。','scope':affected}]
            if parameter_changes&ANALYSIS_KEYS:
                for node in p['graph']:
                    if node['type']=='pitch' and not node.get('voice_part'):node.update(state='stale',reason='音高分析参数已改变，请重新检测')
        if 'selected_candidate_id' in patch and patch['selected_candidate_id']:
            if p.get('voice_parts'):raise ValueError('主唱/和声模式请在对应声部采用候选，不能将完整混合人声重新叠入。')
            selected=next((c for c in p['candidates'] if c['id']==patch['selected_candidate_id']),None)
            if not selected: raise ValueError('候选不存在。')
            if selected.get('archived'):raise ValueError('此候选已移除，请先恢复片段。')
            source=next(a for a in p['assets'] if a['id']==selected['source_artifact_id']);output=next(a for a in p['assets'] if a['id']==selected['artifact_id'])
            source_track=next((t for t in p['tracks'] if t['artifact_id']==source['id']),{})
            if not covers_source(output,source,source_track.get('offset_ticks',0)):raise ValueError('短试听不能替换整条人声；可采用试听设置、局部采用到完整候选，或生成完整版本。')
            matching=[t for t in p['tracks'] if t['artifact_id']==selected['artifact_id']]
            preferred=[t for t in matching if t.get('candidate_id')==selected['id']]
            selected_track=(preferred or matching)[-1]['id'] if matching else None
            for c in p['candidates']: c['selected']=c['id']==selected['id']
            for t in p['tracks']:
                if t['role'] in ('converted','vocal','guide','original','mix'): t['mute']=t['id']!=selected_track
            invalidate_mix(p,'已采用不同的人声候选')
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
    if p.get('voice_parts'):raise ValueError('双声部模式请分别调整声部参数；旧完整人声参数实验暂不用于双声部混音。')
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
        p['last_user_audio_edit']=uid('edit')
        role=a['stem_role'];duration=round(a['sample_count']*TIMEBASE/a['sample_rate'])
        p['parameters'].setdefault('task_confirmed',True);p['parameters'].setdefault('import_role',role)
        p['tracks'].append({'id':uid('track'),'name':a['name'],'role':role,'voice_part':voice_part(a),'artifact_id':a['id'],'gain_db':0,'pan':0,'mute':False,'solo':False,'offset_ticks':a.get('timeline_offset_ticks',0),'effects':[]})
        if role in VOICE_PARTS:
            bindings=p.setdefault('voice_parts',{});bindings[role]=new_binding(a,bindings.get(role));sync_part_tracks(p,[role])
        elif p.get('voice_parts') and role in ('original','vocal','guide'):p['tracks'][-1]['mute']=True
        if not p['selection']['end']: p['selection']={'start':0,'end':duration}
        if role=='original' and len(p['assets'])==1 and p['name']=='未命名工程': p['name']=Path(a['name']).stem
        invalidate_mix(p,'导入素材改变了混音输入，请更新混音')
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
    if role not in ('original','vocal','instrumental','guide',*VOICE_PARTS): raise ValueError('不支持的素材角色。')
    path=await upload(file)
    try:
        a=await asyncio.to_thread(media.import_audio,store,path,role);a['name']=Path(file.filename or '音频').name;store.put('artifacts',a)
        return attach_import(pid,a)
    finally: path.unlink(missing_ok=True)

@app.post('/api/v1/projects/{pid}/import-path')
def import_path(pid:str,body:dict):
    store.get('projects',pid);role=body.get('role','original')
    if role not in ('original','vocal','instrumental','guide',*VOICE_PARTS): raise ValueError('不支持的素材角色。')
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
        for part,binding in source.get('voice_parts',{}).items():
            if part not in p.get('voice_parts',{}):p.setdefault('voice_parts',{})[part]={**binding,'source_artifact_id':mapping.get(binding['source_artifact_id'],binding['source_artifact_id'])}
    for a in p['assets']:
        for key in ('path','original_uri','original_snapshot'): a.pop(key,None)
    for e in p['exports']: e.pop('path',None)
    used={c['model_id'] for c in p['candidates']}|{b.get('parameters',{}).get('model_id') for b in p.get('voice_parts',{}).values()}|{p['parameters'].get('model_id')}
    manifests=[]
    for identifier in used:
        if not identifier:continue
        try:m=catalog.get_model(identifier)
        except KeyError:continue
        manifests.append({k:v for k,v in m.items() if 'path' not in k})
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
    try:model=catalog.get_model(candidate.get('model_id'))
    except KeyError:model=None
    if not model or model.get('model_hash')!=output.get('model_hash'):
        model=next((m for m in catalog.list_models(q=output.get('model_hash') or 'unmatchable-hash',limit=250,include_aliases=True)['models'] if m.get('model_hash')==output.get('model_hash')),None)
    model_hash=output.get('model_hash')
    if output.get('assembly'):
        assembly=output['assembly'];model_hash=assembly.get('preview_model_hash')
        try:model=catalog.get_model(assembly['preview_model_id'])
        except KeyError:model=None
        if model and model.get('model_hash')!=model_hash:model=None
    restored={**candidate,'artifact_id':output['id'],'source_artifact_id':expected_source,'generation_depth':max(1,depth),'model_id':model['id'] if model else 'unresolved','model_manifest_hash':model_hash,'state':'succeeded' if depth else 'unverified_restored_media'}
    if candidate.get('source_preview_artifact_id') in mapping:restored['source_preview_artifact_id']=mapping[candidate['source_preview_artifact_id']]
    if output.get('assembly'):restored['assembly']=copy.deepcopy(output['assembly'])
    part=voice_part(output)
    restored.pop('voice_part',None);restored.pop('part_source_artifact_id',None)
    if part:
        bound=mapping.get(candidate.get('part_source_artifact_id',candidate['source_artifact_id']),expected_source)
        if bound not in ancestors or store.get('artifacts',bound).get('stem_role')!=part:raise ValueError('声部候选的原始绑定不符合实际血缘。')
        restored.update(voice_part=part,part_source_artifact_id=bound)
    return restored

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
    available=store.all('artifacts');by_id={a['id']:a for a in available};mapping={};missing=[]
    assets=[]
    for a in source.get('assets',[]):
        found=by_id.get(a.get('id'))
        if not found or found['sha256']!=a.get('sha256') or found.get('stem_role')!=a.get('stem_role'):
            found=next((known for known in available if known['sha256']==a.get('sha256') and known.get('stem_role')==a.get('stem_role')),None)
        if found:
            try: media.validate_artifact(found)
            except (ValueError,OSError): found=None
        if found: assets.append(found);mapping[a['id']]=found['id']
        else: missing.append({'hash':a.get('sha256'),'original_hash':a.get('original_hash'),'name':a.get('name'),'old_id':a.get('id')})
    def mutate(project):
        for key in ('selection','playhead','mode','parameters','pitch_edits','notes','lyrics','view_preferences','model_bindings','comparisons'):
            if key in source: project[key]=source[key]
        for scope in ('tracks','clips'):
            for entry in project.get('model_bindings',{}).get(scope,{}).values():
                if entry.get('source_artifact_id') in mapping:entry['source_artifact_id']=mapping[entry['source_artifact_id']]
        for comparison in project.get('comparisons',[]):
            for key in ('source_artifact_id','pitch_artifact_id'):
                if comparison.get(key) in mapping:comparison[key]=mapping[comparison[key]]
        project['assets']=assets
        project['tracks']=[{**t,'artifact_id':mapping[t['artifact_id']]} for t in source.get('tracks',[]) if t.get('artifact_id') in mapping]
        project['candidates']=[]
        for c in source.get('candidates',[]):
            if c.get('artifact_id') in mapping and c.get('source_artifact_id') in mapping:
                project['candidates'].append(restored_candidate(c,mapping))
        project['selected_candidate_id']=source.get('selected_candidate_id') if any(c['id']==source.get('selected_candidate_id') for c in project['candidates']) else None
        project['voice_parts']=remap_parts(source.get('voice_parts',{}),mapping,{c['id'] for c in project['candidates']})
        project['graph']=[{**node,'artifact_refs':[mapping.get(ref,ref) for ref in node.get('artifact_refs',[])],'input_refs':[mapping.get(ref,ref) for ref in node.get('input_refs',[])]} for node in source.get('graph',[]) if isinstance(node,dict)]
        project['missing_media']=missing
        project['restore_pending']={'source':source,'mapping':mapping}
        model_ids={m['id'] for m in catalog.list_models(limit=250,include_aliases=True)['models']}
        project['warnings']=[copy.deepcopy(w) for w in source.get('warnings',[]) if isinstance(w,dict) and w.get('code') not in ('missing_media','missing_model')]
        project['warnings'] += [{'code':'missing_media','message':f"缺少素材 {m['name']}，请按哈希重新链接。",**m} for m in missing]
        fallback_parts=[part for part,b in project['voice_parts'].items() if b.get('mode')=='convert' and not b.get('selected_candidate_id') and source.get('voice_parts',{}).get(part,{}).get('selected_candidate_id')]
        if fallback_parts:
            sync_part_tracks(project,fallback_parts)
            project['warnings'] += [{'code':'missing_part_candidate','voice_part':part,'message':PART_NAMES[part]+'转换候选缺失，已保留原声供试听；请重新转换。'} for part in fallback_parts]
        if project['parameters'].get('model_id') and project['parameters']['model_id'] not in model_ids: project['warnings'].append({'code':'missing_model','message':'配方模型缺失，请导入匹配哈希的模型；不会自动替换。'})
        for part,binding in project['voice_parts'].items():
            if binding.get('parameters',{}).get('model_id') not in model_ids:project['warnings'].append({'code':'missing_model','voice_part':part,'message':PART_NAMES[part]+'模型需要重新选择或导入匹配模型。'})
        validate_patch(project,{k:project[k] for k in ('selection','playhead','mode','parameters','pitch_edits','notes','lyrics','tracks','voice_parts')})
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
        for node in project['graph']:
            node['artifact_refs']=[mapping.get(ref,ref) for ref in node.get('artifact_refs',[])]
            node['input_refs']=[mapping.get(ref,ref) for ref in node.get('input_refs',[])]
        project['warnings']=[w for w in project['warnings'] if w.get('old_id')!=missing['old_id']]
        wanted=source.get('selected_candidate_id')
        if any(c['id']==wanted for c in project['candidates']):project['selected_candidate_id']=wanted
        project['voice_parts']=remap_parts(source.get('voice_parts',{}),mapping,{c['id'] for c in project['candidates']})
        fallback_parts=[part for part,b in project['voice_parts'].items() if b.get('mode')=='convert' and not b.get('selected_candidate_id') and source.get('voice_parts',{}).get(part,{}).get('selected_candidate_id')]
        if fallback_parts:sync_part_tracks(project,fallback_parts)
        validate_patch(project,{'voice_parts':project['voice_parts']})
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
def retry_job(jid:str,body:dict|None=None):
    original=store.get('jobs',jid)
    if original['state'] not in ('failed','cancelled','blocked'): raise ValueError('此任务不需要重试。')
    body=body or {}
    if set(body)-{'reset_pitch_binding'} or type(body.get('reset_pitch_binding',False)) is not bool:raise ValueError('未知的重试恢复选项。')
    parameters=copy.deepcopy(original['parameters'])
    if body.get('reset_pitch_binding'):
        if original['operation'] not in ('convert','compare_versions'):raise ValueError('该任务不支持音高绑定恢复。')
        parameters['pitch_artifact_id']=None
    p=store.get('projects',original['project_id'])
    return public_job(scheduler.submit(p['id'],{'revision':p['revision'],'operation':original['operation'],'input_artifact_id':original['input_artifact_id'],'parameters':parameters,'idempotency_key':uid('retry')}))

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

@app.post('/api/v1/artifacts/{aid}/measure')
def measure_audio(aid:str,body:dict|None=None):
    artifact=store.get('artifacts',aid);body=body or {};region=body.get('range_ticks')
    if set(body)-{'range_ticks'}:raise ValueError('未知测量选项。')
    media.validate_artifact(artifact)
    key=media.hash_json({'measurement_version':1,'sha256':artifact['sha256'],'offset':artifact.get('timeline_offset_ticks',0),'range':region})
    result=store.cache_get(key)
    if result is None:result=media.measure_artifact(artifact,region);store.cache_put(key,result)
    return {**result,'artifact_id':aid}
@app.get('/api/v1/exports/{eid}/download')
def download(eid:str):
    e=next((e for p in store.all('projects') for e in p['exports'] if e['id']==eid),None)
    if not e: raise KeyError(eid)
    path=Path(e['path']).resolve()
    if not path.is_relative_to((DATA/'exports').resolve()) or media.sha256(path)!=e['sha256']: raise ValueError('导出文件缺失或已变化。')
    return FileResponse(path,filename=e['name'])

@app.get('/api/v1/diagnostics')
def diagnostics(): return {'protocol_version':1,'python':sys.version,'data_root':str(DATA),'pending_jobs':[public_job(j) for j in store.all('jobs') if j['state'] not in ('succeeded','cancelled')],'engines':capabilities()['engines']}

@app.post('/api/v1/dev/shutdown')
def shutdown_development_session():
    if os.environ.get('SONORA_DEV_SESSION')!='1':raise HTTPException(404,'Not found')
    if any(j['state'] in ('queued','preparing','running') for j in store.all('jobs')):raise HTTPException(409,'仍有作业运行，不能关闭开发会话。')
    server=getattr(app.state,'server',None)
    if server is None:raise HTTPException(409,'会话不由本服务启动器管理。')
    threading.Timer(.3,lambda:setattr(server,'should_exit',True)).start()
    return {'status':'shutting_down'}

web=ROOT/'apps/web/dist'
if web.exists(): app.mount('/',StaticFiles(directory=str(web),html=True),name='editor')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8765);args=parser.parse_args()
    if not TOKEN: raise SystemExit('SONORA_SESSION_TOKEN is required. Use the SONORA launcher.')
    import uvicorn
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=args.port,log_level='info',access_log=False))
    app.state.server=server;server.run()
if __name__=='__main__': main()
