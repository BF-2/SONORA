"""Standard MIDI notes are references, never assumed ground-truth transcription."""
from fractions import Fraction
from io import BytesIO
import mido
from .config import TIMEBASE
from .store import uid

def read_midi(content):
    if len(content)>5*1024*1024:raise ValueError('MIDI 超过 5 MiB。')
    midi=mido.MidiFile(file=BytesIO(content))
    if midi.type==2 or midi.ticks_per_beat<=0:raise ValueError('当前只支持共享时间轴的 MIDI type 0/1 与 PPQ。')
    events=[]
    for track_index,track in enumerate(midi.tracks):
        tick=0
        for order,msg in enumerate(track):
            tick+=msg.time
            if msg.type in ('note_on','note_off','set_tempo'):events.append((tick,track_index,order,msg))
    if len(events)>100000:raise ValueError('MIDI 事件过多。')
    events.sort(key=lambda e:e[:3]);tempo=500000;last=0;elapsed=Fraction(0);active={};notes=[]
    for tick,track,_,msg in events:
        elapsed+=Fraction((tick-last)*tempo*TIMEBASE,midi.ticks_per_beat*1000000);last=tick
        position=round(elapsed)
        if msg.type=='set_tempo':tempo=msg.tempo;continue
        key=(track,msg.channel,msg.note)
        if msg.type=='note_on' and msg.velocity>0:active[key]=(position,msg.velocity)
        elif key in active:
            start,velocity=active.pop(key)
            if position>start:notes.append({'id':uid('note'),'start':start,'end':position,'pitch_midi_float':msg.note,'reference_source':'midi','velocity':velocity,'midi_track':track,'midi_channel':msg.channel,'lyric':'','phonemes':''})
    if any(n['pitch_midi_float']<24 or n['pitch_midi_float']>108 for n in notes):raise ValueError('MIDI 含当前编辑器范围以外的音符（24–108），请先选择声部。')
    if len(notes)>10000:raise ValueError('音符数量超过当前编辑上限。')
    return sorted(notes,key=lambda n:(n['start'],n['pitch_midi_float']))

def write_midi(notes):
    midi=mido.MidiFile(ticks_per_beat=480);track=mido.MidiTrack();midi.tracks.append(track)
    track.append(mido.MetaMessage('set_tempo',tempo=500000,time=0));events=[]
    for n in notes:
        note=round(n['pitch_midi_float'])
        events.extend([(round(n['start']/50),1,mido.Message('note_on',note=note,velocity=int(n.get('velocity',90)))),(round(n['end']/50),0,mido.Message('note_off',note=note,velocity=0))])
    events.sort(key=lambda e:(e[0],e[1]));last=0
    for tick,_,msg in events:msg.time=max(0,tick-last);last=tick;track.append(msg)
    track.append(mido.MetaMessage('end_of_track',time=0));out=BytesIO();midi.save(file=out);return out.getvalue()
