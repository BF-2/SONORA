from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import numpy as np
import soundfile as sf
from .config import DATA, TIMEBASE, binary
from .store import now, uid

MEDIA_EXTENSIONS = {'.wav','.wave','.mp3','.flac','.ogg','.opus','.m4a','.mp4','.aac','.aif','.aiff','.wma','.webm','.mkv'}
FORMAT_ALLOW = 'wav,mp3,flac,ogg,mov,aac,aiff,asf,matroska,webm'
NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

class Cancelled(Exception): pass

def sha256(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda:f.read(4*1024*1024),b''): h.update(block)
    return h.hexdigest()

def hash_json(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()

def run(args, cancel=None, log=None, timeout=7200, env=None, cwd=None):
    target = Path(log or DATA / 'jobs' / (uid('process')+'.log'))
    with target.open('wb') as out:
        proc=subprocess.Popen([str(v) for v in args],stdout=out,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,creationflags=NO_WINDOW,env=env,cwd=cwd)
        start=time.monotonic()
        while proc.poll() is None:
            if (cancel and cancel.is_set()) or time.monotonic()-start>timeout:
                if os.name=='nt': subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True,creationflags=NO_WINDOW)
                else: proc.kill()
                proc.wait(timeout=20)
                if cancel and cancel.is_set(): raise Cancelled('任务已取消，已提交的产物仍保留。')
                raise RuntimeError('处理超时，请减小选区或更换执行器。')
            time.sleep(0.08)
    if proc.returncode:
        detail=target.read_text(encoding='utf-8',errors='replace')[-6000:]
        raise RuntimeError(f'执行器失败 (exit {proc.returncode})：{detail}')
    return target

def safe_input(path):
    p=Path(path)
    if p.suffix.lower() not in MEDIA_EXTENSIONS: raise ValueError('不支持此媒体格式。播放列表与远程引用不允许导入。')
    return ['-protocol_whitelist','file,pipe','-format_whitelist',FORMAT_ALLOW,'-i',str(p)]

def probe(path):
    result=subprocess.run([binary('ffprobe'),'-v','error','-protocol_whitelist','file,pipe','-format_whitelist',FORMAT_ALLOW,'-show_streams','-show_format','-of','json',str(path)],capture_output=True,timeout=30,creationflags=NO_WINDOW)
    if result.returncode: raise ValueError('音频探测失败：'+result.stderr.decode('utf-8',errors='replace')[-1000:])
    info=json.loads(result.stdout)
    stream=next((s for s in info.get('streams',[]) if s['codec_type']=='audio'),None)
    if not stream: raise ValueError('文件没有可解码的音频流。')
    if int(stream.get('channels',0))>8: raise ValueError('最多支持 8 声道素材，请先导出需要的声道。')
    return {'sample_rate':int(stream['sample_rate']),'channels':int(stream['channels']),'channel_layout':stream.get('channel_layout','unknown'),'codec':stream['codec_name'],'duration':float(stream.get('duration') or info.get('format',{}).get('duration') or 0),'file_size':Path(path).stat().st_size,'container':info.get('format',{}).get('format_name')}

def decode(path,output,cancel=None):
    probe(path)
    run([binary('ffmpeg'),'-v','error','-nostdin',*safe_input(path),'-map','0:a:0','-vn','-c:a','pcm_f32le','-y',str(output)],cancel)

def analyze(path):
    with sf.SoundFile(path) as audio:
        count=len(audio); sr=audio.samplerate; channels=audio.channels
        bins=min(2048,max(1,count)); width=max(1,math.ceil(count/bins))
        peaks=[]; squares=0.; clipped=0; peak=0.; silence=[]; silence_start=None; offset=0
        while True:
            block=audio.read(width,dtype='float32',always_2d=True)
            if len(block)==0: break
            if not np.isfinite(block).all(): raise ValueError('输出包含非有限采样，拒绝提交。')
            mono=block.mean(axis=1); lo=float(mono.min()); hi=float(mono.max()); peaks.append([round(lo,5),round(hi,5)])
            absolute=np.abs(block); peak=max(peak,float(absolute.max())); squares+=float(np.square(block.astype(np.float64)).sum()); clipped+=int(np.count_nonzero(absolute>=0.999969))
            quiet=float(np.max(np.abs(mono)))<0.001
            if quiet and silence_start is None: silence_start=offset
            if not quiet and silence_start is not None:
                if offset-silence_start>sr*.2: silence.append({'start_tick':round(silence_start*TIMEBASE/sr),'end_tick':round(offset*TIMEBASE/sr)})
                silence_start=None
            offset+=len(block)
        if silence_start is not None and count-silence_start>sr*.2: silence.append({'start_tick':round(silence_start*TIMEBASE/sr),'end_tick':round(count*TIMEBASE/sr)})
    rms=math.sqrt(squares/max(1,count*channels))
    return {'sample_rate':sr,'sample_count':count,'channels':channels,'peaks':peaks,'levels':[], 'diagnostics':{'sample_peak_dbfs':20*math.log10(max(peak,1e-12)),'rms_dbfs':20*math.log10(max(rms,1e-12)),'clipping_samples':clipped,'silence_regions':silence,'measurement_kind':'sample_peak_and_rms','true_peak_dbfs':None,'integrated_lufs':None}}

