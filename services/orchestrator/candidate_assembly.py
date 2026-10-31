"""Adopt a same-source version preview into a full converted base, preserving time."""
import numpy as np
import soundfile as sf
from . import media
from .config import TIMEBASE,binary
from .voices import covers_source

def assemble_preview(store,project,preview_id,base_id,folder,cancel,fade_ms=20):
    candidates={c['id']:c for c in project['candidates']}
    preview=candidates.get(preview_id);base=candidates.get(base_id)
    if not preview or not base or preview_id==base_id:raise ValueError('请选择短试听与另一个完整同源候选。')
    if preview['source_artifact_id']!=base['source_artifact_id'] or preview.get('voice_part')!=base.get('voice_part'):raise ValueError('局部采用必须使用同一输入和同一声部。')
    source=store.get('artifacts',base['source_artifact_id']);base_audio=store.get('artifacts',base['artifact_id']);preview_audio=store.get('artifacts',preview['artifact_id'])
    source_track=next((t for t in project['tracks'] if t['artifact_id']==source['id']),{})
    origin=source_track.get('offset_ticks',0)
    if not covers_source(base_audio,source,origin):raise ValueError('基础候选必须覆盖完整输入；先生成完整版本，再局部采用。')
    start=int(preview_audio.get('timeline_offset_ticks',0))-int(base_audio.get('timeline_offset_ticks',origin))
    duration=round(preview_audio['sample_count']*TIMEBASE/preview_audio['sample_rate'])
    length=round(base_audio['sample_count']*TIMEBASE/base_audio['sample_rate'])
    if start<0 or duration<=0 or start+duration>length:raise ValueError('试听范围超出基础候选。')
    fade=round(media.bounded(fade_ms,0,200)*48);fade=min(fade,duration//2)
    channels=base_audio['channels'];folder.mkdir(parents=True,exist_ok=True)
    for name,audio in [('base',base_audio),('preview',preview_audio)]:
        media.run([binary('ffmpeg'),'-v','error',*media.safe_input(media.validate_artifact(audio)),'-ar','48000','-ac',str(channels),'-c:a','pcm_f32le','-y',str(folder/(name+'.wav'))],cancel)
    output=folder/'adopted-preview.wav'
    with sf.SoundFile(folder/'base.wav') as original,sf.SoundFile(folder/'preview.wav') as patch,sf.SoundFile(output,'w',samplerate=48000,channels=channels,subtype='FLOAT') as result:
        for at in range(0,length,48000):
            if cancel.is_set():raise media.Cancelled()
            count=min(48000,length-at);original.seek(at);block=original.read(count,dtype='float32',always_2d=True)
            if len(block)!=count:raise ValueError('基础候选长度校验失败。')
            lo=max(at,start);hi=min(at+count,start+duration)
            if hi>lo:
                patch.seek(lo-start);part=patch.read(hi-lo,dtype='float32',always_2d=True)
                if len(part)!=hi-lo:raise ValueError('试听候选长度校验失败。')
                positions=np.arange(lo,hi)
                weight=np.ones(hi-lo) if not fade else np.minimum(np.minimum((positions-start)/fade,(start+duration-1-positions)/fade),1).clip(0,1)
                block[lo-at:hi-at]=block[lo-at:hi-at]*(1-weight[:,None])+part*weight[:,None]
            result.write(block)
    metadata={'base_candidate_id':base_id,'preview_candidate_id':preview_id,'source_artifact_id':source['id'],
              'range_ticks':[start+origin,start+origin+duration],'fade_ms':fade_ms,'outside_range':'full_converted_base',
              'preview_model_id':preview['model_id'],'preview_model_hash':preview['model_manifest_hash'],
              'version_bindings':[{'candidate_id':c['id'],'model_id':c['model_id'],'model_hash':c['model_manifest_hash'],'index_hash':c.get('index_hash')} for c in (base,preview)]}
    return output,base,preview,metadata,origin
