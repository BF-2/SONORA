"""Target pitch/notes and bounded multi-candidate composition on integer timelines."""
import copy
import json
import math
from pathlib import Path
import sys
import numpy as np
import soundfile as sf
from . import media
from .config import ROOT,TIMEBASE,DATA,binary

# Pure numerical editor shared with worker; importing does not load a model.
sys.path.insert(0,str(ROOT/'workers'))
from pitch.engine import apply_edits,apply_target_parameters,shift_pitch_to_project

def note_edits(notes):
    result=[]
    for note in notes:
        if note.get('reference_source') not in ('manual','midi'): continue
        pitch=media.bounded(note['pitch_midi_float'],24,108)
        result.append({'id':'note-'+note['id'],'start_tick':int(note['start']),'end_tick':int(note['end']),'operation':'set_hz','hz':440*2**((pitch-69)/12),'locked':False,'preserve_unvoiced':True})
    return result

def freeze_locks(project,patch,store,source_id=None):
    edits=copy.deepcopy(patch.get('pitch_edits',project['pitch_edits']))
    if not any(e.get('locked') and not e.get('snapshot') for e in edits): return
    pitch=next((a for a in reversed(project['assets']) if a.get('stem_role')=='pitch' and (source_id is None and not a.get('voice_part') or source_id in a.get('input_artifact_ids',[]))),None)
    if not pitch: return
    data=json.loads(media.validate_artifact(pitch).read_text(encoding='utf-8'))
    data=shift_pitch_to_project(data,pitch.get('timeline_offset_ticks',0))
    params=patch.get('parameters',project['parameters'])
    target=apply_target_parameters(data,{**params,'pitch_edits':note_edits(patch.get('notes',project['notes']))+edits})
    for edit in edits:
        if not edit.get('locked') or edit.get('snapshot'): continue
        first=max(0,math.ceil((edit['start_tick']*data['sample_rate']/TIMEBASE-data['frame_center_offset'])/data['hop']))
        end=min(len(target['f0_hz']),math.ceil((edit['end_tick']*data['sample_rate']/TIMEBASE-data['frame_center_offset'])/data['hop']))
        if end>first:
            edit['snapshot']={'sample_rate':data['sample_rate'],'hop':data['hop'],'frame_center_offset':data['frame_center_offset']+first*data['hop'],'f0_hz':target['f0_hz'][first:end]}
    patch['pitch_edits']=edits

def compose(store,candidates,base_id,segments,folder,cancel):
    by_id={c['id']:c for c in candidates};base=by_id.get(base_id)
    if not base: raise ValueError('请选择拼选的基础候选。')
    if not segments or len(segments)>128: raise ValueError('请提供 1–128 个拼选区间。')
    ordered=sorted(segments,key=lambda x:x['start_tick']);previous=0
    base_artifact=store.get('artifacts',base['artifact_id']);length=round(base_artifact['sample_count']*TIMEBASE/base_artifact['sample_rate'])
    sources={base_id:base};seams=[]
    for seg in ordered:
        c=by_id.get(seg['candidate_id'])
        if not c or c['source_artifact_id']!=base['source_artifact_id']: raise ValueError('拼选候选必须来自同一不可变源。')
        start,end=seg['start_tick'],seg['end_tick']
        if not isinstance(start,int) or not isinstance(end,int) or start<previous or end<=start or end>length: raise ValueError('拼选范围必须在源时长内且不得互相重叠。')
        a=store.get('artifacts',c['artifact_id'])
        if abs(round(a['sample_count']*TIMEBASE/a['sample_rate'])-length)>1: raise ValueError('候选时间轴长度不同，请先对齐后拼选。')
        sources[c['id']]=c;previous=end;media.bounded(seg.get('fade_ms',20),1,200)
        seams.extend([start,end])
    resampled={}
    for index,(identifier,c) in enumerate(sources.items()):
        # IDs from portable recipes are metadata, never filesystem components.
        a=store.get('artifacts',c['artifact_id']);path=folder/f'comp-input-{index}.wav'
        media.run([binary('ffmpeg'),'-v','error',*media.safe_input(media.validate_artifact(a)),'-ar','48000','-ac','2','-c:a','pcm_f32le','-y',str(path)],cancel)
        resampled[identifier]=path
    output=folder/'comp.wav'
    # Block-wise source selection keeps memory bounded for full-length sessions.
    handles={key:sf.SoundFile(path) for key,path in resampled.items()}
    try:
        with sf.SoundFile(output,'w',samplerate=48000,channels=2,subtype='FLOAT') as dest:
            for start in range(0,length,48000):
                if cancel.is_set(): raise media.Cancelled()
                count=min(48000,length-start);base_file=handles[base_id];base_file.seek(start);block=base_file.read(count,dtype='float32',always_2d=True)
                if len(block)<count: block=np.pad(block,((0,count-len(block)),(0,0)))
                positions=np.arange(start,start+count)
                for seg in ordered:
                    lo=max(start,seg['start_tick']);hi=min(start+count,seg['end_tick'])
                    if lo>=hi: continue
                    f=handles[seg['candidate_id']];f.seek(lo);chunk=f.read(hi-lo,dtype='float32',always_2d=True)
                    if len(chunk)!=hi-lo: raise ValueError('候选采样不足，拒绝提交半截拼选。')
                    fade=min(round(seg.get('fade_ms',20)*48),(seg['end_tick']-seg['start_tick'])//2)
                    pos=positions[lo-start:hi-start]
                    weight=np.minimum(np.minimum((pos-seg['start_tick'])/max(fade,1),(seg['end_tick']-1-pos)/max(fade,1)),1).clip(0,1)
                    # Linear crossfade avoids +3 dB boost for correlated same-source voices.
                    block[lo-start:hi-start]=block[lo-start:hi-start]*(1-weight[:,None])+chunk*weight[:,None]
                dest.write(block)
    finally:
        for f in handles.values(): f.close()
    return output,[c['artifact_id'] for c in sources.values()],seams