def commit(store,path,*,kind,inputs=None,job=None,worker='ffmpeg',engine='ffmpeg',model_hash=None,role='audio',name=None,extra=None):
    p=Path(path); digest=sha256(p); ext=p.suffix.lower(); destination=DATA/'cache'/digest[:2]/(digest+ext)
    destination.parent.mkdir(parents=True,exist_ok=True)
    meta={}
    if ext in ('.wav','.flac'):
        wave=analyze(p)
        if wave['sample_count']==0: raise ValueError('产物没有有效采样。')
        meta={k:wave[k] for k in ('sample_rate','sample_count','channels','diagnostics')}
    elif ext=='.json':
        data=json.loads(p.read_text(encoding='utf-8'))
        meta={'sample_rate':data.get('sample_rate',16000),'sample_count':data.get('sample_count',len(data.get('f0_hz',[]))*data.get('hop',160)),'channels':1,'frame_hop':data.get('hop',160),'frame_center_offset':data.get('frame_center_offset',0)}
    else: raise ValueError('中间产物必须是无损音频或 JSON。')
    if not destination.exists(): os.replace(p,destination)
    elif sha256(destination)!=digest: raise ValueError('缓存校验失败。')
    if ext in ('.wav','.flac'):
        wavepath=destination.with_suffix('.waveform.json')
        tmp=wavepath.with_suffix('.'+uid('write')+'.tmp')
        tmp.write_text(json.dumps(wave,separators=(',',':')),encoding='utf-8'); os.replace(tmp,wavepath)
    artifact={'id':uid('artifact'),'sha256':digest,'checksum':digest,'path':str(destination),'name':name or p.name,'media_type':'application/json' if ext=='.json' else 'audio/wav','schema_version':1,'revision':1,'created_at':now(),'updated_at':now(),'processing_kind':kind,'input_artifact_ids':inputs or [],'job_id':job['id'] if job else None,'worker':worker,'engine':engine,'model_hash':model_hash,'stem_role':role,'commit_state':'committed','source_offset':0,'frame_hop':0,'frame_center_offset':0,'algorithm_latency':None,'codec_priming':None,'manual_alignment_offset':0,**meta}
    artifact['valid_range']=[0,artifact['sample_count']]
    artifact['time_map']={'source_rate':artifact['sample_rate'],'output_rate':artifact['sample_rate'],'source_offset':0,'output_offset':0}
    if extra: artifact.update(extra)
    if kind=='model_inference' and (not job or not model_hash): raise ValueError('模型产物缺少推理来源。')
    return store.put('artifacts',artifact)

def validate_artifact(artifact):
    p=Path(artifact['path']).resolve()
    if not p.is_relative_to((DATA/'cache').resolve()) or artifact['commit_state']!='committed' or not p.is_file() or sha256(p)!=artifact['sha256']:
        raise ValueError('素材缺失或哈希已变化，请重新链接/重新运行该节点。')
    return p

def import_audio(store,path,role='original'):
    p=Path(path)
    safe_input(p)
    source_hash=sha256(p); metadata=probe(p)
    source=DATA/'cache'/source_hash[:2]/(source_hash+'.source'+p.suffix.lower()); source.parent.mkdir(parents=True,exist_ok=True)
    if not source.exists():
        try: os.link(p,source)
        except OSError:
            import shutil
            shutil.copyfile(p,source)
    if sha256(source)!=source_hash: raise ValueError('源文件在导入时发生变化。')
    decoded=DATA/'uploads'/(uid('decode')+'.wav'); decode(source,decoded)
    return commit(store,decoded,kind='decode',role=role,name=p.name,extra={'original_uri':str(p),'original_hash':source_hash,'original_snapshot':str(source),'media_metadata':metadata,'read_only_original':True})

