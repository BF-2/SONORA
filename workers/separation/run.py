"""Offline, pinned audio-separator adapter. Model roles come from verified configs."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import logging
from common import worker_main, resource_config, environment, safe_torch, device_name, sha256, audio_metadata


def probe():
    result=environment('separation')
    result['capabilities']={'stem_separation':True,'stems':['vocal','instrumental'],'lead_backing_separation':False,
                            'local_segment_render':False,'cancel_mode':'terminate_process','device':['cpu','cuda']}
    return result


def render(request):
    import yaml
    import soundfile as sf
    import numpy as np
    torch=safe_torch(); config=resource_config(request); params=request.get('parameters',{})
    section=config['separation']; selected=params.get('separation_model_id',section['default_model'])
    manifest=next((m for m in section['models'] if m['id']==selected),None)
    if not manifest: raise ValueError('Registered separation model missing')
    if sha256(manifest['path'])!=manifest['model_hash'] or sha256(manifest['config_path'])!=manifest['config_hash']:
        raise ValueError('Separation model/config hash changed; re-register before rendering')
    # The known tuple is data, handled by SafeLoader only; every other Python tag remains forbidden.
    class DataLoader(yaml.SafeLoader): pass
    DataLoader.add_constructor('tag:yaml.org,2002:python/tuple',lambda loader,node:tuple(loader.construct_sequence(node)))
    model_data=yaml.load(Path(manifest['config_path']).read_text(encoding='utf-8'),Loader=DataLoader)
    if model_data['training'].get('target_instrument')!='Vocals' or model_data['training']['instruments']!=['Vocals','Instrumental']:
        raise ValueError('Unverified stem semantics; adapter requires confirmed Vocals/Instrumental config')
    model_data['is_roformer']=True
    from audio_separator.separator import Separator
    device=device_name(params)
    separator=Separator(output_dir=request['output_dir'],model_file_dir=section['model_dir'],output_format='WAV',
                        sample_rate=manifest['sample_rate'],normalization_threshold=1.0,amplification_threshold=0,
                        use_soundfile=True,log_level=logging.INFO,
                        mdxc_params={'segment_size':int(params.get('segment_size',256)),'override_model_segment_size':True,
                                     'batch_size':1,'overlap':int(params.get('overlap',4)),'pitch_shift':0})
    separator.torch_device=torch.device(device)
    # Bypass network catalogue and YAML FullLoader using the pinned manifest.
    separator.download_model_files=lambda _name:(Path(manifest['path']).name,'MDXC',manifest['name'],manifest['path'],manifest['config_path'])
    separator.load_model_data_from_yaml=lambda _path:model_data
    separator.load_model(Path(manifest['path']).name)
    # Preserve floating-point intermediate stems rather than quantizing on each stage.
    def float_write(stem_path, source):
        destination=Path(request['output_dir'])/Path(stem_path).name
        sf.write(destination,np.asarray(source,dtype=np.float32),manifest['sample_rate'],subtype='FLOAT')
    separator.model_instance.write_audio=float_write
    outputs=separator.separate(request['input_path'],custom_output_names={'Vocals':'vocal','Instrumental':'instrumental'})
    artifacts=[]
    for role in ('vocal','instrumental'):
        path=Path(request['output_dir'])/(role+'.wav')
        if not path.is_file(): raise RuntimeError('Expected explicit stem missing: '+role+' '+str(outputs))
        audio,sr=sf.read(path,always_2d=True)
        if not np.isfinite(audio).all(): raise ValueError('Nonfinite separation output')
        source_info=sf.info(request['input_path'])
        expected=(source_info.frames*sr+source_info.samplerate//2)//source_info.samplerate
        if abs(len(audio)-expected)>1: raise ValueError('Separation output duration differs from source')
        artifacts.append(audio_metadata(path,role,'audio-separator',model_hash=manifest['model_hash'],
                                       time_map={'source_rate':source_info.samplerate,'output_rate':sr,'source_offset':0,'output_offset':0}))
    return {'outputs':artifacts,'metadata':{'model_id':manifest['id'],'config_hash':manifest['config_hash'],'device':device,
                                          'engine_version':section['version'],'stem_roles':manifest['stem_roles'],
                                          'effective_parameters':params},
            'warnings':['人声/伴奏分离不等于主唱/和声分离；请检查重叠声部与接缝。']}


if __name__=='__main__': raise SystemExit(worker_main('separation',render,probe))
