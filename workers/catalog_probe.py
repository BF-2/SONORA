"""Bounded checkpoint metadata inspection. Never unpickles or reads tensor storage.

The tiny pickle opcode interpreter below only builds plain data and symbolic
tensor shapes. GLOBAL/REDUCE are inspected, never imported or called.
"""
from __future__ import annotations
import argparse
from collections import Counter
import json
from pathlib import Path, PurePosixPath
import pickletools
import re
import struct
import zipfile

MAX_METADATA = 4 * 1024 * 1024
MAX_OPS = 300_000
MAX_MODEL_BYTES = 2 * 1024**3
MARK = object()
class TensorMetadata:
    def __init__(self,shape):self.shape=shape
ALLOWED_GLOBALS = {'collections OrderedDict', 'torch._utils _rebuild_tensor',
    'torch._utils _rebuild_tensor_v2', 'torch._utils _rebuild_tensor_v3',
    'torch._utils _rebuild_parameter', 'torch._utils _rebuild_parameter_with_state',
    'torch Tensor', 'torch.nn.parameter Parameter', 'builtins set'}


def safe_pickle_metadata(data: bytes):
    if len(data) > MAX_METADATA: raise ValueError('Checkpoint metadata exceeds 4 MiB budget')
    stack=[]; memo={}; globals_seen=[]
    def mark():
        for i in range(len(stack)-1,-1,-1):
            if stack[i] is MARK:
                values=stack[i+1:];del stack[i:];return values
        raise ValueError('Malformed pickle mark')
    for count,(op,arg,pos) in enumerate(pickletools.genops(data)):
        if count > MAX_OPS or len(stack)>100_000 or len(memo)>100_000: raise ValueError('Metadata complexity budget exceeded')
        name=op.name
        if name in ('PROTO','FRAME'):continue
        if name=='MARK':stack.append(MARK)
        elif name=='STOP':
            if len(stack)!=1:raise ValueError('Malformed checkpoint metadata stack')
            return stack[0],globals_seen
        elif name in ('NONE','NEWTRUE','NEWFALSE'):stack.append({'NONE':None,'NEWTRUE':True,'NEWFALSE':False}[name])
        elif name in ('INT','BININT','BININT1','BININT2','LONG','LONG1','LONG4','FLOAT','BINFLOAT','UNICODE','BINUNICODE','SHORT_BINUNICODE','BINUNICODE8','STRING','BINSTRING','SHORT_BINSTRING','BINBYTES','SHORT_BINBYTES','BINBYTES8','BYTEARRAY8'):
            if isinstance(arg,(str,bytes,bytearray)) and len(arg)>1_000_000:raise ValueError('Metadata scalar too large')
            stack.append(arg)
        elif name=='EMPTY_DICT':stack.append({})
        elif name=='EMPTY_LIST':stack.append([])
        elif name=='EMPTY_TUPLE':stack.append(())
        elif name=='EMPTY_SET':stack.append(set())
        elif name in ('DICT','LIST','TUPLE','FROZENSET'):
            items=mark()
            stack.append(dict(zip(items[::2],items[1::2])) if name=='DICT' else list(items) if name=='LIST' else tuple(items) if name=='TUPLE' else frozenset(items))
        elif name in ('TUPLE1','TUPLE2','TUPLE3'):
            n=int(name[-1]);items=stack[-n:];del stack[-n:];stack.append(tuple(items))
        elif name=='SETITEM':
            value=stack.pop();key=stack.pop();stack[-1][key]=value
        elif name=='SETITEMS':
            items=mark()
            if len(items)%2:raise ValueError('Invalid dict metadata')
            stack[-1].update(zip(items[::2],items[1::2]))
        elif name=='APPEND':
            value=stack.pop();stack[-1].append(value)
        elif name=='APPENDS':
            items=mark();stack[-1].extend(items)
        elif name=='ADDITEMS':
            items=mark();stack[-1].update(items)
        elif name in ('BINPUT','LONG_BINPUT','PUT'):memo[int(arg)]=stack[-1]
        elif name=='MEMOIZE':memo[len(memo)]=stack[-1]
        elif name in ('BINGET','LONG_BINGET','GET'):stack.append(memo[int(arg)])
        elif name in ('GLOBAL','STACK_GLOBAL'):
            if name=='STACK_GLOBAL':
                symbol=stack.pop();module=stack.pop();arg=str(module)+' '+str(symbol)
            allowed=arg in ALLOWED_GLOBALS or re.fullmatch(r'torch (?:Float|Half|Double|Long|Int|Short|Char|Byte|Bool|BFloat16)Storage',str(arg))
            if not allowed:raise ValueError('Unsupported pickle global (not executed): '+str(arg))
            globals_seen.append(arg);stack.append({'__global__':arg})
        elif name=='BINPERSID':
            storage=stack.pop();stack.append({'__storage__':repr(storage)[:300]})
        elif name=='REDUCE':
            args=stack.pop();function=stack.pop();symbol=function.get('__global__') if isinstance(function,dict) else None
            if symbol=='collections OrderedDict':stack.append(dict(args[0]) if args else {})
            elif symbol and symbol.startswith('torch._utils _rebuild_tensor'):
                shape=list(args[2])
                if len(shape)>12 or any(not isinstance(x,int) or x<0 or x>2**40 for x in shape):raise ValueError('Invalid tensor shape')
                stack.append(TensorMetadata(shape))
            elif symbol and symbol.startswith('torch._utils _rebuild_parameter'):stack.append(args[0])
            elif symbol=='builtins set':stack.append(set(args[0]) if args else set())
            else:raise ValueError('Unsupported REDUCE (never executed): '+str(symbol))
        elif name=='BUILD':
            state=stack.pop()
            # Attribute metadata is irrelevant to exported tensor shape/config.
            if not isinstance(state,(dict,tuple,type(None))):raise ValueError('Unsupported BUILD state')
        elif name=='POP':stack.pop()
        elif name=='POP_MARK':mark()
        elif name=='DUP':stack.append(stack[-1])
        else:raise ValueError('Unsupported pickle opcode (never executed): '+name)
    raise ValueError('Incomplete checkpoint metadata')


