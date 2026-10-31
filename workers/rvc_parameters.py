"""Machine-readable RVC parameter contracts; validation performs no model loading.

Only this module decides defaults, supported ranges and inactive parameters.
The registry supplies verified model/index/speaker metadata. Runtime workers
still verify actual file hashes and tensor/index dimensions before inference.
"""
from __future__ import annotations
import copy
import json
import math
from functools import lru_cache
from pathlib import Path
import re

SCHEMA_PATH=Path(__file__).resolve().parents[1]/'packages/contracts/rvc-parameters.schema.json'
PARAMETER_SCHEMA_VERSION='1.1.0'
F0_ORDER=['raw_detection','voiced_run_median','chromatic_autotune','global_transpose','manual_edits','locked_absolute_snapshots']
THRESHOLD_DEFAULTS={'rmvpe':.03,'fcpe':.006,'crepe':.1,'crepe-tiny':.1,'pyin':0.0,'harvest':None,'dio':None}
F0_ANALYSIS_KEYS={'pitch_algorithm','f0_min','f0_max','threshold','silence_db','device','hop_length'}
F0_TARGET_KEYS={'transpose','filter_radius','f0_autotune','f0_autotune_strength'}
CONTEXT_FIELDS=set('model_id model_artifact_id model_hash index_id index_hash preset_id parameter_sources model_selection source_timeline_offset_ticks pitch_artifact_id pitch_edits range_ticks voice_part active_voice_part chain_candidate_id comparison_id algorithm skip_separation separation_model_id harmony_model_id separation_task parts mix_after comp_segments comp_base_candidate_id score_duration_ticks name format artifact_id selection_only model_converted tracks sample_rate allow_stale wav_subtype allow_clipping svs_model_id language duration_ticks notes lyrics timing_policy steps speaker'.split())
CONTEXT_FIELDS.update('source_artifact_id voice_id run_id training_run_id voice_profile_id switch_policy model_ids comparison_parameters compare_index_id comparison_name adopt_result auto_adopt model_binding_scope model_binding_target clip_id clip_range_ticks view_preferences'.split())
CONTEXT_FIELDS.update('track_id ignore_clip_bindings accepted_preview_id'.split())
ALIASES={'pitch':'transpose','f0_method':'pitch_algorithm','algorithm':'pitch_algorithm'}


class ParameterValidationError(ValueError):
    def __init__(self,parameter,message,code='invalid_parameter'):
        self.parameter=parameter;self.code=code
        super().__init__(f'{parameter}: {message}')


@lru_cache(maxsize=1)
def load_parameter_schema():
    return json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))


def parameter_defaults(model=None):
    defaults={key:copy.deepcopy(spec['default']) for key,spec in load_parameter_schema()['properties'].items()}
    ids=_trained_speakers(model or {})
    if ids:defaults['speaker_id']=0 if 0 in ids else ids[0]
    if (model or {}).get('embedder_verified'):defaults['embedder_model']=model['embedder_model']
    return defaults


def _validate_primitive(key,value,definition):
    types=definition['type'];types=types if isinstance(types,list) else [types]
    matches=(value is None and 'null' in types) or (isinstance(value,bool) and 'boolean' in types) or (isinstance(value,str) and 'string' in types)
    if not isinstance(value,bool) and isinstance(value,(int,float)):
        matches=matches or ('number' in types) or ('integer' in types and isinstance(value,int))
        if not math.isfinite(value):raise ParameterValidationError(key,'必须是有限数值')
    if not matches:raise ParameterValidationError(key,f'类型必须为 {" / ".join(types)}')
    if value is None:return
    if 'enum' in definition and value not in definition['enum']:
        raise ParameterValidationError(key,f'当前执行器只支持 {definition["enum"]}','unsupported_parameter_value')
    if isinstance(value,(int,float)) and not isinstance(value,bool):
        if 'minimum' in definition and value<definition['minimum']:raise ParameterValidationError(key,f'不能小于 {definition["minimum"]}')
        if 'maximum' in definition and value>definition['maximum']:raise ParameterValidationError(key,f'不能大于 {definition["maximum"]}')


def _trained_speakers(model):
    status=model.get('speaker_metadata_status')
    ids=model.get('speaker_ids_trained')
    if ids is None and status in ('verified_training_metadata','checkpoint'):ids=model.get('speaker_ids')
    if status not in ('verified_training_metadata','checkpoint') or not isinstance(ids,list) or not ids:
        return []
    if any(isinstance(i,bool) or not isinstance(i,int) or i<0 for i in ids):return []
    return sorted(set(ids))


