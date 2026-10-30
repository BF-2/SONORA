"""Independent pitch analysis and target edits; no voice conversion dependency."""
from __future__ import annotations
import hashlib
import importlib.util
import json
from pathlib import Path
import numpy as np
from common import safe_torch, setup_applio, device_name, sha256, ADAPTER_VERSION


def contour_hash(data):
    fields={k:data.get(k) for k in ('sample_rate','hop','frame_center_offset','f0_hz','voiced')}
    return hashlib.sha256(json.dumps(fields,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def resample_voiced(f0, source_centers, target_centers):
    """Interpolate log Hz only within contiguous voiced runs. Silence stays silence."""
    values=np.asarray([0 if x is None else x for x in f0],dtype=np.float64)
    source_centers=np.asarray(source_centers,dtype=float)
    target_centers=np.asarray(target_centers,dtype=float)
    out=np.zeros(len(target_centers),dtype=np.float64)
    voiced=values>0
    edges=np.diff(np.r_[False,voiced,False].astype(int))
    for start,end in zip(np.flatnonzero(edges==1),np.flatnonzero(edges==-1)):
        if end-start==1:
            exact=np.isclose(target_centers,source_centers[start],atol=1e-7)
            out[exact]=values[start]
        else:
            mask=(target_centers>=source_centers[start])&(target_centers<=source_centers[end-1])
            out[mask]=np.exp(np.interp(target_centers[mask],source_centers[start:end],np.log(values[start:end])))
    return out


def apply_edits(data, edits, transpose=0):
    target=dict(data); f0=np.asarray([0 if x is None else x for x in data['f0_hz']],dtype=np.float64)
    centers=(np.arange(len(f0))*data['hop']+data.get('frame_center_offset',0))*48000/data['sample_rate']
    f0*=2**(float(transpose)/12)
    locked=np.zeros(len(f0),dtype=bool)
    # Normal edits first, locked edits last, so future analyses preserve locks.
    for edit in sorted(edits or [],key=lambda e:bool(e.get('locked',False))):
        region=edit.get('range_ticks',edit.get('range',[edit.get('start_tick',edit.get('start',0)),edit.get('end_tick',edit.get('end',0))]))
        if isinstance(region,dict): region=[region['start'],region['end']]
        if len(region)!=2 or region[1]<=region[0]: raise ValueError('Invalid pitch edit range')
        mask=(centers>=region[0])&(centers<region[1])
        snapshot=edit.get('snapshot')
        if edit.get('locked') and isinstance(snapshot,dict) and 'f0_hz' in snapshot:
            frozen_centers=(np.arange(len(snapshot['f0_hz']))*snapshot['hop']+snapshot.get('frame_center_offset',0))*48000/snapshot['sample_rate']
            frozen=resample_voiced(snapshot['f0_hz'],frozen_centers,centers)
            f0[mask]=frozen[mask]
            locked[mask]=True
            continue
        operation=edit.get('operation','transpose')
        params=edit.get('parameters',{})
        if operation in ('transpose','octave','cents'):
            shift=float(edit.get('semitones',params.get('semitones',edit.get('value',12 if operation=='octave' else 0))))
            if operation=='cents': shift/=100
            if abs(shift)>48: raise ValueError('Pitch edit exceeds 48 semitones')
            f0[mask]*=2**(shift/12)
        elif operation in ('set_hz','set','note'):
            value=edit.get('hz',params.get('hz',edit.get('value')))
            if operation=='note': value=440*2**((float(edit.get('pitch_midi_float',value))-69)/12)
            value=float(value)
            if not 20<=value<=2000: raise ValueError('Target F0 must be 20–2000 Hz')
            f0[mask & (f0>0) if edit.get('preserve_unvoiced') else mask]=value
        elif operation in ('unvoiced','clear'): f0[mask]=0
        elif operation in ('lock','restore'): pass
        else: raise ValueError('Unsupported pitch edit: '+str(operation))
        if edit.get('locked'): locked[mask]=True
    target['f0_hz']=[float(x) if x>0 else None for x in f0]
    target['voiced']=(f0>0).tolist()
    target['locked_regions']=[e for e in edits or [] if e.get('locked')]
    target['target_hash']=contour_hash(target)
    target['original_detection_hash']=data.get('target_hash') or contour_hash(data)
    return target


def analyze(input_path, config, parameters=None):
    import soundfile as sf
    import librosa
    torch=safe_torch()
    parameters=parameters or {}; algorithm=parameters.get('pitch_algorithm',parameters.get('algorithm','rmvpe'))
    if algorithm=='auto': algorithm='rmvpe'
    device=device_name(parameters)
    source, original_sr=sf.read(input_path,dtype='float32',always_2d=True)
    x=source.mean(axis=1)
    if original_sr!=16000: x=librosa.resample(x,orig_sr=original_sr,target_sr=16000,res_type='soxr_vhq')
    if len(x)<1024: x=np.pad(x,(0,1024-len(x)))
    hop=160; fmin=float(parameters.get('f0_min',50)); fmax=float(parameters.get('f0_max',1100))
    if not 20<=fmin<fmax<=2000: raise ValueError('Invalid pitch range')
    setup_applio(config)
    model_hash=None; score_kind='unavailable'; scores=None
    with torch.inference_mode():
        if algorithm=='rmvpe':
            from rvc.lib.predictors.RMVPE import RMVPE0Predictor
            path=Path(config['applio']['root'])/'rvc/models/predictors/rmvpe.pt'
            model=RMVPE0Predictor(str(path),device=device)
            audio=torch.from_numpy(x).float().to(device).unsqueeze(0)
            hidden=model.mel2hidden(model.mel_extractor(audio,center=True)).squeeze(0).cpu().numpy()
            threshold=float(parameters.get('threshold',.03))
            f0=model.decode(hidden,thred=threshold)
            scores=hidden.max(axis=1); score_kind='rmvpe_salience_max'
            model_hash=sha256(path)
        elif algorithm=='fcpe':
            from rvc.lib.predictors.f0 import FCPE
            f0=FCPE(device).get_f0(x,filter_radius=float(parameters.get('threshold',.006)))
            model_hash=sha256(Path(config['applio']['root'])/'rvc/models/predictors/fcpe.pt')
        elif algorithm in ('crepe','crepe-tiny','torchcrepe'):
            import torchcrepe
            model_name='tiny' if algorithm=='crepe-tiny' else 'full'
            f0,pd=torchcrepe.predict(torch.tensor(x,device=device).unsqueeze(0),16000,hop,fmin,fmax,
                                      model=model_name,batch_size=256,device=device,return_periodicity=True)
            f0=f0[0].cpu().numpy(); scores=pd[0].cpu().numpy(); score_kind='periodicity'
            f0[scores<float(parameters.get('threshold',.1))]=0
            weight_path=Path(torchcrepe.__file__).parent/'assets'/(model_name+'.pth')
            model_hash=sha256(weight_path)
        elif algorithm=='pyin':
            f0,voiced,scores=librosa.pyin(x,sr=16000,fmin=fmin,fmax=fmax,hop_length=hop,frame_length=1024,fill_na=0)
            f0[~voiced]=0; score_kind='voiced_probability'
        elif algorithm in ('harvest','dio'):
            import pyworld
            if algorithm=='harvest': f0,_=pyworld.harvest(x.astype(np.float64),16000,f0_floor=fmin,f0_ceil=fmax,frame_period=10)
            else:
                f0,t=pyworld.dio(x.astype(np.float64),16000,f0_floor=fmin,f0_ceil=fmax,frame_period=10)
                f0=pyworld.stonemask(x.astype(np.float64),f0,t,16000)
        else: raise ValueError('Unavailable pitch algorithm: '+algorithm)
    f0=np.asarray(f0,dtype=float).reshape(-1)
    # Independent deterministic silence gate. No interpolation over unvoiced frames.
    rms=librosa.feature.rms(y=x,frame_length=1024,hop_length=hop,center=True)[0]
    count=min(len(f0),len(rms)); f0=f0[:count]
    f0[(rms[:count]<10**(float(parameters.get('silence_db',-60))/20))|~np.isfinite(f0)|(f0<fmin)|(f0>fmax)]=0
    result={'schema_version':1,'sample_rate':16000,'sample_count':len(x),'hop':hop,'frame_center_offset':0,
            'f0_hz':[float(v) if v>0 else None for v in f0], 'voiced':(f0>0).tolist(),
            'native_score':[float(s) if np.isfinite(s) else None for s in scores[:count]] if scores is not None else [None]*count,
            'score_kind':score_kind,'calibration_id':None,'estimator':algorithm,'version':ADAPTER_VERSION,
            'source_hash':sha256(input_path),'model_hash':model_hash,'locked_regions':[],
            'time_map':{'source_rate':original_sr,'output_rate':16000,'source_offset':0,'output_offset':0}}
    result['target_hash']=contour_hash(result)
    return result
