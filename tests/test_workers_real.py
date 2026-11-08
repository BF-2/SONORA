"""Real engine integration and external-F0 response test. Explicit opt-in CLI.

python tests/test_workers_real.py --run-real (uses existing local weights, GPU sequentially).
Outputs stay in ignored tests/results/workers. Technical signal is not listening-quality evidence.
"""
from pathlib import Path
import json
import os
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'tests/results/workers'


def run(engine, name, input_path, params=None, **extra):
    config=json.loads((ROOT/'engine-envs/resources.json').read_text(encoding='utf-8'))
    directory=OUT/name; directory.mkdir(parents=True,exist_ok=True)
    request={'protocol_version':1,'job_id':name,'input_path':str(input_path),'output_dir':str(directory),
             'resource_config_path':str(ROOT/'engine-envs/resources.json'),'parameters':params or {},**extra}
    path=directory/'request.json'; path.write_text(json.dumps(request),encoding='utf-8')
    python=config.get('pitch',config['applio'])['python'] if engine=='pitch' else config['separation' if engine=='separation' else 'applio']['python']
    with (directory/'worker.log').open('w',encoding='utf-8') as log:
        result=subprocess.run([python,str(ROOT/'workers'/engine/'run.py'),'--request',str(path),'--response',str(directory/'response.json')],
                              env={**os.environ,'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'},stdout=log,stderr=subprocess.STDOUT)
    response=json.loads((directory/'response.json').read_text(encoding='utf-8'))
    print(name,result.returncode,response.get('metadata',{}).get('elapsed_seconds'),response.get('error'),flush=True)
    if not response['ok']: raise AssertionError(response)
    return response


def main():
    if '--run-real' not in sys.argv: raise SystemExit('Pass --run-real to explicitly execute model tests')
    import numpy as np
    import soundfile as sf
    OUT.mkdir(parents=True,exist_ok=True)
    # A 3-second sustained, harmonic vowel with 0.2-second silence boundaries.
    sr=44100; t=np.arange(sr*3)/sr
    envelope=np.minimum(np.minimum(t/.1,(3-t)/.1),1).clip(0,1)
    envelope[(t<.2)|(t>2.8)]=0
    vowel=sum(np.sin(2*np.pi*220*k*t)/k for k in range(1,9))*.13*envelope
    source=OUT/'harmonic-vowel.wav'; sf.write(source,vowel,sr,subtype='FLOAT')
    pitch=run('pitch','pitch-baseline',source,{'pitch_algorithm':'rmvpe'})
    a=run('rvc','f0-baseline',source,{'model_id':'haysay_twilight_sparkle','index_rate':0,'transpose':0},pitch_path=pitch['outputs'][0]['path'])
    b=run('rvc','f0-octave',source,{'model_id':'haysay_twilight_sparkle','index_rate':0,'transpose':12},pitch_path=pitch['outputs'][0]['path'])
    analyzed_a=run('pitch','output-baseline',a['outputs'][0]['path'],{'pitch_algorithm':'rmvpe'})
    analyzed_b=run('pitch','output-octave',b['outputs'][0]['path'],{'pitch_algorithm':'rmvpe'})
    def median(response):
        data=json.loads(Path(response['outputs'][0]['path']).read_text())
        f=np.array([x or 0 for x in data['f0_hz']])[50:250]
        return float(np.median(f[f>0]))
    baseline,octave=median(analyzed_a),median(analyzed_b)
    response_cents=1200*np.log2(octave/baseline)
    assert 950<response_cents<1450,(baseline,octave,response_cents)
    assert a['outputs'][0]['requested_f0_hash']!=b['outputs'][0]['requested_f0_hash']
    assert a['outputs'][0]['consumed_f0_hash']!=b['outputs'][0]['consumed_f0_hash']
    assert a['metadata']['generator_conditions']!=b['metadata']['generator_conditions']
    separated=run('separation','separation-demo',ROOT.parent/'demo/audio/original_demo_song.wav')
    report={'external_f0_verified':True,'baseline_f0_hz':baseline,'octave_f0_hz':octave,'response_cents':float(response_cents),
            'requested_consumed_hashes_a':[a['outputs'][0]['requested_f0_hash'],a['outputs'][0]['consumed_f0_hash']],
            'requested_consumed_hashes_b':[b['outputs'][0]['requested_f0_hash'],b['outputs'][0]['consumed_f0_hash']],
            'separation_outputs':separated['outputs'],'fixture':'synthetic technical signal, not subjective vocal quality acceptance'}
    (OUT/'verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__=='__main__': main()