def validate_rvc_parameters(parameters,model=None,*,has_external_f0=False,scope='rvc',clean_audio_available=False,available_algorithms=None):
    """Return accepted/effective/inactive snapshots; reject unknown or unsafe values.

The function is deterministic for its arguments and static schema, does not
inspect files, import neural runtimes, or mutate caller dictionaries.
"""
    if scope not in ('rvc','pitch'):raise ValueError('scope must be rvc or pitch')
    if not isinstance(parameters,dict):raise ParameterValidationError('parameters','必须为对象')
    props=load_parameter_schema()['properties'];supplied=copy.deepcopy(parameters)
    unknown=set(supplied)-set(props)-CONTEXT_FIELDS-set(ALIASES)
    if unknown:raise ParameterValidationError(sorted(unknown)[0],'当前执行器没有此参数；不能静默忽略','unsupported_parameter')
    alias_sources={}
    for alias,canonical in ALIASES.items():
        if alias not in supplied:continue
        if canonical in supplied and supplied[canonical]!=supplied[alias]:
            raise ParameterValidationError(canonical,f'与别名 {alias} 冲突')
        supplied[canonical]=supplied[alias];alias_sources[canonical]=alias
    accepted={key:copy.deepcopy(value['default']) for key,value in props.items()}
    sources={key:'executor_default' for key in accepted}
    if model and scope=='rvc':
        ids=_trained_speakers(model)
        if ids:
            accepted['speaker_id']=0 if 0 in ids else ids[0];sources['speaker_id']='model_metadata'
        if model.get('embedder_verified') and model.get('embedder_model'):
            accepted['embedder_model']=model['embedder_model'];sources['embedder_model']='model_metadata'
    for key in props:
        if key in supplied:
            accepted[key]=supplied[key]
            declared=supplied.get('parameter_sources',{}).get(key) if isinstance(supplied.get('parameter_sources'),dict) else None
            sources[key]=declared or 'user_override'
        if not props[key]['x-sonora']['supported'] and accepted[key]!=props[key]['default']:
            raise ParameterValidationError(key,props[key]['x-sonora']['support_reason'],'unsupported_parameter')
        _validate_primitive(key,accepted[key],props[key])
    if accepted['pitch_algorithm']=='auto':accepted['pitch_algorithm']='rmvpe'
    algorithm=accepted['pitch_algorithm']
    if available_algorithms is not None and algorithm not in available_algorithms:
        raise ParameterValidationError('pitch_algorithm',f'{algorithm} 未在当前PitchService环境安装','missing_dependency')
    if accepted['f0_min']>=accepted['f0_max']:raise ParameterValidationError('f0_min','必须小于 f0_max')
    if accepted['threshold'] is None:accepted['threshold']=THRESHOLD_DEFAULTS[algorithm]
    elif THRESHOLD_DEFAULTS[algorithm] is None:
        raise ParameterValidationError('threshold',f'{algorithm} 没有可比较的原生阈值，请使用 null','unsupported_parameter')
    if 'range_ticks' in supplied:
        region=supplied['range_ticks']
        if not isinstance(region,(list,tuple)) or len(region)!=2 or any(type(v) is not int for v in region) or region[0]<0 or region[1]<=region[0]:
            raise ParameterValidationError('range_ticks','必须为递增的非负整数 [start,end]')
    if 'source_timeline_offset_ticks' in supplied and type(supplied['source_timeline_offset_ticks']) is not int:
        raise ParameterValidationError('source_timeline_offset_ticks','必须为整数ticks，可为负值')
    inactive={};warnings=[]
    def disable(key,reason,code='inactive_parameter'):
        inactive[key]={'value':copy.deepcopy(accepted[key]),'reason':reason,'code':code}
        if key in supplied:warnings.append(f'{key} 未生效：{reason}')
    for key,definition in props.items():
        metadata=definition['x-sonora']
        if not metadata['supported']:
            if accepted[key]!=definition['default']:
                raise ParameterValidationError(key,metadata['support_reason'],'unsupported_parameter')
            disable(key,metadata['support_reason'],'unsupported_parameter')
    if accepted['index_rate']==0:disable('protect','index_rate=0，检索前后内容特征相同，保护参数不会改变音频','no_op_condition')
    if not accepted['f0_autotune']:disable('f0_autotune_strength','自动靠近半音未启用')
    if not supplied.get('range_ticks'):disable('context_seconds','本次为全源渲染，不使用短预览上下文')
    if THRESHOLD_DEFAULTS[algorithm] is None:disable('threshold',f'{algorithm} 无同类原生分数阈值')
    if not accepted['clean_audio']:disable('clean_strength','转换后降噪未启用')
    if accepted['clean_audio'] and scope=='rvc' and not clean_audio_available:
        raise ParameterValidationError('clean_audio','当前执行环境未验证 noisereduce，不能启用','missing_dependency')
    if scope=='pitch':
        active=F0_ANALYSIS_KEYS|F0_TARGET_KEYS
        for key in accepted:
            if key not in active and key not in inactive:disable(key,'此参数属于RVC/后处理阶段，不由PitchService执行','other_stage')
    elif model is not None:
        ids=_trained_speakers(model)
        if not ids:raise ParameterValidationError('speaker_id','训练说话人元数据未确认；请先重新验证模型，不使用embedding容量猜测','unverified_model')
        if accepted['speaker_id'] not in ids:raise ParameterValidationError('speaker_id',f'未训练的ID；已验证可用ID为 {ids}','incompatible_model')
        capacity=model.get('embedding_speaker_capacity')
        if isinstance(capacity,int) and accepted['speaker_id']>=capacity:
            raise ParameterValidationError('speaker_id','训练元数据ID超出实际embedding容量，请重新验证模型','incompatible_model')
        if not model.get('embedder_verified') or not model.get('embedder_model'):
            raise ParameterValidationError('embedder_model','训练内容编码器未验证；请重新探测模型来源','unverified_model')
        if model['embedder_model']!=accepted['embedder_model']:
            raise ParameterValidationError('embedder_model','所选编码器与已验证训练元数据不一致','incompatible_model')
        if model.get('f0_conditioned') is False:
            raise ParameterValidationError('model_id','当前外部F0适配器要求F0条件模型','incompatible_model')
        if accepted['index_rate']>0:
            match=model.get('index_match') or {}
            if match.get('status')!='verified' or not match.get('evidence'):
                raise ParameterValidationError('index_rate','索引未验证匹配当前实验与编码器；先绑定匹配索引或明确选0','unverified_index')
            if not model.get('index_path') or not re.fullmatch('[0-9a-fA-F]{64}',str(model.get('index_hash',''))):
                raise ParameterValidationError('index_rate','缺少已校验的索引文件/哈希','missing_index')
            if match.get('content_dim')!=model.get('content_dim') or match.get('embedder_model')!=model.get('embedder_model'):
                raise ParameterValidationError('index_rate','索引维度或编码器与模型不一致','incompatible_index')
            if supplied.get('index_id') and supplied['index_id']!=model.get('index_id'):
                raise ParameterValidationError('index_id','作业所选索引与已解析清单不一致','incompatible_index')
            if supplied.get('index_hash') and supplied['index_hash']!=model.get('index_hash'):
                raise ParameterValidationError('index_hash','作业索引哈希与已解析清单不一致','incompatible_index')
    effective={key:value for key,value in accepted.items() if key not in inactive}
    context={key:copy.deepcopy(value) for key,value in supplied.items() if key in CONTEXT_FIELDS and key not in ALIASES}
    return {'parameter_schema_version':PARAMETER_SCHEMA_VERSION,'accepted_parameters':accepted,'effective_parameters':effective,
            'inactive_parameters':inactive,'warnings':warnings,'parameter_sources':sources,'context_parameters':context,
            'aliases':alias_sources,'f0_order':F0_ORDER,'has_external_f0':bool(has_external_f0)}


