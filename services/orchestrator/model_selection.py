"""Explicit project/track/range model inheritance over immutable catalog selections."""
import copy
from . import media
from .store import uid,Conflict
from .rvc_service import resolve_selection,selection_fields
from workers.rvc_parameters import parameter_defaults

RUNTIME_SELECTION_KEYS={'model_id','model_artifact_id','model_hash','voice_id','run_id','index_id','index_hash','speaker_id','preset_id'}
def runtime_selection(selection):
    return {key:copy.deepcopy(value) for key,value in selection.items() if key in RUNTIME_SELECTION_KEYS}

def resolve_binding(project,parameters,source_id=None):
    result=copy.deepcopy(parameters)
    if result.get('voice_part'):return result,source_id
    result['model_binding_scope']='project'
    bindings=project.get('model_bindings',{})
    track_id=result.get('track_id')
    if track_id:
        track=next((t for t in project['tracks'] if t['id']==track_id),None)
        if not track:raise ValueError('所选轨道不存在。')
        if source_id and source_id!=track['artifact_id']:raise ValueError('模型轨道覆盖与输入音频不一致。')
        source_id=track['artifact_id']
    elif source_id:
        track=next((t for t in project['tracks'] if t['artifact_id']==source_id),None)
        track_id=track['id'] if track else None
    if track_id in bindings.get('tracks',{}):
        selection=bindings['tracks'][track_id]
        result.update(selection.get('parameters',{}));result.update(runtime_selection(selection['selection']));result.update(track_id=track_id,model_binding_scope='track',model_binding_target=track_id)
    clips={key:value for key,value in bindings.get('clips',{}).items() if value.get('source_artifact_id')==source_id}
    chosen=result.get('clip_id')
    if chosen:
        if chosen not in clips:raise ValueError('区间模型覆盖不属于当前输入。')
        matches=[(chosen,clips[chosen])]
    else:
        region=result.get('range_ticks')
        matches=[(key,value) for key,value in clips.items() if region and region[0]>=value['range_ticks'][0] and region[1]<=value['range_ticks'][1]]
        if len(matches)>1:raise ValueError('有多个重叠区间覆盖，请明确选择区间绑定。')
        if clips and not region and not result.get('ignore_clip_bindings'):raise ValueError('此输入已有区间模型覆盖；请分区间渲染并局部采用，或明确忽略区间覆盖执行全曲版本。')
    if matches and not result.get('ignore_clip_bindings'):
        identifier,clip=matches[0];region=result.get('range_ticks',clip['range_ticks'])
        if region[0]<clip['range_ticks'][0] or region[1]>clip['range_ticks'][1]:raise ValueError('试听范围越过所选模型覆盖区间。')
        result.update(clip.get('parameters',{}));result.update(runtime_selection(clip['selection']));result.update(clip_id=identifier,range_ticks=region,model_binding_scope='clip',model_binding_target=identifier)
    return result,source_id

def clear_selection(store,pid,body):
    scope=body.get('scope');key=body.get('idempotency_key')
    if scope not in ('track','clip') or not isinstance(key,str) or not 1<=len(key)<=128:raise ValueError('请选择要清除的轨道/区间覆盖，并提供幂等键。')
    opkey=media.hash_json('clear:'+key)
    current=store.get('projects',pid)
    if opkey in current.get('model_bindings',{}).get('operations',{}):return {'project':current,'selection':None}
    def mutate(project):
        project['last_user_audio_edit']=uid('edit')
        bindings=project.setdefault('model_bindings',{})
        if scope=='track':
            identifier=body.get('track_id');bindings.setdefault('tracks',{}).pop(identifier,None)
        else:
            identifier=body.get('clip_id');bindings.setdefault('clips',{}).pop(identifier,None)
            if project['parameters'].get('clip_id')==identifier:project['parameters'].pop('clip_id',None)
        bindings.setdefault('operations',{})[opkey]={'cleared_scope':scope,'target_id':identifier}
        for node in project['graph']:
            if not node.get('voice_part') and node['type'] in ('convert','mix','export'):node.update(state='stale',reason='模型覆盖已清除，请重新试听当前继承版本')
    return {'project':store.change(pid,mutate,revision=body.get('revision')),'selection':None}