def crop(artifact,output,start_tick,end_tick,cancel=None):
    sr=artifact['sample_rate']; count=artifact['sample_count']
    start=max(0,(int(start_tick)*sr+TIMEBASE//2)//TIMEBASE); end=min(count,(int(end_tick)*sr+TIMEBASE//2)//TIMEBASE)
    if end<=start: raise ValueError('试听选区必须有正时长。')
    run([binary('ffmpeg'),'-v','error','-nostdin',*safe_input(validate_artifact(artifact)),'-af',f'atrim=start_sample={start}:end_sample={end},asetpts=PTS-STARTPTS','-c:a','pcm_f32le','-y',str(output)],cancel)
    return start,end

def bounded(v,lo,hi):
    f=float(v)
    if not math.isfinite(f) or not lo<=f<=hi: raise ValueError(f'参数不在 {lo}–{hi} 范围内。')
    return f

def effects_filter(effects):
    filters=[]
    for fx in effects:
        if not fx.get('enabled',True): continue
        typ=fx['type']
        if typ in ('highpass','lowpass'): filters.append(f"{typ}=f={bounded(fx.get('frequency',80 if typ=='highpass' else 16000),20,22000)}")
        elif typ=='compressor': filters.append(f"acompressor=threshold={10**(bounded(fx.get('threshold_db',-18),-60,0)/20)}:ratio={bounded(fx.get('ratio',3),1,20)}:makeup=1")
        elif typ=='delay': filters.append(f"aecho=1:1:{bounded(fx.get('delay_ms',120),1,2000)}:{bounded(fx.get('decay',.2),0,.9)}")
        elif typ=='reverb':
            wet=bounded(fx.get('wet',.15),0,.5)
            tail=bounded(fx.get('room_seconds',.65),.1,3)
            predelay=bounded(fx.get('predelay_ms',18),0,100)
            delays=[predelay+tail*1000*(index/24)**1.35 for index in range(1,25)]
            weights=[math.exp(-3*index/24) for index in range(1,25)]
            total=sum(weights)
            decay=[wet*weight/total for weight in weights]
            filters.append('aecho=1:1:'+ '|'.join(f'{value:.3f}' for value in delays)+':'+ '|'.join(f'{value:.6f}' for value in decay))
        elif typ=='limiter': filters.append(f"alimiter=limit={10**(bounded(fx.get('limit_db',-1),-18,0)/20)}:level=false:latency=true")
        else: raise ValueError(f'未实现的效果：{typ}')
    return filters

def mix(store,tracks,output,cancel=None,range_ticks=None,timeline_end=None):
    tracks=[t for t in tracks if not t.get('mute')]
    if any(t.get('solo') for t in tracks): tracks=[t for t in tracks if t.get('solo')]
    if not tracks:
        if range_ticks and len(range_ticks)==2 and all(type(v) is int for v in range_ticks) and range_ticks[0]>=0 and range_ticks[1]>range_ticks[0]:
            run([binary('ffmpeg'),'-v','error','-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-af',f'atrim=end_sample={range_ticks[1]-range_ticks[0]}','-c:a','pcm_f32le','-y',str(output)],cancel)
            return []
        raise ValueError('没有可混音的轨道。')
    args=[binary('ffmpeg'),'-v','error','-nostdin']; graph=[]; inputs=[]; total=0
    for i,t in enumerate(tracks):
        a=store.get('artifacts',t['artifact_id']); inputs.append(a['id']); args+=safe_input(validate_artifact(a))
        offset=int(t.get('offset_ticks',0)); total=max(total,round(a['sample_count']*TIMEBASE/a['sample_rate'])+offset)
        gain=10**(bounded(t.get('gain_db',0),-96,24)/20); pan=bounded(t.get('pan',0),-1,1)
        left=math.sqrt(1-max(0,pan)); right=math.sqrt(1+min(0,pan))
        filters=['aresample=48000','aformat=channel_layouts=stereo',f'volume={gain}',f'pan=stereo|c0={left}*c0|c1={right}*c1',*effects_filter(t.get('effects',[]))]
        if offset>0: filters.append(f'adelay={offset}S:all=1')
        elif offset<0: filters.append(f'atrim=start_sample={-offset}')
        filters+=['asetpts=PTS-STARTPTS']
        graph.append(f'[{i}:a]'+','.join(filters)+f'[t{i}]')
    if timeline_end is not None:total=max(total,int(timeline_end))
    trim=f'atrim=end_sample={max(total,1)}'
    if range_ticks:
        start,end=range_ticks
        if type(start) is not int or type(end) is not int or start<0 or end<=start:raise ValueError('混音试听选区无效。')
        trim+=f',apad,atrim=start_sample={start}:end_sample={end},asetpts=PTS-STARTPTS'
    graph.append(''.join(f'[t{i}]' for i in range(len(tracks)))+f'amix=inputs={len(tracks)}:normalize=0:duration=longest,apad,{trim}[out]')
    run(args+['-filter_complex',';'.join(graph),'-map','[out]','-c:a','pcm_f32le','-y',str(output)],cancel)
    return inputs

def loudness(path,sample_range=None):
    filters=(f'atrim=start_sample={sample_range[0]}:end_sample={sample_range[1]},asetpts=PTS-STARTPTS,' if sample_range else '')+'ebur128=peak=true'
    result=subprocess.run([binary('ffmpeg'),'-hide_banner','-nostdin',*safe_input(path),'-af',filters,'-f','null','-'],capture_output=True,timeout=180,creationflags=NO_WINDOW)
    text=result.stderr.decode(errors='replace'); tail=text[text.rfind('Summary:'):]
    i=re.search(r'I:\s+(-?[\d.]+) LUFS',tail); peak=re.search(r'Peak:\s+(-?[\d.]+) dBFS',tail)
    return {'integrated_lufs':float(i.group(1)) if i else None,'true_peak_dbfs':float(peak.group(1)) if peak else None,'measurement_engine':'ffmpeg ebur128 peak=true','measurement_succeeded':result.returncode==0}

def meter_levels(path,hop_seconds=.05):
    with sf.SoundFile(path) as audio:
        hop=max(1,round(audio.samplerate*hop_seconds));levels=[]
        while True:
            block=audio.read(hop,dtype='float32',always_2d=True)
            if not len(block):break
            levels.append([round(float(20*np.log10(max(float(np.max(np.abs(block[:,i]))),1e-12))),2) for i in range(block.shape[1])])
        return {'hop_seconds':hop/audio.samplerate,'peaks_dbfs':levels}

def measure_artifact(artifact,range_ticks=None):
    if not artifact.get('media_type','').startswith('audio'):raise ValueError('只能测量音频产物。')
    if range_ticks is not None and (not isinstance(range_ticks,list) or len(range_ticks)!=2 or any(type(v) is not int for v in range_ticks)):raise ValueError('测量区间无效。')
    path=validate_artifact(artifact);offset=artifact.get('timeline_offset_ticks',0);sr=artifact['sample_rate']
    start,end=0,artifact['sample_count']
    if range_ticks:
        if len(range_ticks)!=2 or any(type(v) is not int for v in range_ticks):raise ValueError('测量区间无效。')
        start=max(0,round((range_ticks[0]-offset)*sr/TIMEBASE));end=min(end,round((range_ticks[1]-offset)*sr/TIMEBASE))
    if end<=start:raise ValueError('测量选区与音频没有交集。')
    peak=0.;squares=0.;count=0
    with sf.SoundFile(path) as audio:
        audio.seek(start);remaining=end-start
        while remaining:
            block=audio.read(min(65536,remaining),dtype='float64',always_2d=True)
            if not len(block):raise ValueError('测量采样不足。')
            remaining-=len(block);peak=max(peak,float(np.max(np.abs(block))));squares+=float(np.square(block).sum());count+=block.size
    return {**loudness(path,[start,end]),'sample_peak_dbfs':20*math.log10(max(peak,1e-12)),
            'rms_dbfs':10*math.log10(max(squares/count,1e-24)),'sample_range':[start,end],
            'duration':(end-start)/sr,'artifact_id':artifact['id'],'artifact_hash':artifact['sha256'],'range_ticks':range_ticks}
