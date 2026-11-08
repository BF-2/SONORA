"""Fixed-window lyric/score synthesis contracts, plus opt-in real ONNX test."""
from pathlib import Path
import json
import os
import subprocess
import sys
import unittest
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'workers'))
from svs.run import validate_notes,allocate_duration


class SVSContracts(unittest.TestCase):
    def test_duration_allocation_keeps_anchor(self):
        result=allocate_duration([1,7,1],61)
        self.assertEqual(int(result.sum()),61)
        self.assertTrue((result>=3).all())

    def test_too_many_phonemes_never_squeezed_silently(self):
        with self.assertRaises(ValueError): allocate_duration([1,1,1],5)

    def test_unsupported_language_reports_precisely(self):
        params={'notes':[{'start_tick':0,'end_tick':48000,'lyric':'hello'}],'language':'en'}
        with self.assertRaisesRegex(ValueError,'Japanese'): validate_notes(params,{}, {})

    def test_nonoverlap_and_window(self):
        params={'notes':[{'start_tick':0,'end_tick':48000,'lyric':'あ'},{'start_tick':40000,'end_tick':60000,'lyric':'あ'}],'language':'ja'}
        with self.assertRaisesRegex(ValueError,'overlaps'): validate_notes(params,{'あ':['ja/a']},{'ja/a':7})

    def test_slur_has_no_new_consonant(self):
        params={'notes':[{'start_tick':0,'end_tick':24000,'lyric':'か'},{'start_tick':24000,'end_tick':48000,'lyric':'+','pitch_midi_float':64}],'language':'ja'}
        notes,duration=validate_notes(params,{'か':['ja/k','ja/a']},{'ja/k':50,'ja/a':7})
        self.assertTrue(notes[1]['slur'])
        self.assertEqual(notes[1]['phonemes'],['ja/a'])


def real_test():
    import soundfile as sf
    config=json.loads((ROOT/'engine-envs/resources.json').read_text(encoding='utf-8'))
    outputs=[];all_responses=[]
    for name,text in [('svs-kirakira','きらきら'),('svs-harukana','はるかな')]:
        directory=ROOT/'tests/results/workers'/name;directory.mkdir(parents=True,exist_ok=True)
        params={'svs_model_id':'nishiren-diffsinger-v2','language':'ja','duration_ticks':144000,'steps':10,'timing_policy':'locked_window',
                'notes':[{'id':str(i),'start_tick':14400+i*28800,'end_tick':14400+(i+1)*28800,'pitch_midi_float':pitch,'lyric':lyric}
                         for i,(lyric,pitch) in enumerate(zip(text,[60,60,67,67]))]}
        request={'protocol_version':1,'job_id':name,'output_dir':str(directory),'parameters':params,'resource_config_path':str(ROOT/'engine-envs/resources.json')}
        (directory/'request.json').write_text(json.dumps(request,ensure_ascii=False),encoding='utf-8')
        with (directory/'worker.log').open('w',encoding='utf-8') as log:
            job=subprocess.run([config['svs']['python'],str(ROOT/'workers/svs/run.py'),'--request',str(directory/'request.json'),'--response',str(directory/'response.json')],
                               stdout=log,stderr=subprocess.STDOUT,env={**os.environ,'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'})
        response=json.loads((directory/'response.json').read_text(encoding='utf-8'))
        assert job.returncode==0 and response['ok'],response
        samples,sr=sf.read(response['outputs'][0]['path'])
        assert sr==44100 and len(samples)==132300 and np.isfinite(samples).all()
        assert np.max(np.abs(samples))>1e-4
        outputs.append(samples);all_responses.append(response)
        print(name,response['metadata']['elapsed_seconds'],float(np.max(np.abs(samples))),flush=True)
    assert all_responses[0]['metadata']['generation_condition_hash']!=all_responses[1]['metadata']['generation_condition_hash']
    rms_difference=float(np.sqrt(np.mean((outputs[0]-outputs[1])**2)))
    assert rms_difference>.005
    report={'engine':'diffsinger-onnx','model':'Nishiren Gard AI DiffSinger 2.0','provider':'CPUExecutionProvider','sample_rate':44100,
            'sample_count':132300,'lyrics':['きらきら','はるかな'],'notes_midi':[60,60,67,67],'rms_difference':rms_difference,
            'condition_hashes':[r['metadata']['generation_condition_hash'] for r in all_responses],
            'elapsed_seconds':[r['metadata']['elapsed_seconds'] for r in all_responses],
            'scope':'Real duration+variance+acoustic+vocoder synthesis; exact fixed length and changed phoneme conditioning; intelligibility requires listening'}
    (ROOT/'engine-envs/svs-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    if '--run-real' in sys.argv: real_test()
    else: unittest.main()
