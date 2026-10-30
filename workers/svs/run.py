"""DiffSinger ONNX: real lyric/phoneme + score synthesis, preserving fixed windows.

Compatible with the unmodified Nishiren Gard AI DiffSinger v2.0 bank.
Japanese dictionary kana and explicit native phonemes are the verified frontend.
"""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import json
import math
import hashlib
import gc
from common import worker_main,resource_config,read_json,write_json,sha256,audio_metadata,ADAPTER_VERSION


def probe():
    import onnxruntime as ort
    cfg=resource_config().get('svs',{})
    models=cfg.get('models',[])
    return {'id':'svs','name':'DiffSinger ONNX','protocol_version':1,'status':'ready' if models else 'missing_model',
            'onnxruntime':ort.__version__,'models':[{'id':m['id'],'name':m['name'],'test_status':m['test_status']} for m in models],
            'capabilities':{'svs':bool(models),'notes':True,'phoneme_timing':True,'languages':['ja'],
                            'manual_native_phonemes':True,'automatic_text_frontend':'Japanese dictionary kana per note',
                            'timing_policy':['locked_window'],'device':['cpu'],'cancel_mode':'terminate_process'}}


def validate_notes(params, dictionary, phoneme_ids):
    """Resolve dictionary text without inventing pronunciation or changing timings."""
    notes=params.get('notes') or []
    if not notes or len(notes)>512: raise ValueError('SVS requires 1–512 explicit notes')
    language=params.get('language','ja')
    if language!='ja': raise ValueError('Automatic lyric frontend currently verifies Japanese kana only; choose ja or use an external guide')
    duration=int(params.get('duration_ticks',max(n.get('end_tick',n.get('end',0)) for n in notes)))
    if not 4800<=duration<=48000*60: raise ValueError('Render a fixed SVS phrase between 0.1 and 60 seconds')
    if params.get('timing_policy','locked_window')!='locked_window': raise ValueError('SVS only supports locked_window; ripple requires explicit full-track orchestration')
    result=[]; last=0
    lyrics=params.get('lyrics',[])
    for i,note in enumerate(sorted(notes,key=lambda n:n.get('start_tick',n.get('start',0)))):
        start=int(note.get('start_tick',note.get('start',0))); end=int(note.get('end_tick',note.get('end',0)))
        if start<last or end<=start or end>duration: raise ValueError(f'Note {i+1} overlaps or exceeds fixed window')
        midi=float(note.get('pitch_midi_float',note.get('midi',60)))
        if not 36<=midi<=84: raise ValueError(f'Note {i+1} outside this adapter tested range MIDI 36–84')
        lyric=str(note.get('lyric',note.get('text','')))
        if not lyric:
            unit=next((u for u in lyrics if note.get('id') in u.get('note_ids',[]) or u.get('start_tick')==start),None)
            if unit: lyric=str(unit.get('new_text',unit.get('text','')))
        continuation=bool(note.get('slur',False) or lyric in ('+','ー'))
        if continuation:
            if not result or start!=last:
                raise ValueError('A slur must directly follow an existing voiced note')
            phones=[result[-1]['phonemes'][-1]]
            if phones[0] in ('SP','AP'): raise ValueError('Cannot slur a rest or breath')
        else:
            phones=note.get('phonemes')
        if isinstance(phones,str): phones=phones.split()
        if not phones:
            phones=dictionary.get(lyric)
            if not phones and lyric:
                # Greedy dictionary match supports ordinary multi-mora kana words.
                phones=[]; text=lyric
                while text:
                    match=next((text[:length] for length in range(min(3,len(text)),0,-1) if text[:length] in dictionary
                                and all(p.startswith('ja/') for p in dictionary[text[:length]])),None)
                    if not match: raise ValueError(f'Cannot pronounce lyric at note {i+1}: {lyric}; supply Japanese kana or explicit native phonemes')
                    phones.extend(dictionary[match]); text=text[len(match):]
        if not phones or not isinstance(phones,list) or len(phones)>24: raise ValueError(f'Note {i+1} needs valid lyric/phonemes')
        for phone in phones:
            if phone not in phoneme_ids: raise ValueError(f'Unsupported phoneme {phone} in note {i+1}')
            if phone not in ('SP','AP') and not phone.startswith('ja/'):
                raise ValueError('This verified frontend accepts ja/ phonemes only')
        result.append({'id':str(note.get('id',i)), 'start_tick':start,'end_tick':end,'midi':midi,'lyric':lyric,'phonemes':phones,'slur':continuation})
        last=end
    return result,duration


