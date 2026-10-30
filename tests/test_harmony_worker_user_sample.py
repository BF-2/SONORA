"""Explicit two-stage test of the user's confirmed 1:29–1:55 overlap passage."""
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys
import numpy as np
import soundfile as sf
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'workers'))
from common import read_json,write_json,sha256


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run-real',action='store_true')
    args=parser.parse_args()
    if not args.run_real:parser.error('--run-real is required')
    folder=ROOT/'tests/results/harmony/user-89-115'
    source=folder/'source.wav'
    original=Path(r'D:\phpstudy_pro\WWW\video\video\BREAK FROM THE LINE (feat. Taylor Louderman & Natalie Weiss) by Joey Contreras.mp3')
    original_hash=sha256(original);source_hash=sha256(source)
    config=read_json(ROOT/'engine-envs/resources.json')
    def job(name,input_path,parameters):
        out=folder/name;out.mkdir(exist_ok=True)
        request={'protocol_version':1,'job_id':'user-overlap-'+name,'input_path':str(input_path),'output_dir':str(out),
                 'resource_config_path':str(ROOT/'engine-envs/resources.json'),'parameters':parameters}
        write_json(out/'request.json',request)
        with (out/'worker.log').open('w',encoding='utf-8') as log:
            completed=subprocess.run([config['separation']['python'],str(ROOT/'workers/separation/run.py'),'--request',str(out/'request.json'),'--response',str(out/'response.json')],
                                     stdout=log,stderr=subprocess.STDOUT,env={**os.environ,'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'})
        response=read_json(out/'response.json')
        assert completed.returncode==0 and response.get('ok'),response
        print(json.dumps({'stage':name,'elapsed_seconds':response['metadata']['elapsed_seconds'],'outputs':[{k:a[k] for k in ['role','sample_rate','sample_count','channels']} for a in response['outputs']]},ensure_ascii=True),flush=True)
        return response
    general=job('vocal-instrumental',source,{'separation_task':'vocals_instrumental','device':'cuda','segment_size':801,'overlap':4})
    vocal=next(a for a in general['outputs'] if a['role']=='vocal')
    harmony=job('lead-backing',Path(vocal['path']),{'separation_task':'lead_backing','device':'cuda'})
    arrays={}
    for response in (general,harmony):
        for artifact in response['outputs']:
            data,sr=sf.read(artifact['path'],dtype='float32',always_2d=True)
            assert sr==44100 and data.shape==(26*44100,2)
            assert np.isfinite(data).all()
            arrays[artifact['role']]=data.astype(np.float64)
    input_audio,input_sr=sf.read(source,always_2d=True)
    general_error=arrays['vocal']+arrays['instrumental']-input_audio
    harmony_error=arrays['lead_vocal']+arrays['backing_vocal']-arrays['vocal']
    assert np.max(np.abs(harmony_error))<1e-6
    report={'passed_technical_checks':True,'original_path':str(original),'original_sha256_before':original_hash,'original_sha256_after':sha256(original),
            'source_interval_seconds':[89,115],'source_hash_before':source_hash,'source_hash_after':sha256(source),
            'sample_rate':44100,'sample_count':26*44100,'channels':2,
            'first_stage':{'model_id':general['metadata']['model_id'],'parameters':general['metadata']['effective_parameters'],'elapsed_seconds':general['metadata']['elapsed_seconds']},
            'second_stage':harmony['metadata'],
            'general_reconstruction_max_abs':float(np.max(np.abs(general_error))),'general_reconstruction_rms':float(np.sqrt(np.mean(general_error**2))),
            'harmony_reconstruction_max_abs':float(np.max(np.abs(harmony_error))),'harmony_reconstruction_rms':float(np.sqrt(np.mean(harmony_error**2))),
            'stem_rms':{role:float(np.sqrt(np.mean(value**2))) for role,value in arrays.items()},
            'artifacts':general['outputs']+harmony['outputs'],'warnings':harmony['warnings'],
            'listening_status':'not accepted; user-specified overlap passage, no ground truth clean stems or automatic quality score'}
    assert report['original_sha256_before']==report['original_sha256_after'] and report['source_hash_before']==report['source_hash_after']
    write_json(folder/'worker-verification.json',report)
    write_json(ROOT/'engine-envs/harmony/user-sample-verification.json',report)
    print(json.dumps({k:v for k,v in report.items() if k in ['passed_technical_checks','source_interval_seconds','sample_count','stem_rms','harmony_reconstruction_max_abs','general_reconstruction_max_abs']},ensure_ascii=True),flush=True)


if __name__=='__main__':main()