def _positive_int(value):
    return value if type(value) is int and 0<=value<2**53 else None


def static_checkpoint(path):
    path=Path(path)
    if not 0<path.stat().st_size<=MAX_MODEL_BYTES:raise ValueError('Checkpoint size outside allowed budget')
    if not zipfile.is_zipfile(path):raise ValueError('Legacy non-ZIP checkpoint requires a separate trusted export; no unsafe pickle fallback')
    with path.open('rb') as source:
        source.seek(max(0,path.stat().st_size-65557));tail=source.read(65557)
    end=tail.rfind(b'PK\x05\x06')
    if end<0 or len(tail)-end<22:raise ValueError('Invalid ZIP footer')
    entries_count=struct.unpack_from('<H',tail,end+10)[0]
    central_size=struct.unpack_from('<I',tail,end+12)[0]
    if entries_count>20_000 or central_size>MAX_METADATA:raise ValueError('ZIP directory exceeds metadata budget')
    with zipfile.ZipFile(path) as archive:
        entries=archive.infolist()
        if len(entries)>20_000:raise ValueError('Too many ZIP entries')
        for entry in entries:
            p=PurePosixPath(entry.filename)
            if p.is_absolute() or '..' in p.parts or '\\' in entry.filename or ':' in entry.filename:raise ValueError('Unsafe ZIP member path')
        members=[x for x in entries if PurePosixPath(x.filename).name=='data.pkl']
        if len(members)!=1:raise ValueError('Expected one PyTorch metadata member')
        member=members[0]
        if member.file_size>MAX_METADATA or member.file_size>max(1,member.compress_size)*200:raise ValueError('Metadata ZIP size/ratio exceeds budget')
        data,globals_seen=safe_pickle_metadata(archive.read(member))
    if not isinstance(data,dict):raise ValueError('Checkpoint root is not a dictionary')
    checkpoint_name=bool(re.match(r'^[GD]_\d+',path.stem,re.I))
    weights=data.get('weight');config=data.get('config')
    if not isinstance(weights,dict) or not isinstance(config,(list,tuple)):
        if checkpoint_name or any(k in data for k in ('optimizer','model','iteration')):
            return {'kind':'training_checkpoint','status':'training_only','epoch':_positive_int(data.get('epoch')),
                'global_step':_positive_int(data.get('global_step',data.get('step'))),
                'training_iteration':_positive_int(data.get('iteration')),
                'filename_counter_hint':int(re.match(r'^[GD]_(\d+)',path.stem,re.I)[1]) if checkpoint_name else None,
                'counter_semantics':'unknown_without_training_adapter',
                'metadata_evidence':'static_checkpoint_schema','reason':'G/D/optimizer training state is not a confirmed inference export'}
        raise ValueError('Missing inference weight/config structure')
    emb=weights.get('emb_g.weight');shape=emb.shape if isinstance(emb,TensorMetadata) else []
    if len(config)<10 or len(shape)!=2:raise ValueError('Invalid RVC inference configuration/speaker embedding')
    version=data.get('version','v1');rate=_positive_int(config[-1]);vocoder=data.get('vocoder','HiFi-GAN')
    if version not in ('v1','v2') or rate not in (24000,32000,40000,48000) or vocoder not in ('HiFi-GAN','RefineGAN'):raise ValueError('Unsupported RVC architecture/sample rate/vocoder')
    epoch=_positive_int(data.get('epoch'));step=_positive_int(data.get('global_step',data.get('step')))
    result={'kind':'inference_export','status':'discovered','engine_id':'rvc','architecture':'RVC-'+version,
        'sample_rate':rate,'content_dim':768 if version=='v2' else 256,'f0_conditioned':bool(data.get('f0',1)),
        'vocoder':vocoder,'embedding_speaker_capacity':shape[0],'epoch':epoch,'global_step':step,
        'metadata_evidence':'static_checkpoint_schema','epoch_source':'checkpoint' if epoch is not None else 'unknown',
        'step_source':'checkpoint' if step is not None else 'unknown','tensor_storage_loaded':False,
        'metadata_globals':sorted(set(globals_seen))}
    for key in ('embedder_model','model_name','name','creation_date','author','speakers_id','speaker_ids'):
        value=data.get(key)
        if isinstance(value,(str,int,list)) and len(str(value))<4096:result[key]=value
    # Epoch/step from names remain explicitly inferred and never replace checkpoint scalars.
    match=re.search(r'(?:^|[_-])(\d+)e(?:[_-](\d+)s)?(?:$|[_-])',path.stem,re.I)
    if match:
        if result['epoch'] is None:result.update(epoch=int(match[1]),epoch_source='filename_inferred')
        if result['global_step'] is None and match[2]:result.update(global_step=int(match[2]),step_source='filename_inferred')
    return result


def static_index(path):
    path=Path(path)
    if not 0<path.stat().st_size<=MAX_MODEL_BYTES:raise ValueError('Index size outside allowed budget')
    with path.open('rb') as file:header=file.read(64)
    if len(header)<16:raise ValueError('Truncated FAISS index')
    magic=header[:4].decode('ascii',errors='replace');dimension=struct.unpack('<i',header[4:8])[0]
    if not magic.startswith('I') or not 1<=dimension<=8192:raise ValueError('Unsupported FAISS header')
    return {'kind':'index','status':'discovered','content_dim':dimension,'index_type':magic,
            'dimension_source':'faiss_binary_header','fully_validated':False}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('path');parser.add_argument('--index',action='store_true');args=parser.parse_args()
    try:
        if args.index:
            import faiss
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from workers.faiss_io import enable_unicode_paths
            enable_unicode_paths(faiss)
            index=faiss.read_index(str(Path(args.path).resolve(strict=True)))
            result={'ok':True,'content_dim':int(index.d),'ntotal':int(index.ntotal),'trained':bool(index.is_trained),'fully_validated':True}
        else:result={'ok':True,**static_checkpoint(args.path)}
        print(json.dumps(result,ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=True));raise SystemExit(1)


if __name__=='__main__':main()