def select_model(store,catalog,pid,body):
    current=store.get('projects',pid);key=body.get('idempotency_key')
    if not isinstance(key,str) or not 1<=len(key)<=128:raise ValueError('模型选择需要幂等键。')
    opkey=media.hash_json(key)
    previous=current.get('model_bindings',{}).get('operations',{}).get(opkey)
    if previous:return {'project':current,'selection':previous}
    if current['revision']!=body.get('revision'):raise Conflict('工程已更新，请刷新后选择模型。')
    scope=body.get('scope','project');policy=body.get('policy','preserve_overrides')
    if scope not in ('project','track','clip') or policy not in ('preserve_overrides','reset_defaults'):raise ValueError('无效的模型覆盖范围或切换策略。')
    track=None;region=None
    if scope!='project':
        track=next((t for t in current['tracks'] if t['id']==body.get('track_id')),None)
        if not track:raise ValueError('请先选择已存在的输入轨道。')
        if track.get('voice_part'):raise ValueError('主唱/和声请使用已有声部模型绑定。')
    if scope=='clip':
        region=body.get('range_ticks');a=next(a for a in current['assets'] if a['id']==track['artifact_id'])
        end=track.get('offset_ticks',0)+round(a['sample_count']*48000/a['sample_rate'])
        if not isinstance(region,list) or len(region)!=2 or any(type(v) is not int for v in region) or region[0]<max(0,track.get('offset_ticks',0)) or region[1]<=region[0] or region[1]>end:raise ValueError('模型区间覆盖必须落在输入轨道范围内。')
    params={key:body[key] for key in ('model_id','index_id','speaker_id') if key in body};model=resolve_selection(catalog,params)
    pins=selection_fields(model,params);selection=catalog.record_selection(model['id'],pid,track['id'] if track else None,body.get('clip_id'),pins.get('index_id') or '',pins.get('speaker_id',0),body.get('preset_id'))
    selection.update(pins)
    defaults=parameter_defaults(model) if policy=='reset_defaults' else {}
    def mutate(project):
        project['last_user_audio_edit']=uid('edit')
        bindings=project.setdefault('model_bindings',{});bindings.setdefault('tracks',{});bindings.setdefault('clips',{});bindings.setdefault('operations',{})
        entry={'selection':selection,'parameters':defaults,'policy':policy}
        if scope=='project':
            bindings['project']=entry
            project['parameters'].update(defaults);project['parameters'].update(pins);project['parameters']['switch_policy']=policy
        elif scope=='track':
            entry['source_artifact_id']=track['artifact_id'];bindings['tracks'][track['id']]=entry
            project['parameters'].update(track_id=track['id'],source_artifact_id=track['artifact_id']);project['parameters'].pop('clip_id',None)
        else:
            identifier=body.get('clip_id') or uid('clip')
            entry.update(track_id=track['id'],source_artifact_id=track['artifact_id'],range_ticks=region)
            bindings['clips'][identifier]=entry;selection['clip_id']=identifier
            project['parameters'].update(track_id=track['id'],clip_id=identifier,source_artifact_id=track['artifact_id'])
        bindings['operations'][opkey]=selection
        for node in project['graph']:
            affected=scope=='project' or (track and track['artifact_id'] in node.get('input_refs',[]))
            if not node.get('voice_part') and (affected and node['type'] in ('convert','pipeline') or node['type'] in ('mix','export','export_stems')):
                node.update(state='stale',reason='模型版本绑定已变化；分离与原始F0保留')
        project['warnings']=[w for w in project['warnings'] if w.get('code')!='pending_model_version']+[{'code':'pending_model_version','message':'已固定新模型版本；旧候选可回听，请生成当前版本试听。'}]
    result=store.change(pid,mutate,revision=body['revision'])
    return {'project':result,'selection':selection}