def allocate_duration(predicted, frames, minimum=3):
    import numpy as np
    if frames<len(predicted)*minimum:
        raise ValueError(f'Timing conflict: {len(predicted)} phonemes need at least {len(predicted)*minimum} frames, window has {frames}')
    weights=np.maximum(np.asarray(predicted,dtype=float),.01)
    free=frames-len(weights)*minimum
    scaled=weights/weights.sum()*free
    durations=np.floor(scaled).astype(np.int64)+minimum
    remaining=frames-int(durations.sum())
    for index in np.argsort(-(scaled-np.floor(scaled)))[:remaining]: durations[index]+=1
    return durations


def render(request):
    import numpy as np
    import soundfile as sf
    import onnxruntime as ort
    import yaml
    params=request.get('parameters',{}); cfg=resource_config(request).get('svs',{})
    selected=params.get('svs_model_id',cfg.get('default_model'))
    manifest=next((m for m in cfg.get('models',[]) if m['id']==selected),None)
    if not manifest: raise ValueError('No compatible registered DiffSinger voicebank')
    root=Path(manifest['root'])
    # A bank identity includes acoustic, vocoder, dictionary, predictors and speaker embeddings.
    for relative,entry in manifest['components'].items():
        if sha256(root/relative)!=entry['sha256']: raise ValueError('Voicebank component changed: '+relative)
    phonemes=read_json(root/'dsmain/phonemes.json')
    dictionary_data=yaml.safe_load((root/'dsmain/dsdict.yaml').read_text(encoding='utf-8'))
    dictionary={str(e['grapheme']):e['phonemes'] for e in dictionary_data['entries']}
    notes,duration_ticks=validate_notes(params,dictionary,phonemes)
    steps=int(params.get('steps',10))
    if not 5<=steps<=30: raise ValueError('DiffSinger steps must be 5–30')
    speaker=params.get('speaker','Standard')
    if speaker not in ('Standard','Sweet','Soft','Power','Emotional','2P'): raise ValueError('Unknown voicebank speaker style')
    if params.get('device','cpu') not in ('cpu','auto'): raise ValueError('This DiffSinger adapter currently validates CPU ONNX Runtime only')
    sr=44100; hop=512; head=20; tail=20
    duration_samples=(duration_ticks*sr+24000)//48000
    valid_frames=math.ceil(duration_samples/hop)
    def tick_frame(tick): return (tick*sr+24000*hop)//(48000*hop)
    # Word list contains explicit silent gaps. Source anchors never move.
    words=[{'phonemes':['SP'],'frames':head,'midi':0,'kind':'head'}];cursor=0
    for note in notes:
        start=tick_frame(note['start_tick']);end=tick_frame(note['end_tick'])
        if start>cursor: words.append({'phonemes':['SP'],'frames':start-cursor,'midi':0,'kind':'rest'})
        if end<=start: raise ValueError('Note shorter than a model frame')
        if note['slur'] and words[-1]['kind']=='note':
            words[-1]['frames']+=end-start
            words[-1]['midi_segments'].append([note['midi'],end-start])
        else:
            words.append({'phonemes':note['phonemes'],'frames':end-start,'midi':note['midi'],'kind':'note','note_id':note['id'],
                          'midi_segments':[[note['midi'],end-start]]})
        cursor=end
    if valid_frames>cursor: words.append({'phonemes':['SP'],'frames':valid_frames-cursor,'midi':0,'kind':'rest'})
    words.append({'phonemes':['SP'],'frames':tail,'midi':0,'kind':'tail'})
    phone_sequence=[p for word in words for p in word['phonemes']]
    ort.set_seed(int(params.get('seed',0)))
    options=ort.SessionOptions();options.intra_op_num_threads=4;options.inter_op_num_threads=1
    options.graph_optimization_level=ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    def session(relative):
        result=ort.InferenceSession(str(root/relative),sess_options=options,providers=['CPUExecutionProvider'])
        return result
    def text_tensors(directory):
        tokens=read_json(root/directory/'phonemes.json');langs=read_json(root/directory/'languages.json')
        return {'tokens':np.array([[tokens[p] for p in phone_sequence]],dtype=np.int64),
                'languages':np.array([[langs.get(p.split('/')[0],0) if '/' in p else 0 for p in phone_sequence]],dtype=np.int64)}
    def embedding(directory,count):
        value=np.fromfile(root/directory/(speaker+'.emb'),dtype='<f4')
        if value.shape!=(384,): raise ValueError('Speaker embedding shape mismatch')
        return np.tile(value[None,None,:],(1,count,1))
    print('SVS duration predictor',flush=True)
    inputs=text_tensors('dsdur')
    inputs['word_div']=np.array([[len(w['phonemes']) for w in words]],dtype=np.int64)
    inputs['word_dur']=np.array([[w['frames'] for w in words]],dtype=np.int64)
    linguistic=session('dsdur/linguistic.onnx'); encoded=linguistic.run(None,inputs);del linguistic
    dur_model=session('dsdur/dur.onnx')
    predicted=dur_model.run(None,{'encoder_out':encoded[0],'x_masks':encoded[1],
                                  'ph_midi':np.array([[int(round(w['midi'])) for w in words for _p in w['phonemes']]],dtype=np.int64),
                                  'spk_embed':embedding('dsdur',len(phone_sequence))})[0][0]
    del dur_model;gc.collect()
    durations=[];offset=0;midi_curve=[]
    for word in words:
        count=len(word['phonemes'])
        allocated=allocate_duration(predicted[offset:offset+count],word['frames'],minimum=3 if word['kind']=='note' else 0)
        durations.extend(allocated.tolist())
        for pitch,frame_count in word.get('midi_segments',[[word['midi'],word['frames']]]): midi_curve.extend([pitch]*frame_count)
        offset+=count
    ph_dur=np.array([durations],dtype=np.int64);n_frames=sum(durations)
    midi_curve=np.array([midi_curve],dtype=np.float32)
    f0=np.where(midi_curve>0,440*2**((midi_curve-69)/12),0).astype(np.float32)
    print('SVS variance predictor',flush=True)
    variance_text=text_tensors('dsvariance');variance_text['ph_dur']=ph_dur
    linguistic=session('dsvariance/linguistic.onnx');encoder=linguistic.run(None,variance_text)[0];del linguistic
    zeros=np.zeros((1,n_frames),dtype=np.float32)
    variance_inputs={'encoder_out':encoder,'ph_dur':ph_dur,'pitch':midi_curve,'breathiness':zeros,'voicing':zeros.copy(),
                     'tension':zeros.copy(),'retake':np.ones((1,n_frames,3),dtype=bool),'spk_embed':embedding('dsvariance',n_frames),
                     'steps':np.array(steps,dtype=np.int64)}
    variance_model=session('dsvariance/variance.onnx');variance=variance_model.run(None,variance_inputs);del variance_model,encoder;gc.collect()
    print('SVS acoustic model',flush=True)
    acoustic_inputs=text_tensors('dsmain')
    acoustic_inputs.update(durations=ph_dur,f0=f0,breathiness=np.clip(variance[0],-96,0),voicing=np.clip(variance[1],-96,0),
                           tension=variance[2],gender=zeros.copy(),velocity=np.ones_like(zeros),
                           spk_embed=embedding('dsmain',n_frames),steps=np.array(steps,dtype=np.int64))
    acoustic=session('dsmain/acoustic.onnx');mel=acoustic.run(None,acoustic_inputs)[0];del acoustic;gc.collect()
    print('SVS vocoder',flush=True)
    vocoder=session('dsvocoder/gda_pc-hifigan.onnx');wave=vocoder.run(None,{'mel':mel,'f0':f0})[0].reshape(-1);del vocoder
    if not np.isfinite(wave).all(): raise ValueError('Nonfinite SVS output')
    wave=wave[head*hop:head*hop+duration_samples]
    if len(wave)!=duration_samples or np.max(np.abs(wave))<1e-5: raise ValueError('SVS output is empty or has incorrect duration')
    output=Path(request['output_dir'])/'synthesized-vocal.wav';sf.write(output,wave,sr,subtype='FLOAT')
    conditions={'notes':notes,'phonemes':phone_sequence,'durations':durations,'words':words,'duration_ticks':duration_ticks,
                'sample_rate':sr,'hop':hop,'head_frames':head,'tail_frames':tail,'steps':steps,'speaker':speaker,'seed':int(params.get('seed',0))}
    write_json(Path(request['output_dir'])/'synthesis-conditions.json',conditions)
    conditions_hash=hashlib.sha256(json.dumps(conditions,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'outputs':[audio_metadata(output,'synthesized','diffsinger-onnx',model_hash=manifest['model_hash'],
                                      generation_condition_hash=conditions_hash,source_offset=0)],
            'metadata':{'engine_version':ADAPTER_VERSION,'onnxruntime':ort.__version__,'provider':'CPUExecutionProvider',
                        'model_id':manifest['id'],'model_name':manifest['name'],'language':'ja','timing_policy':'locked_window',
                        'duration_ticks':duration_ticks,'generation_condition_hash':conditions_hash,'condition_summary':conditions,
                        'attribution':'Nishiren Gard AI DiffSinger 2.0 / Gardanana','license':manifest['license']},
            'warnings':['本适配器已验证日语假名/显式音素。歌词可懂度与自然度需要试听确认；并非自动歌词识别验收。']}


if __name__=='__main__': raise SystemExit(worker_main('svs',render,probe))
