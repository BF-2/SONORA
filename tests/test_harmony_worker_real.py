"""Opt-in real lead/backing inference on an existing private combined vocal.

This checks pipeline/signal integrity, not singer-identification accuracy.
"""
from pathlib import Path
import argparse
import json
import os
import re
import subprocess
import sys
import numpy as np
import soundfile as sf
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'workers'))
from common import sha256,read_json,write_json


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run-real',action='store_true')
    parser.add_argument('--device',choices=['cpu','cuda'],default='cuda')
    parser.add_argument('--input',type=Path)
    parser.add_argument('--name',default='worker')
    parser.add_argument('--segment-size',type=int)
    args=parser.parse_args()
    if not args.run_real:parser.error('--run-real required to run the actual model')
    config=read_json(ROOT/'engine-envs/resources.json')
    if args.input:source=args.input.resolve()
    else:
        cover=read_json(ROOT/'tests/results/integration/cover.json')
        original=next(a for a in cover['artifacts'] if a['stem_role']=='vocal')
        source=ROOT/'runtime/cache'/original['sha256'][:2]/(original['sha256']+'.wav')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',args.name):raise ValueError('Invalid test name')
    output=ROOT/'tests/results/harmony'/args.name;output.mkdir(parents=True,exist_ok=True)
    before=sha256(source);source_audio,source_sr=sf.read(source,dtype='float32',always_2d=True)
    request={'protocol_version':1,'job_id':'harmony-real-signal-check','input_path':str(source),'output_dir':str(output),
             'resource_config_path':str(ROOT/'engine-envs/resources.json'),
             'parameters':{'separation_task':'lead_backing','harmony_model_id':config['separation']['default_harmony_model'],
                           'device':args.device,'source_timeline_offset_ticks':96000}}
    if args.segment_size:request['parameters']['segment_size']=args.segment_size
    write_json(output/'request.json',request)
    with (output/'worker.log').open('w',encoding='utf-8') as log:
        job=subprocess.run([config['separation']['python'],str(ROOT/'workers/separation/run.py'),'--request',str(output/'request.json'),'--response',str(output/'response.json')],
                           stdout=log,stderr=subprocess.STDOUT,env={**os.environ,'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'})
    result=read_json(output/'response.json')
    if job.returncode or not result.get('ok'):raise AssertionError(result)
    stems={a['role']:a for a in result['outputs']}
    assert set(stems)=={'lead_vocal','backing_vocal'}
    lead,sr=sf.read(stems['lead_vocal']['path'],dtype='float32',always_2d=True)
    backing,back_sr=sf.read(stems['backing_vocal']['path'],dtype='float32',always_2d=True)
    assert sr==source_sr==back_sr==44100
    assert lead.shape==backing.shape==source_audio.shape
    assert np.isfinite(lead).all() and np.isfinite(backing).all()
    diff=lead.astype(np.float64)+backing.astype(np.float64)-source_audio.astype(np.float64)
    reconstruction_max=float(np.max(np.abs(diff)))
    assert reconstruction_max<1e-6
    assert np.sqrt(np.mean((lead-source_audio)**2))>1e-7,'Output was unchanged source'
    assert sha256(source)==before,'Source changed'
    for artifact in stems.values():
        assert artifact['processing_kind']=='model_inference'
        assert artifact['timeline_offset_ticks']==96000
        assert artifact['model_hash'] and artifact['config_hash']
    report={'passed':True,'scope':'Real local combined-vocal neural split; technical integrity only, no ground truth lead/back accuracy claim',
            'input_path':str(source),'input_sha256_before':before,'input_sha256_after':sha256(source),
            'input_sample_rate':source_sr,'sample_count':len(source_audio),'channels':source_audio.shape[1],
            'model_id':result['metadata']['model_id'],'model_hash':stems['lead_vocal']['model_hash'],
            'config_hash':stems['lead_vocal']['config_hash'],'elapsed_seconds':result['metadata']['elapsed_seconds'],
            'peak_cuda_bytes':result['metadata'].get('peak_cuda_bytes'),'device':result['metadata']['device'],
            'reconstruction_max_abs':reconstruction_max,'reconstruction_rms':float(np.sqrt(np.mean(diff**2))),
            'stem_rms':result['metadata']['stem_rms'],'outputs':result['outputs'],'warnings':result['warnings']}
    write_json(ROOT/'engine-envs/harmony/verification.json',report)
    write_json(ROOT/'engine-envs/harmony'/f'verification-{args.name}.json',report)
    manifest_path=ROOT/'engine-envs/harmony/model-manifest.json';manifest=read_json(manifest_path)
    manifest['test_status']='verified_local_combined_vocal';manifest['verification']='engine-envs/harmony/verification.json';write_json(manifest_path,manifest)
    config=read_json(ROOT/'engine-envs/resources.json')
    config['separation']['models']=[manifest if m['id']==manifest['id'] else m for m in config['separation']['models']]
    write_json(ROOT/'engine-envs/resources.json',config)
    print(json.dumps({k:v for k,v in report.items() if k not in ('outputs','warnings','input_path')},ensure_ascii=True),flush=True)


if __name__=='__main__':main()
