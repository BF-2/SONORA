"""Fixed Applio adapter with genuinely consumed external pitch conditions."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import hashlib
import json
import types
import subprocess
import os
import importlib.util
import importlib.metadata
from common import ROOT, worker_main, resource_config, write_json, read_json, environment, safe_torch, setup_applio, device_name, sha256, audio_metadata
from pitch.engine import apply_target_parameters,contour_hash,resample_voiced,shift_pitch_to_project
from pitch.voicing import mute_unpitched, normalize_voicing_settings
from rvc_parameters import validate_rvc_parameters,pitch_parameter_subset,pitch_analysis_parameters,parameter_hash,PARAMETER_SCHEMA_VERSION


def probe():
    result=environment('rvc')
    config=resource_config()
    result['engine_revision']=config['applio']['revision']
    result['source_hash']=config['applio']['source_hash']
    result['capabilities']={'voice_conversion':True,'singing_conversion':True,'external_f0':True,'streaming':False,
                            'svs':False,'local_segment_render':False,'cancel_mode':'terminate_process','device':['cpu','cuda'], 'precision':['float32'],
                            'target_f0_median':True,'target_f0_autotune':True,'clean_audio':importlib.util.find_spec('noisereduce') is not None,
                            'formant_control':False,'proposed_pitch':False,'parameter_schema_version':PARAMETER_SCHEMA_VERSION}
    result['test_status']='adapter_implemented' # Bootstrap registry records model-specific verification.
    return result


def render(request):
    import numpy as np
    import soundfile as sf
    import librosa
    config=resource_config(request); requested_params=request.get('parameters',{})
    model=request.get('model')
    if not model:
        model=next((m for m in config['models'] if m['id']==requested_params.get('model_id')),None)
    if not model: raise ValueError('Select a registered RVC model')
    validation=validate_rvc_parameters(requested_params,model,has_external_f0=bool(request.get('pitch_path')),
                                       clean_audio_available=importlib.util.find_spec('noisereduce') is not None)
    params={**validation['accepted_parameters'],**validation['context_parameters']}
    voicing_settings = normalize_voicing_settings(params.get('voicing_settings'))
    torch=safe_torch()
    if sha256(model['path'])!=model['model_hash']: raise ValueError('Model hash changed; re-register before rendering')
    index_rate=float(params['index_rate']);protect=float(params['protect']) if index_rate>0 else .5
    if index_rate and (not model.get('index_path') or not Path(model['index_path']).is_file()):
        raise ValueError('Retrieval index missing; choose index_rate=0 explicitly')
    if index_rate and model.get('index_hash') and sha256(model['index_path'])!=model['index_hash']:
        raise ValueError('Retrieval index changed; re-register model')
    if index_rate:
        import faiss
        check_index=faiss.read_index(model['index_path'])
        if check_index.d!=model['content_dim'] or check_index.ntotal<8:
            raise ValueError('Retrieval index dimension/count incompatible with model')
        actual_index_metadata={'id':model.get('index_id'),'sha256':model['index_hash'],'dimension':check_index.d,'vectors':check_index.ntotal}
        del check_index
    else:actual_index_metadata=None
    root=setup_applio(config); device=device_name(params)
    torch.manual_seed(int(params.get('seed',0))); np.random.seed(int(params.get('seed',0)))
    torch.set_num_threads(params['cpu_threads'])
    checkpoint=torch.load(model['path'],map_location='cpu',weights_only=True)
    if not checkpoint.get('f0',1): raise ValueError('This adapter currently requires an F0-conditioned RVC model')
    from rvc.lib.algorithm.synthesizers import Synthesizer
    from rvc.lib.utils import HubertModelWithFinalProj
    from rvc.infer.pipeline import Pipeline
    cfg=list(checkpoint['config']); cfg[-3]=int(checkpoint['weight']['emb_g.weight'].shape[0])
    sample_rate=int(cfg[-1]); version=checkpoint.get('version','v1')
    if model.get('content_dim')!=(768 if version=='v2' else 256):raise ValueError('Checkpoint content dimension differs from verified registry metadata')
    if model.get('sample_rate')!=sample_rate:raise ValueError('Checkpoint sample rate differs from verified registry metadata')
    if params['speaker_id']>=cfg[-3]:raise ValueError('Verified speaker ID exceeds actual checkpoint embedding capacity')
    generator=Synthesizer(*cfg,use_f0=True,text_enc_hidden_dim=768 if version=='v2' else 256,vocoder=checkpoint.get('vocoder','HiFi-GAN'))
    del generator.enc_q
    incompatible=generator.load_state_dict(checkpoint['weight'],strict=False)
    critical=[k for k in incompatible.missing_keys if not k.startswith('enc_q.')]
    if critical: raise ValueError('Incomplete model weights: '+','.join(critical[:8]))
    generator=generator.to(device).float().eval()
    embedder_path=root/'rvc/models/embedders/contentvec'
    if not (embedder_path/'pytorch_model.bin').is_file(): raise FileNotFoundError('Content encoder missing; no automatic downloads')
    hubert=HubertModelWithFinalProj.from_pretrained(str(embedder_path),local_files_only=True).to(device).float().eval()
    pipe=Pipeline(sample_rate,types.SimpleNamespace(device=device,x_pad=1,x_query=6,x_center=38,x_max=41))
    retrieval_audit={'calls':0,'feature_rows':0,'index_hash':model.get('index_hash') if index_rate else None}
    native_retrieve=pipe._retrieve_speaker_embeddings
    def audited_retrieval(self,features,index,table,rate):
        retrieval_audit['calls']+=1;retrieval_audit['feature_rows']+=int(features.shape[1])
        return native_retrieve(features,index,table,rate)
    pipe._retrieve_speaker_embeddings=types.MethodType(audited_retrieval,pipe)
    source,source_sr=sf.read(request['input_path'],dtype='float32',always_2d=True)
    full_source_count=len(source)
    source_timeline_offset=int(params.get('source_timeline_offset_ticks',0))
    requested_range=params.get('range_ticks')
    requested_start=0;requested_end=full_source_count;context_start=0;context_end=full_source_count
    timeline_offset_ticks=source_timeline_offset
    if requested_range is not None:
        if not isinstance(requested_range,(list,tuple)) or len(requested_range)!=2:
            raise ValueError('Preview range_ticks must be [start,end]')
        start_tick,end_tick=map(int,requested_range)
        if end_tick<=start_tick or start_tick<source_timeline_offset:
            raise ValueError('Preview selection is outside source timeline')
        requested_start=((start_tick-source_timeline_offset)*source_sr+24000)//48000
        requested_end=((end_tick-source_timeline_offset)*source_sr+24000)//48000
        if requested_end>full_source_count or requested_end-requested_start<source_sr*.1:
            raise ValueError('Preview selection must be within source and at least 0.1 seconds')
        context_seconds=float(params.get('context_seconds',1.0))
        if not 1<=context_seconds<=5: raise ValueError('Preview context must be 1–5 seconds')
        context=int(round(context_seconds*source_sr))
        context_start=max(0,requested_start-context);context_end=min(full_source_count,requested_end+context)
        timeline_offset_ticks=start_tick
        source=source[context_start:context_end]
    source_count=len(source); audio=source.mean(axis=1)
    if source_sr!=16000: audio=librosa.resample(audio,orig_sr=source_sr,target_sr=16000,res_type='soxr_vhq')
    if len(audio)<1600: raise ValueError('RVC clip must be at least 0.1 seconds')
    unpadded_input_count=len(audio)
    if request.get('pitch_path'):
        detected=read_json(request['pitch_path'])
        if detected.get('source_hash')!=sha256(request['input_path']):
            raise ValueError('Pitch source hash does not match conversion input')
        expected_analysis=pitch_analysis_parameters(params)
        if detected.get('analysis_parameters'):
            if detected['analysis_parameters']!=expected_analysis:
                raise ValueError('Saved F0 analysis parameters are stale; re-analyze the same source before conversion')
        elif detected.get('estimator')!=expected_analysis['pitch_algorithm'] or expected_analysis!=pitch_analysis_parameters({'pitch_algorithm':detected.get('estimator')}):
            raise ValueError('Legacy F0 only supports its recorded estimator with default settings; re-analyze to use changed detection parameters')
        else:validation['warnings'].append('复用旧版默认检测曲线；该产物没有完整analysis_parameters记录。')
    else:
        # Use PitchService's own locked runtime, including optional CPU algorithms.
        # This also avoids silently changing algorithms when the RVC environment
        # does not contain the optional estimator package.
        analysis_dir=Path(request['output_dir'])/'pitch-analysis'
        analysis_dir.mkdir(parents=True,exist_ok=True)
        analysis_request={**request,'output_dir':str(analysis_dir),'parameters':pitch_parameter_subset(params)}
        write_json(analysis_dir/'request.json',analysis_request)
        completed=subprocess.run([config.get('pitch',config['applio'])['python'],str(ROOT/'workers/pitch/run.py'),
                                  '--request',str(analysis_dir/'request.json'),'--response',str(analysis_dir/'response.json')],
                                 env={**os.environ,'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'})
        result=read_json(analysis_dir/'response.json')
        if completed.returncode or not result.get('ok'):
            raise RuntimeError('PitchService failed: '+str(result.get('error')))
        detected=read_json(result['outputs'][0]['path'])
    # The target is resolved once; get_f0 below never re-runs an estimator.
    # Imported guide audio can start later on the project timeline. Editing uses
    # project ticks; the native analysis grid is shifted exactly once here.
    detected=shift_pitch_to_project(detected,source_timeline_offset)
    target=apply_target_parameters(detected,params)
    requested_hash=contour_hash(target)
    target_path=Path(request['output_dir'])/'requested-f0.json'; write_json(target_path,target)
    consumed={}; generator_consumption=[]
    original_infer=generator.infer
    def audited_infer(phone,phone_lengths,pitch,pitchf,sid,*args,**kwargs):
        condition=pitchf.detach().float().cpu().numpy()
        generator_consumption.append({'frames':int(condition.shape[-1]),'voiced_frames':int(np.sum(condition>0)),
                                      'unvoiced_frames':int(np.sum(condition<=0)),'speaker_id':int(params['speaker_id']),
                                      'sha256':hashlib.sha256(condition.astype('<f4').tobytes()).hexdigest()})
        return original_infer(phone,phone_lengths,pitch,pitchf,sid,*args,**kwargs)
    generator.infer=audited_infer
    def external_f0(self,x,p_len,*_args,**_kwargs):
        centers=(np.arange(len(target['f0_hz']))*target['hop']+target.get('frame_center_offset',0))/target['sample_rate']
        times=(np.arange(p_len)*self.window-self.t_pad)/self.sample_rate
        # Match the audio context reflection without bridging silence runs.
        duration=(unpadded_input_count-1)/self.sample_rate
        reflected=np.abs((times+duration)%(2*duration)-duration)
        context_origin_seconds=context_start/source_sr+source_timeline_offset/48000
        pitchf=resample_voiced(target['f0_hz'],centers,reflected+context_origin_seconds).astype(np.float32)
        coarse=1127*np.log1p(pitchf/700)
        mask=coarse>0
        coarse[mask]=(coarse[mask]-self.f0_mel_min)*254/(self.f0_mel_max-self.f0_mel_min)+1
        coarse=np.rint(np.clip(coarse,1,255)).astype(np.int64)
        consumed_center=context_origin_seconds*16000-self.t_pad
        consumed.update(sample_rate=16000,hop=160,frame_center_offset=int(consumed_center) if consumed_center.is_integer() else consumed_center,
                        f0_hz=[float(x) if x>0 else None for x in pitchf],voiced=(pitchf>0).tolist())
        return coarse,pitchf
    pipe.get_f0=types.MethodType(external_f0,pipe)
    # Hubert valid convolution produces 20 ms fewer feature-aligned samples.
    # Render additional reflected context, then cut the valid output to source length.
    # This preserves the last syllable instead of adding 20 ms of artificial silence.
    audio=np.pad(audio,(0,320),mode='reflect')
    native_parameters={'pitch':0,'f0_method':'external','index_rate':index_rate,'protect':protect,'volume_envelope':params['volume_envelope'],
                       'sid':params['speaker_id'],'f0_autotune':False,'f0_autotune_strength':0,'proposed_pitch':False,'filter_radius':0,
                       'embedder_model':model['embedder_model'],'device':device,'precision':'float32'}
    with torch.inference_mode():
        converted=pipe.pipeline(model=hubert,net_g=generator,sid=int(params.get('speaker_id',0)),audio=audio,
                                pitch=0,f0_method='external',file_index=model.get('index_path') or '',index_rate=index_rate,
                                pitch_guidance=True,volume_envelope=float(params.get('volume_envelope',1)),version=version,
                                protect=protect,f0_autotune=False,f0_autotune_strength=0,proposed_pitch=False,
                                proposed_pitch_threshold=155,filter_radius=0)
    if not consumed or not generator_consumption: raise RuntimeError('Generator did not consume external F0')
    if index_rate and not retrieval_audit['calls']:raise RuntimeError('Requested retrieval index was never consumed')
    if not np.isfinite(converted).all() or (np.max(np.abs(converted))==0 and any(target['voiced'])): raise ValueError('Invalid or silent model output')
    expected=(source_count*sample_rate+source_sr//2)//source_sr
    original_count=len(converted)
    if abs(original_count-expected)>sample_rate*.04: raise ValueError('Unexpected conversion length drift >40 ms')
    converted=np.pad(converted,(0,max(0,expected-len(converted))))[:expected]
    processing_chain=[{'kind':'model_inference','engine':'rvc','model_hash':model['model_hash'],'index_hash':model.get('index_hash') if index_rate else None}]
    if params['clean_audio']:
        import noisereduce as nr
        before_clean=Path(request['output_dir'])/'before-clean.wav';sf.write(before_clean,converted,sample_rate,subtype='FLOAT')
        converted=nr.reduce_noise(y=converted,sr=sample_rate,prop_decrease=params['clean_strength'],stationary=False,use_torch=False,n_jobs=1)
        if len(converted)!=expected or not np.isfinite(converted).all():raise ValueError('Clean Audio returned invalid data')
        processing_chain.append({'kind':'dsp','engine':'noisereduce','version':importlib.metadata.version('noisereduce'),
                                 'stationary':False,'prop_decrease':params['clean_strength'],'input_hash':sha256(before_clean)})
    valid_start=((requested_start-context_start)*sample_rate+source_sr//2)//source_sr
    valid_count=((requested_end-requested_start)*sample_rate+source_sr//2)//source_sr
    converted=converted[valid_start:valid_start+valid_count]
    if len(converted)!=valid_count: raise ValueError('Preview valid range exceeds rendered context')
    converted, voicing_gate = mute_unpitched(converted, sample_rate, target,
        source_timeline_offset / 48000 + requested_start / source_sr, **voicing_settings)
    processing_chain.append(voicing_gate)
    output=Path(request['output_dir'])/'converted.wav'; sf.write(output,converted,sample_rate,subtype='FLOAT')
    consumed_hash=contour_hash(consumed); write_json(Path(request['output_dir'])/'consumed-f0.json',consumed)
    metadata={'requested_f0_hash':requested_hash,'consumed_f0_hash':consumed_hash,'generator_conditions':generator_consumption,
              'pitch_transform':{'kind':'voiced_log_interpolation_with_reflected_context','source_rate':target['sample_rate'],'source_hop':target['hop'],
                                 'output_rate':16000,'output_hop':160,'padding_samples':pipe.t_pad},
              'device':device,'precision':'float32','seed':int(params.get('seed',0)),'engine_revision':config['applio']['revision'],
              'source_code_hash':config['applio']['source_hash'],'encoder_hash':sha256(embedder_path/'pytorch_model.bin'),
              'input_sample_rate':source_sr,'input_sample_count':full_source_count,'raw_output_samples':original_count,
              'context_sample_range':[context_start,context_end],'valid_source_range':[requested_start,requested_end],
              'timeline_offset_ticks':timeline_offset_ticks,
              'preview_mode':'contextual_excerpt_not_full_equivalence' if requested_range is not None else 'full_source',
              'length_pad_samples':max(0,expected-original_count),'encoder_end_context_samples_16k':320,
              'effective_parameters':{**validation['effective_parameters'],'device':device},
              'inactive_parameters':validation['inactive_parameters'],'parameter_sources':validation['parameter_sources'],
              'context_parameters':validation['context_parameters'],'parameter_schema_version':PARAMETER_SCHEMA_VERSION,
              'native_parameters':native_parameters,'index_metadata':actual_index_metadata,'retrieval_audit':retrieval_audit,
              'private_adapter_sha256':sha256(Path(__file__)),
              'analysis_parameters':detected.get('analysis_parameters',pitch_analysis_parameters(params)),
              'analysis_parameters_hash':detected.get('analysis_parameters_hash',parameter_hash(pitch_analysis_parameters(params))),
              'target_parameters':target['target_parameters'],'target_parameters_hash':target['target_parameters_hash'],
              'target_processing_order':target['target_processing_order'],'processing_chain':processing_chain}
    if torch.cuda.is_available(): metadata['peak_cuda_bytes']=torch.cuda.max_memory_allocated()
    return {'outputs':[audio_metadata(output,'converted','rvc',model_hash=model['model_hash'],requested_f0_hash=requested_hash,
                                     consumed_f0_hash=consumed_hash,source_offset=requested_start,source_sample_rate=source_sr,
                                     timeline_offset_ticks=timeline_offset_ticks,
                                     preview_mode=metadata['preview_mode'],context_sample_range=[context_start,context_end],
                                     index_id=model.get('index_id') if index_rate else None,index_hash=model.get('index_hash') if index_rate else None,
                                     effective_parameters=metadata['effective_parameters'],parameter_schema_version=PARAMETER_SCHEMA_VERSION,
                                     time_map={'source_rate':source_sr,'output_rate':sample_rate,'source_offset':requested_start,'output_offset':0})],
            'metadata':metadata,'warnings':validation['warnings']+target.get('target_warnings',[])+['算法固定延迟未做回环实测；模型音质需试听验收。']}


if __name__=='__main__': raise SystemExit(worker_main('rvc',render,probe))
