"""RVC capability/preset API and immutable model selections shared by UI/jobs."""
from __future__ import annotations
import copy,json
from pathlib import Path
from fastapi import APIRouter
from .config import ROOT
from .store import now,uid,encode
from . import media
from workers.rvc_parameters import (load_parameter_schema,parameter_defaults,parameter_catalog,
    validate_rvc_parameters,pitch_analysis_parameters,parameter_hash)

PARAMETER_KEYS=set(load_parameter_schema()['properties'])
ANALYSIS_KEYS={'pitch_algorithm','f0_min','f0_max','threshold','silence_db','hop_length','device'}
VIEW_PARAMETER_KEYS={'active_voice_part','comp_segments','comp_base_candidate_id','score_duration_ticks','switch_policy','parameter_sources','preset_id'}

def clean_audio_available():
    lock=ROOT/'engine-envs/locked-environments.json'
    if not lock.exists():return False
    return 'noisereduce' in json.loads(lock.read_text(encoding='utf-8')).get('rvc',{}).get('packages',{})

def quick_manifest(catalog,parameters):
    if not parameters.get('model_id'):return None
    try:
        model=catalog.get_model(parameters['model_id'])
        chosen=parameters.get('index_id',model.get('selected_index_id'))
        if chosen:
            entries=catalog.model_indices(model['id'])['indices']
            index=next((i for i in entries if i['id']==chosen),None)
            if index:
                model.update(index_id=chosen,index_path=index.get('path'),index_hash=index.get('sha256'),index_match=index.get('compatibility',{}))
        return model
    except (ValueError,KeyError):return None

def resolve_selection(catalog,parameters):
    if not parameters.get('model_id'):raise ValueError('请先选择声音和具体推理版本。')
    chosen=parameters.get('index_id')
    if 'index_id' in parameters and not chosen:chosen=''
    try:model=catalog.resolve_model(parameters['model_id'],chosen)
    except KeyError:raise ValueError('模型版本或索引不存在，请刷新目录或重新链接原哈希。')
    expected=parameters.get('model_hash')
    if expected and expected!=model['model_hash']:raise ValueError('所选模型哈希不等于工程绑定，不能悄悄替换版本；请重新链接原模型。')
    if parameters.get('index_hash') and chosen and parameters['index_hash']!=model.get('index_hash'):raise ValueError('索引哈希与工程绑定不一致，请重新绑定匹配索引。')
    return model

def pin_parameter_delta(catalog,current,delta):
    """Resolve explicit selection outside the project's short write transaction."""
    if not isinstance(delta,dict):raise ValueError('参数必须为对象。')
    merged={**current,**delta}
    if not merged.get('model_id'):return copy.deepcopy(delta)
    changed_model=merged.get('model_id')!=current.get('model_id')
    changed_index='index_id' in delta and delta['index_id']!=current.get('index_id')
    needs_pin=changed_model or changed_index or not current.get('model_hash') or any(key in delta and delta[key]!=current.get(key) for key in ('model_hash','index_hash'))
    if not needs_pin:return copy.deepcopy(delta)
    if changed_model:
        # Choosing a new immutable ID is an explicit version switch. Old pins from
        # a caller's merged form cannot silently turn it back into the old version.
        if merged.get('model_hash')==current.get('model_hash'):merged.pop('model_hash',None)
        if 'index_id' not in delta:merged.pop('index_id',None)
        merged.pop('index_hash',None)
    if changed_index:merged.pop('index_hash',None)
    model=resolve_selection(catalog,merged)
    return {**copy.deepcopy(delta),**selection_fields(model,merged)}

def selection_fields(model,parameters=None):
    parameters=parameters or {}
    speakers=model.get('speaker_ids_trained') or model.get('speaker_ids') or [0]
    speaker=parameters.get('speaker_id',0 if 0 in speakers else speakers[0])
    return {'model_id':model['id'],'model_artifact_id':model.get('canonical_id',model.get('model_artifact_id',model['id'])),
            'model_hash':model['model_hash'],'voice_id':model.get('voice_id'),'run_id':model.get('run_id'),
            'index_id':model.get('index_id'),'index_hash':model.get('index_hash'),'speaker_id':speaker}

def builtin_presets(model=None,task='speech'):
    matched=(model or {}).get('index_match',{}).get('status')=='verified'
    return [
        {'id':'natural-v2','name':'自然对白','version':2,'source_type':'task_preset','parameters':{'transpose':0,'index_rate':0,'protect':.33,'volume_envelope':0,'pitch_algorithm':'rmvpe','f0_autotune':False,'filter_radius':0},'explanation':'通用保守起点：不移调、不自动修音，保留输入说话动态。检索为0时保护参数不改变结果。'},
        {'id':'singing-v2','name':'保留歌唱动态','version':2,'source_type':'task_preset','parameters':{'transpose':0,'index_rate':.5 if matched else 0,'protect':.33,'volume_envelope':.5,'pitch_algorithm':'rmvpe','f0_autotune':False,'filter_radius':0},'explanation':'同片段比较动态混合0.5与1；'+('已验证索引使用中等检索比例，不代表最佳。' if matched else '当前无已验证索引，明确提供无检索起点0。')},
        {'id':'quick-v2','name':'快速短试听','version':2,'source_type':'task_preset','parameters':{'transpose':0,'index_rate':0,'protect':.33,'volume_envelope':1,'pitch_algorithm':'fcpe','threshold':None,'context_seconds':1,'f0_autotune':False},'explanation':'使用已有FCPE，在8–15秒代表片段比较；速度以当前机器实测为准。'},
        {'id':'consonants-v2','name':'清辅音单项对照','version':2,'source_type':'task_preset','requires_verified_index':True,'available':matched,'parameters':{'index_rate':.5,'protect':.15,'volume_envelope':1},'explanation':'保持已验证检索比例0.5，对照protect0.15与0.5；0.5关闭保护，数值越小保护越强。'}]