def parameter_catalog(model=None,values=None,task='speech',has_external_f0=False,available_algorithms=None,clean_audio_available=False):
    """Return the UI catalogue without requiring a renderable model selection."""
    values=values or {};schema=load_parameter_schema();algorithm=values.get('pitch_algorithm','rmvpe')
    if algorithm=='auto':algorithm='rmvpe'
    ids=_trained_speakers(model or {});rows=[]
    declared_sources=values.get('parameter_sources') if isinstance(values.get('parameter_sources'),dict) else {}
    for key,definition in schema['properties'].items():
        row=copy.deepcopy(definition['x-sonora']);row.update(type=definition['type'],default=definition['default'])
        for constraint in ('minimum','maximum','enum'):
            if constraint in definition:row[constraint]=copy.deepcopy(definition[constraint])
        row['current_value']=copy.deepcopy(values.get(key,row['default']))
        row['current_source']=copy.deepcopy(declared_sources.get(key,'user_override' if key in values else 'executor_default'))
        def disabled(reason):row.update(supported=False,support_reason=reason)
        if key=='speaker_id':
            row['enum']=ids;row['read_only']=len(ids)<=1
            if ids:
                row['default']=0 if 0 in ids else ids[0]
                if key not in values:row.update(current_value=row['default'],current_source='model_metadata')
                row['support_reason']='训练元数据确认的ID；embedding容量仅作诊断。'
                row['recommendation'].update(value=row['default'],source_type='模型已知设置',basis=f'训练元数据已确认ID：{ids}')
            else:disabled('先验证模型的训练说话人元数据；不能使用embedding行数代替人数。')
        if key=='embedder_model':
            row['current_value']=(model or {}).get('embedder_model')
            row['current_source']='model_metadata' if (model or {}).get('embedder_verified') else 'unknown'
            row['recommendation'].update(value=(model or {}).get('embedder_model'),source_type='模型已知设置' if (model or {}).get('embedder_verified') else '未知',
                                         basis='读取已验证训练编码器；未知时不猜测。')
            if not (model or {}).get('embedder_verified'):disabled('模型训练编码器未知，请先验证。')
        if key=='index_rate' and (model or {}).get('index_match',{}).get('status')!='verified':disabled('索引未验证匹配；仅可明确使用0或先完成绑定。')
        if key=='protect' and values.get('index_rate',0)==0:disabled('检索比例为0，此时保护参数不改变输出；开启已验证检索后才生效。')
        if key=='f0_autotune_strength' and not values.get('f0_autotune',False):disabled('自动靠近半音未启用。')
        if key=='context_seconds' and not values.get('range_ticks'):disabled('只用于短片段预览；全源转换不使用此参数。')
        if key=='threshold':
            row['default']=THRESHOLD_DEFAULTS.get(algorithm)
            if values.get(key) is None:row['current_value']=row['default']
            if row['default'] is None:disabled('WORLD不提供此类原生分数门限；请使用音域与独立静音门控。')
            else:row['support_reason']=f'{algorithm} 的原生门限；与其他算法不是同一正确率量纲。'
        if key=='clean_audio' and not clean_audio_available:disabled('当前执行环境未验证noisereduce，不能启用转换后降噪。')
        if key=='clean_strength' and (not clean_audio_available or not values.get('clean_audio',False)):disabled('转换后降噪未启用或依赖不可用。')
        if key=='pitch_algorithm' and available_algorithms is not None:row['enum']=['auto']+list(available_algorithms)
        if key=='volume_envelope':
            row['recommendation']['value']=.5 if task in ('cover','rewrite') else 0
            row['recommendation']['reason']='与当前默认1作同片段比较；'+('歌声可先比较0.5保留部分源动态。' if task in ('cover','rewrite') else '口播可比较0保留源说话动态。')
        if key=='index_rate':row['recommendation']['value']=.5 if (model or {}).get('index_match',{}).get('status')=='verified' else 0
        rows.append(row)
    return {'schema_version':1,'executor':'sonora-rvc','parameter_schema_version':PARAMETER_SCHEMA_VERSION,
            'model_id':(model or {}).get('id'),'task':task,'f0_order':F0_ORDER,'parameters':rows,
            'model_diagnostics':{k:(model or {}).get(k) for k in ('id','model_hash','training_run_id','epoch','global_step','speaker_ids_trained','embedding_speaker_capacity','speaker_metadata_status','embedder_model','embedder_verified','index_id','index_hash','index_match')}}


def pitch_parameter_subset(parameters):
    """Project parameters can be routed to a separate PitchService without ambiguity."""
    keys=F0_ANALYSIS_KEYS|F0_TARGET_KEYS|{'source_timeline_offset_ticks','pitch_edits','parameter_sources'}
    return {key:copy.deepcopy(value) for key,value in parameters.items() if key in keys}


def pitch_analysis_parameters(parameters):
    subset={key:value for key,value in parameters.items() if key in F0_ANALYSIS_KEYS or key in ALIASES}
    accepted=validate_rvc_parameters(subset,scope='pitch')['accepted_parameters']
    algorithm=accepted['pitch_algorithm']
    return {'pitch_algorithm':algorithm,'f0_min':float(accepted['f0_min']),'f0_max':float(accepted['f0_max']),
            'threshold':float(accepted['threshold']) if accepted['threshold'] is not None else None,
            'silence_db':float(accepted['silence_db']),'sample_rate':16000,'hop':160,'frame_length':1024,
            'device':'cpu' if algorithm in ('pyin','harvest','dio') else accepted['device'],
            'resampling':'soxr_vhq','analysis_schema_version':'1.1.0'}


def parameter_hash(parameters):
    import hashlib
    return hashlib.sha256(json.dumps(parameters,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
