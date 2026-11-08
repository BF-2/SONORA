"""Consolidated real-parameter audit, only run during the coordinated final phase.

Usage: <core Python> tests/test_rvc_parameters_real.py --run-real
       --model-manifest <ModelCatalog.resolve_model JSON> [--device cuda|cpu]

The manifest must include verified trained speaker/encoder/index evidence. This
runner never rewrites legacy resource manifests, downloads, or upgrades anything.
All cases share one source and one raw F0 artifact. Signal effects are reported
separately from no-op conditions and are not subjective audio quality scores.
"""
from pathlib import Path
import argparse
import copy
import json
import math
import os
import subprocess
import sys
import time
sys.dont_write_bytecode=True
import numpy as np
import soundfile as sf
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'workers'))
from common import read_json,write_json,sha256
from rvc_parameters import validate_rvc_parameters


def make_fixture(path):
    sr=44100;t=np.arange(5*sr)/sr
    f0=220*2**((.3+.18*np.sin(2*np.pi*5*t)+.08*np.sin(2*np.pi*13*t))/12)
    phase=np.cumsum(2*np.pi*f0/sr)
    voiced=((t>=.25)&(t<1.55))|((t>=2.15)&(t<4.6))
    envelope=np.where(t<2,.11,.30)*np.clip(np.minimum(t/.04,(5-t)/.04),0,1)
    tonal=sum(np.sin(phase*k)/k for k in range(1,8))/2.4
    noise=np.random.default_rng(819).standard_normal(len(t))*.045
    unvoiced=(t>=1.55)&(t<2.15)
    audio=(tonal*envelope*voiced+noise*unvoiced).astype(np.float32)
    sf.write(path,audio,sr,subtype='FLOAT')
    return {'sample_rate':sr,'sample_count':len(audio),'nominal_voiced_hz':220,'unvoiced_region_ticks':[74400,103200],
            'scope':'self-generated dynamic harmonic vowel plus unvoiced noise; engineering fixture, not real-singer quality acceptance'}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run-real',action='store_true')
    parser.add_argument('--model-manifest',type=Path)
    parser.add_argument('--device',choices=['cpu','cuda'],default='cuda')
    parser.add_argument('--output',type=Path,default=ROOT/'tests/results/rvc-parameters')
    args=parser.parse_args()
    if not args.run_real or not args.model_manifest:parser.error('Explicit --run-real and verified --model-manifest required')
    model=read_json(args.model_manifest)
    if 'model' in model and isinstance(model['model'],dict):model=model['model']
    # Fail before GPU work if registry verification is missing or incompatible.
    validate_rvc_parameters({'index_rate':.5,'index_id':model.get('index_id')},model)
    config=read_json(ROOT/'engine-envs/resources.json')
    folder=args.output.resolve();folder.mkdir(parents=True,exist_ok=True)
    source=folder/'parameter-fixture.wav';fixture=make_fixture(source);write_json(folder/'fixture.json',fixture)
    original_hashes={key:sha256(model[key]) for key in ('path','index_path')}
    responses={};started=time.monotonic()
    def run(engine,name,params,input_path=source,pitch=None):
        output=folder/name;output.mkdir(exist_ok=True)
        request={'protocol_version':1,'job_id':'rvc-parameter-'+name,'input_path':str(input_path),'output_dir':str(output),
                 'parameters':params,'resource_config_path':str(ROOT/'engine-envs/resources.json')}
        if engine=='rvc':request['model']=model
        if pitch:request['pitch_path']=str(pitch)
        write_json(output/'request.json',request)
        python=config.get('pitch',config['applio'])['python'] if engine=='pitch' else config['applio']['python']
        with (output/'worker.log').open('w',encoding='utf-8') as log:
            completed=subprocess.run([python,'-B',str(ROOT/'workers'/engine/'run.py'),'--request',str(output/'request.json'),'--response',str(output/'response.json')],
                                     stdout=log,stderr=subprocess.STDOUT,env={**os.environ,'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'})
        response=read_json(output/'response.json')
        if completed.returncode or not response.get('ok'):raise AssertionError({'case':name,'response':response})
        responses[name]=response
        print(json.dumps({'case':name,'elapsed_seconds':response['metadata']['elapsed_seconds']},ensure_ascii=True),flush=True)
        return response
    raw=run('pitch','raw-analysis',{'pitch_algorithm':'rmvpe','device':args.device})
    pitch_path=Path(raw['outputs'][0]['path']);pitch_hash=sha256(pitch_path)
    detected=read_json(pitch_path)
    base={'model_id':model['id'],'index_id':model['index_id'],'pitch_algorithm':'rmvpe','device':args.device,'seed':0,
          'index_rate':0,'protect':.33,'volume_envelope':1,
          'pitch_edits':[{'id':'known-unvoiced','start_tick':74400,'end_tick':103200,'operation':'unvoiced'}]}
    cases=[('reference',{}),('octave',{'transpose':12}),('index-mid',{'index_rate':.5,'protect':.5}),
           ('index-protect',{'index_rate':.5,'protect':0}),('protect-no-index',{'protect':.5}),
           ('source-envelope',{'volume_envelope':0}),('smooth-target',{'filter_radius':3}),
           ('autotune-target',{'f0_autotune':True,'f0_autotune_strength':1}),('clean-output',{'clean_audio':True,'clean_strength':.5})]
    for name,overrides in cases:run('rvc',name,{**base,**overrides},pitch=pitch_path)
    reference_target=read_json(folder/'reference/requested-f0.json')
    snapshot={key:copy.deepcopy(reference_target[key]) for key in ('sample_rate','hop','frame_center_offset','f0_hz')}
    lock={'id':'locked-known-target','start_tick':24000,'end_tick':60000,'operation':'lock','locked':True,'snapshot':snapshot}
    run('rvc','locked-auto-combination',{**base,'filter_radius':3,'f0_autotune':True,'f0_autotune_strength':1,'transpose':12,
                                        'pitch_edits':[lock]+base['pitch_edits']},pitch=pitch_path)
    # Independently measure the octave output; never use the requested curve as output measurement.
    for name in ('reference','octave'):
        run('pitch','measure-'+name,{'pitch_algorithm':'rmvpe','device':args.device},input_path=Path(responses[name]['outputs'][0]['path']))
    # Verify actual analysis controls too, with a deliberately excluding range.
    run('pitch','range-exclusion',{'pitch_algorithm':'rmvpe','f0_min':500,'f0_max':1000,'device':args.device})
    arrays={name:sf.read(response['outputs'][0]['path'],dtype='float64')[0] for name,response in responses.items() if response['outputs'][0]['role']=='converted'}
    sr=responses['reference']['outputs'][0]['sample_rate'];expected=5*sr
    assert all(len(a)==expected and np.isfinite(a).all() for a in arrays.values())
    def difference(a,b):
        delta=arrays[a]-arrays[b]
        return {'rms_difference':float(np.sqrt(np.mean(delta**2))),'max_abs_difference':float(np.max(np.abs(delta)))}
    checks={name:difference(name,'reference') for name in arrays if name!='reference'}
    checks['protect-with-index']=difference('index-protect','index-mid')
    for name in ('index-mid','source-envelope','smooth-target','autotune-target','clean-output','protect-with-index'):
        checks[name]['observed_effect']=checks[name]['rms_difference']>1e-7
    noop=checks['protect-no-index'];noop['expected_no_op_condition']='index_rate=0: retrieval features equal source features'
    noop['within_numeric_tolerance']=noop['rms_difference']<1e-6
    # Required controls must genuinely affect this deliberately applicable fixture.
    assert all(checks[name]['observed_effect'] for name in ('index-mid','source-envelope','autotune-target','protect-with-index')),checks
    assert noop['within_numeric_tolerance'],noop
    for name in ('index-mid','index-protect'):
        metadata=responses[name]['metadata']
        assert metadata['retrieval_audit']['calls']>0
        assert metadata['retrieval_audit']['index_hash']==model['index_hash']
        assert sum(c['unvoiced_frames'] for c in metadata['generator_conditions'])>0
    def measured_pitch(name):
        d=read_json(responses['measure-'+name]['outputs'][0]['path'])
        values=np.array([f or 0 for f in d['f0_hz']])[35:145]
        return float(np.median(values[values>0]))
    base_hz,octave_hz=measured_pitch('reference'),measured_pitch('octave')
    cents=1200*math.log2(octave_hz/base_hz);assert 1000<cents<1400,(base_hz,octave_hz,cents)
    target_locked=read_json(folder/'locked-auto-combination/requested-f0.json')
    for index in range(50,125):assert target_locked['f0_hz'][index]==snapshot['f0_hz'][index]
    assert responses['reference']['outputs'][0]['requested_f0_hash']!=responses['octave']['outputs'][0]['requested_f0_hash']
    assert responses['reference']['outputs'][0]['consumed_f0_hash']!=responses['octave']['outputs'][0]['consumed_f0_hash']
    filtered=read_json(responses['range-exclusion']['outputs'][0]['path'])
    assert sum(filtered['voiced'])<sum(detected['voiced'])/2
    assert sha256(pitch_path)==pitch_hash
    assert all(sha256(model[key])==digest for key,digest in original_hashes.items())
    report={'passed_applicable_parameter_checks':True,'scope':fixture['scope'],'model_id':model['id'],'model_hash':model['model_hash'],
            'index_id':model['index_id'],'index_hash':model['index_hash'],'speaker_ids_trained':model['speaker_ids_trained'],
            'source_hash':sha256(source),'shared_raw_f0_hash':pitch_hash,'input_weights_unchanged':True,'parameter_differences':checks,
            'octave_response':{'baseline_measured_hz':base_hz,'shifted_measured_hz':octave_hz,'measured_cents':cents},
            'locked_snapshot_exact':True,'range_exclusion_voiced_frames':[sum(detected['voiced']),sum(filtered['voiced'])],
            'elapsed_seconds':time.monotonic()-started,'case_metadata':{name:r['metadata'] for name,r in responses.items()}}
    write_json(folder/'verification.json',report)
    print(json.dumps({'passed':True,'output':str(folder/'verification.json'),'measured_octave_cents':cents},ensure_ascii=True),flush=True)


if __name__=='__main__':main()