def normalized_job_parameters(parameters,model=None,scope='rvc',external=False):
    checked=validate_rvc_parameters(parameters,model,scope=scope,has_external_f0=external,clean_audio_available=clean_audio_available())
    return {**checked['context_parameters'],**checked['accepted_parameters'],'parameter_sources':checked['parameter_sources']},checked

def render_signature(source,parameters):
    accepted=validate_rvc_parameters({k:v for k,v in parameters.items() if k in PARAMETER_KEYS},scope='rvc',clean_audio_available=clean_audio_available())['accepted_parameters']
    return parameter_hash({'source_hash':source['sha256'],'model_hash':parameters.get('model_hash'),
        'index_hash':parameters.get('index_hash') if accepted.get('index_rate') else None,'parameters':accepted,'pitch_edits':parameters.get('pitch_edits',[]),
        'range_ticks':parameters.get('range_ticks'),'voice_part':parameters.get('voice_part'),'source_timeline_offset_ticks':parameters.get('source_timeline_offset_ticks',0)})

def candidate_matches(project,candidate,source,parameters,model):
    if not candidate or not source or candidate['source_artifact_id']!=source['id']:return False
    if not candidate.get('render_signature'):return False
    from .editing import note_edits
    part=candidate.get('voice_part');binding=project.get('voice_parts',{}).get(part,{}) if part else {}
    edits=binding.get('pitch_edits',project['pitch_edits']);notes=binding.get('notes',project['notes'])
    track=next((t for t in project['tracks'] if t['artifact_id']==source['id']),{})
    pins=selection_fields(model or {'id':parameters.get('model_id'),'model_hash':parameters.get('model_hash')},parameters)
    current={**parameters,**pins,'pitch_edits':note_edits(notes)+edits,'voice_part':part,'source_timeline_offset_ticks':track.get('offset_ticks',0)}
    if candidate.get('preview_only'):current['range_ticks']=[project['selection']['start'],project['selection']['end']]
    else:current.pop('range_ticks',None)
    try:return render_signature(source,current)==candidate['render_signature']
    except ValueError:return False

def make_rvc_router(store,catalog):
    router=APIRouter(prefix='/api/v1/rvc',tags=['rvc-parameters'])
    with store.connect() as db:db.execute('CREATE TABLE IF NOT EXISTS rvc_personal_presets(id TEXT PRIMARY KEY,body TEXT NOT NULL)')
    def get_presets(model_id=None,task='speech'):
        model=quick_manifest(catalog,{'model_id':model_id}) if model_id else None
        values=builtin_presets(model,task)
        with store.connect() as db:rows=[json.loads(r[0]) for r in db.execute('SELECT body FROM rvc_personal_presets ORDER BY rowid DESC')]
        values.extend(p for p in rows if not p.get('model_id') or p['model_id']==model_id)
        return {'presets':values}
    def describe(body):
        values=body.get('values',{});model_id=body.get('model_id') or values.get('model_id')
        model=quick_manifest(catalog,{**values,'model_id':model_id}) if model_id else None
        result=parameter_catalog(model,values,body.get('task','speech'),bool(body.get('has_external_f0')),['rmvpe','fcpe','crepe','crepe-tiny','pyin','harvest','dio'],clean_audio_available())
        result.update(model=model,presets=get_presets(model_id,body.get('task','speech'))['presets'])
        return result
    @router.post('/parameters')
    def parameters(body:dict):return describe(body)
    @router.get('/parameters')
    def parameters_get(model_id:str|None=None,task:str='speech'):return describe({'model_id':model_id,'task':task})
    @router.get('/presets')
    def presets(model_id:str|None=None,task:str='speech'):return get_presets(model_id,task)
    @router.post('/presets')
    def save_preset(body:dict):
        name=str(body.get('name','')).strip()
        if not name or len(name)>80:raise ValueError('预设名称需要1–80个字符。')
        supplied=body.get('parameters',{})
        if not isinstance(supplied,dict) or set(supplied)-PARAMETER_KEYS:raise ValueError('个人预设只能包含当前执行器参数。')
        model=resolve_selection(catalog,{'model_id':body['model_id']}) if body.get('model_id') else None
        checked=validate_rvc_parameters(supplied,model,clean_audio_available=clean_audio_available())
        value={'id':uid('preset'),'name':name,'version':1,'source_type':'personal_preset','parameters':{k:checked['accepted_parameters'][k] for k in supplied},'model_id':body.get('model_id'),'model_hash':(model or {}).get('model_hash'),'created_at':now(),'explanation':'用户保存的设置；不代表跨模型的最佳参数。'}
        with store.connect() as db:db.execute('INSERT INTO rvc_personal_presets VALUES(?,?)',(value['id'],encode(value)))
        return value
    return router
