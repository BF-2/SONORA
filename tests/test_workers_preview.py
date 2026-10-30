"""Explicit real RVC contextual-excerpt contract test; preserves source lineage."""
from pathlib import Path
import json
import sys
import numpy as np
import soundfile as sf
from test_workers_real import run,OUT,ROOT


def main():
    if '--run-real' not in sys.argv: raise SystemExit('Pass --run-real to run local RVC model')
    source=OUT/'harmonic-vowel.wav';pitch=OUT/'pitch-baseline/pitch.json'
    params={'model_id':'twilight_sparkle_pony_singing_r18m_20260914','index_rate':0,'range_ticks':[43200,72000]}
    baseline=run('rvc','preview-baseline',source,params,pitch_path=str(pitch))
    shifted={**params,'range_ticks':[139200,168000],'source_timeline_offset_ticks':96000,
             'pitch_edits':[{'start_tick':139200,'end_tick':168000,'operation':'transpose','semitones':12,'locked':True}]}
    guide=run('rvc','preview-guide-offset',source,shifted,pitch_path=str(pitch))
    for response,timeline in [(baseline,43200),(guide,139200)]:
        artifact=response['outputs'][0];audio,sr=sf.read(artifact['path'])
        assert sr==40000 and len(audio)==24000
        assert artifact['source_offset']==39690 and artifact['source_sample_rate']==44100
        assert artifact['timeline_offset_ticks']==timeline
        assert artifact['preview_mode']=='contextual_excerpt_not_full_equivalence'
        assert artifact['context_sample_range']==[0,110250]
        assert response['metadata']['input_sample_count']==132300
    assert baseline['outputs'][0]['requested_f0_hash']!=guide['outputs'][0]['requested_f0_hash']
    assert baseline['outputs'][0]['consumed_f0_hash']!=guide['outputs'][0]['consumed_f0_hash']
    measured=run('pitch','preview-output-pitch',guide['outputs'][0]['path'],{'pitch_algorithm':'rmvpe'})
    curve=json.loads(Path(measured['outputs'][0]['path']).read_text(encoding='utf-8'))
    f0=np.array([f or 0 for f in curve['f0_hz']])[10:50];median=float(np.median(f0[f0>0]))
    assert 420<median<460,median
    report={'passed':True,'duration_samples':24000,'sample_rate':40000,'source_offset':39690,'source_sample_rate':44100,
            'baseline_timeline_offset_ticks':43200,'guide_timeline_offset_ticks':139200,'context_samples':[0,110250],
            'source_sample_count':132300,'guide_edited_output_f0_median':median,
            'preview_mode':'contextual_excerpt_not_full_equivalence','requested_f0_hash':guide['outputs'][0]['requested_f0_hash'],
            'consumed_f0_hash':guide['outputs'][0]['consumed_f0_hash']}
    (ROOT/'engine-envs/preview-verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))


if __name__=='__main__':main()
