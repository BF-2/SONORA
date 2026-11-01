"""Offline, pinned audio-separator adapter. Model roles come from verified configs."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import logging
from common import worker_main, resource_config, environment, safe_torch, device_name, sha256, audio_metadata
from separation.options import separation_options


def probe():
    result=environment('separation')
    section=resource_config()['separation']
    harmony=[m for m in section['models'] if m.get('task')=='lead_backing' and Path(m['path']).is_file() and Path(m['config_path']).is_file()]
    result['harmony_models']=[{k:m.get(k) for k in ('id','name','task','test_status','native_stem_roles','model_hash','license','warnings')} for m in harmony]
    result['capabilities']={'stem_separation':True,'stems':['vocal','instrumental'],'lead_backing_separation':bool(harmony),
                            'local_segment_render':False,'cancel_mode':'terminate_process','device':['cpu','cuda']}
    return result


def validate_semantics(manifest,model_data,task):
    """An explicit task manifest establishes meaning; native labels alone do not."""
    if task not in ('vocals_instrumental','lead_backing'): raise ValueError('Unknown separation task')
    if manifest.get('task','vocals_instrumental')!=task: raise ValueError('Selected model is registered for a different separation task')
    mapping=manifest.get('native_stem_roles',manifest.get('stem_roles',{}))
    expected={'lead_vocal','backing_vocal'} if task=='lead_backing' else {'vocal','instrumental'}
    if set(mapping.values())!=expected: raise ValueError('Unverified native stem mapping for task')
    training=model_data.get('training',{})
    if set(training.get('instruments',[]))!=set(mapping): raise ValueError('Model native stem names differ from registered config semantics')
    target=training.get('target_instrument')
    if target not in mapping: raise ValueError('Unverified target stem')
    if task=='lead_backing':
        if not manifest.get('semantic_sources') or manifest.get('native_target')!=target:
            raise ValueError('Harmony model needs explicit source-backed target semantics')
        if mapping[target]!='lead_vocal': raise ValueError('This harmony adapter requires a direct lead estimate and backing residual')
        residual=manifest.get('residual_native_stem')
        if mapping.get(residual)!='backing_vocal': raise ValueError('Backing residual semantics missing')
    return mapping,target


def harmony_demix(separator,request,manifest,target):
    """Run a neural target and its complementary residual with original levels."""
    import numpy as np
    import soundfile as sf
    import librosa
    source,sr=sf.read(request['input_path'],dtype='float32',always_2d=True)
    if source.shape[1] not in (1,2): raise ValueError('Separation accepts mono or stereo audio')
    source_count=len(source);channels=source.shape[1];target_sr=manifest['sample_rate']
    mapping=manifest.get('native_stem_roles',manifest['stem_roles'])
    primary_role=mapping[target]
    secondary_role=next(role for native,role in mapping.items() if native!=target)
    if sr!=target_sr: source=librosa.resample(source,orig_sr=sr,target_sr=target_sr,axis=0,res_type='soxr_vhq')
    expected=(source_count*target_sr+sr//2)//sr
    source=np.pad(source,((0,max(0,expected-len(source))),(0,0)))[:expected]
    if not np.isfinite(source).all(): raise ValueError('Nonfinite input samples')
    mix=source.T if channels==2 else np.repeat(source.T,2,axis=0)
    # Preserve the upstream inference input range, then undo that shared gain.
    # Never normalize individual intermediate stems, including peaks above 0dBFS.
    source_peak=float(np.max(np.abs(mix)))
    model_input_gain=1/max(1.,source_peak)
    mix=mix*model_input_gain
    cfg=getattr(separator.model_instance,'model_data_cfgdict',None)
    stft_hop=getattr(getattr(cfg,'model',None),'stft_hop_length',441)
    chunk_size=int(stft_hop)*(separator.model_instance.segment_size-1)
    padding=max(0,chunk_size-mix.shape[1])
    if padding: mix=np.pad(mix,((0,0),(0,padding)),mode='reflect' if mix.shape[1]>1 else 'edge')
    # Call the installed inference implementation directly to avoid individual
    # output normalization breaking source = lead + backing.
    predicted=separator.model_instance.demix(mix)
    if not isinstance(predicted,dict) or target not in predicted: raise RuntimeError('Model did not return its registered target')
    estimate=np.asarray(predicted[target],dtype=np.float32)
    options=separation_options(request.get('parameters',{}))
    if options['separation_algorithm']=='tta':
        swapped=separator.model_instance.demix(mix[::-1].copy())[target]
        inverted=separator.model_instance.demix(-mix)[target]
        estimate=(estimate+np.asarray(swapped)[::-1]-np.asarray(inverted))/3
    lead=estimate[:,:expected].T/model_input_gain*options['primary_gain']
    if channels==1: lead=lead.mean(axis=1,keepdims=True)
    if lead.shape!=source.shape or not np.isfinite(lead).all(): raise ValueError('Invalid lead estimate shape or samples')
    backing=source-lead
    outputs=[]
    for role,data in [(primary_role,lead),(secondary_role,backing)]:
        path=Path(request['output_dir'])/(role+'.wav')
        sf.write(path,data,target_sr,subtype='FLOAT')
        outputs.append(path)
    residual=(lead.astype(np.float64)+backing.astype(np.float64))-source.astype(np.float64)
    metadata={'input_sample_rate':sr,'input_sample_count':source_count,'input_channels':channels,'model_input_channels':2,
              'right_context_padding_samples':padding,'neural_target_native_stem':target,
              'complement_method':f'{secondary_role} = resampled_input - neural_{primary_role}_estimate',
              'model_input_gain':model_input_gain,'input_gain_restored_before_commit':True,'input_sample_peak':source_peak,
              'inference_passes':3 if options['separation_algorithm']=='tta' else 1,
              'primary_gain':options['primary_gain'],'chunk_seconds':chunk_size/target_sr,
              'effective_step_seconds':min(options['overlap_step_seconds'],chunk_size/target_sr),
              'independent_stem_normalization':False,'reconstruction_max_abs':float(np.max(np.abs(residual))),
              'reconstruction_rms':float(np.sqrt(np.mean(residual**2))),
              'stem_rms':{primary_role:float(np.sqrt(np.mean(lead.astype(np.float64)**2))),
                          secondary_role:float(np.sqrt(np.mean(backing.astype(np.float64)**2)))}}
    return outputs,metadata


def render(request):
    import yaml
    import soundfile as sf
    import numpy as np
    torch=safe_torch(); config=resource_config(request); params=request.get('parameters',{})
    section=config['separation'];task=params.get('separation_task','vocals_instrumental')
    selected=(params.get('harmony_model_id') or section.get('default_harmony_model')) if task=='lead_backing' else params.get('separation_model_id',section['default_model'])
    manifest=next((m for m in section['models'] if m['id']==selected),None)
    if not manifest: raise ValueError('Registered separation model missing')
    if sha256(manifest['path'])!=manifest['model_hash'] or sha256(manifest['config_path'])!=manifest['config_hash']:
        raise ValueError('Separation model/config hash changed; re-register before rendering')
    # The known tuple is data, handled by SafeLoader only; every other Python tag remains forbidden.
    class DataLoader(yaml.SafeLoader): pass
    DataLoader.add_constructor('tag:yaml.org,2002:python/tuple',lambda loader,node:tuple(loader.construct_sequence(node)))
    model_data=yaml.load(Path(manifest['config_path']).read_text(encoding='utf-8'),Loader=DataLoader)
    mapping,target=validate_semantics(manifest,model_data,task)
    model_data['is_roformer']=True
    from audio_separator.separator import Separator
    device=device_name(params)
    options=separation_options(params)
    segment_size=options['segment_size'];overlap=options['overlap_step_seconds']
    torch.set_num_threads(options['cpu_threads'])
    separator=Separator(output_dir=request['output_dir'],model_file_dir=section['model_dir'],output_format='WAV',
                        sample_rate=manifest['sample_rate'],normalization_threshold=1.0,amplification_threshold=0,
                        use_soundfile=True,log_level=logging.INFO,
                        mdxc_params={'segment_size':segment_size,'override_model_segment_size':True,
                                     'batch_size':1,'overlap':overlap,'pitch_shift':0})
    separator.torch_device=torch.device(device)
    # Bypass network catalogue and YAML FullLoader using the pinned manifest.
    separator.download_model_files=lambda _name:(Path(manifest['path']).name,'MDXC',manifest['name'],manifest['path'],manifest['config_path'])
    separator.load_model_data_from_yaml=lambda _path:model_data
    separator.load_model(Path(manifest['path']).name)
    outputs,task_metadata=harmony_demix(separator,request,manifest,target)
    artifacts=[]
    timeline_offset=int(params.get('source_timeline_offset_ticks',0))
    source_offset=int(params.get('source_offset_samples',0))
    for role in mapping.values():
        path=Path(request['output_dir'])/(role+'.wav')
        if not path.is_file(): raise RuntimeError('Expected explicit stem missing: '+role+' '+str(outputs))
        audio,sr=sf.read(path,always_2d=True)
        if not np.isfinite(audio).all(): raise ValueError('Nonfinite separation output')
        source_info=sf.info(request['input_path'])
        expected=(source_info.frames*sr+source_info.samplerate//2)//source_info.samplerate
        if abs(len(audio)-expected)>1: raise ValueError('Separation output duration differs from source')
        artifacts.append(audio_metadata(path,role,'audio-separator',model_hash=manifest['model_hash'],
                                       config_hash=manifest['config_hash'],separation_task=task,source_offset=source_offset,
                                       source_sample_rate=source_info.samplerate,timeline_offset_ticks=timeline_offset,
                                       native_stem=next(k for k,v in mapping.items() if v==role),
                                       time_map={'source_rate':source_info.samplerate,'output_rate':sr,'source_offset':source_offset,'output_offset':0}))
    metadata={'model_id':manifest['id'],'config_hash':manifest['config_hash'],'device':device,'separation_task':task,
              'engine_version':section['version'],'stem_roles':mapping,'effective_parameters':{**params,**options},**task_metadata}
    if device.startswith('cuda'):metadata['peak_cuda_bytes']=torch.cuda.max_memory_allocated()
    warnings=list(manifest.get('warnings',[])) if task=='lead_backing' else ['人声/伴奏分离不等于主唱/和声分离；请检查重叠声部与接缝。']
    if task=='lead_backing':
        levels=list(task_metadata['stem_rms'].values())
        if min(levels)<max(max(levels),1e-8)*.01:
            warnings.append('某个声部接近静音：可能没有相应和声，也可能模型未能分开；请对照原人声试听，不要据此判断歌手数量。')
            metadata['review_required']=True
    return {'outputs':artifacts,'metadata':metadata,'warnings':warnings}


if __name__=='__main__': raise SystemExit(worker_main('separation',render,probe))
