"""Explicit resumable installation of the pinned lead/backing separator.

No network request occurs without --download. Ctrl+C or --cancel-file stops the
job and keeps .part bytes for a later resume. Rendering never invokes this script.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'workers'))
from common import sha256,write_json,read_json

MODEL_ID='melband-karaoke-aufr33-viperx-10.1956'
BASE='https://github.com/nomadkaraoke/python-audio-separator/releases/download/model-configs/'
FILES=[
    {'name':'mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt','bytes':913096801,
     'sha256':'1de20d459332fe8869aeb01327a31df0032262706e1365114e852dc271779813','github_asset_id':192841139},
    {'name':'mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956_config.yaml','bytes':1726,
     'sha256':'b35077d94861f068097cce1a5e54633c055e7dcc2613eade4e4dc7c7c9c3f48b','github_asset_id':192850484},
]
DESTINATION=ROOT/'resources/separation/harmony'
RECORDS=ROOT/'engine-envs/harmony'


def download(entry,cancel_file=None):
    path=DESTINATION/entry['name'];partial=path.with_suffix(path.suffix+'.part')
    if path.exists():
        if path.stat().st_size!=entry['bytes'] or sha256(path)!=entry['sha256']:
            raise ValueError('Existing resource hash mismatch; refusing overwrite: '+str(path))
        return
    offset=partial.stat().st_size if partial.exists() else 0
    if offset>entry['bytes']: raise ValueError('Partial file exceeds expected size')
    if cancel_file and cancel_file.exists(): raise KeyboardInterrupt('Cancellation marker present')
    headers={'User-Agent':'SONORA-model-prepare/1','Accept-Encoding':'identity'}
    if offset: headers['Range']=f'bytes={offset}-'
    request=urllib.request.Request(BASE+entry['name'],headers=headers)
    last_report=0;started=time.monotonic()
    if offset<entry['bytes']:
        with urllib.request.urlopen(request,timeout=30) as response:
            if offset and response.status==206:
                if not response.headers.get('Content-Range','').startswith(f'bytes {offset}-'):
                    raise ValueError('Resume Content-Range mismatch')
                mode='ab'
            elif response.status==200:
                # Server ignored Range: restart only this incomplete project file.
                mode='wb';offset=0
            else: raise ValueError(f'Unexpected download status {response.status}')
            with partial.open(mode) as target:
                while True:
                    if cancel_file and cancel_file.exists(): raise KeyboardInterrupt('Cancelled')
                    block=response.read(1024*1024)
                    if not block: break
                    offset+=len(block)
                    if offset>entry['bytes']: raise ValueError('Remote resource exceeded pinned size')
                    target.write(block)
                    if time.monotonic()-last_report>10:
                        status={'state':'downloading','file':entry['name'],'received_bytes':offset,'expected_bytes':entry['bytes']}
                        write_json(RECORDS/'download-state.json',status);print(json.dumps(status),flush=True)
                        last_report=time.monotonic()
                target.flush();os.fsync(target.fileno())
    if partial.stat().st_size!=entry['bytes'] or sha256(partial)!=entry['sha256']:
        raise ValueError('Downloaded resource failed pinned size/SHA-256 verification; partial retained')
    os.replace(partial,path)
    print(json.dumps({'state':'verified','file':entry['name'],'sha256':entry['sha256'],'elapsed_seconds':time.monotonic()-started}),flush=True)


def registry_manifest():
    return {'id':MODEL_ID,'name':'MelBand RoFormer Karaoke · aufr33 & viperx','task':'lead_backing','engine_id':'audio-separator',
            'path':str(DESTINATION/FILES[0]['name']),'config_path':str(DESTINATION/FILES[1]['name']),
            'model_hash':FILES[0]['sha256'],'config_hash':FILES[1]['sha256'],'sample_rate':44100,
            'native_stem_roles':{'Vocals':'lead_vocal','Instrumental':'backing_vocal'},
            'stem_roles':{'Vocals':'lead_vocal','Instrumental':'backing_vocal'},
            'native_target':'Vocals','residual_native_stem':'Instrumental','required_input_role':'combined_vocals',
            'semantic_basis':'Pinned Karaoke checkpoint estimates lead vocals; apply to combined isolated vocals. Complement is backing group plus any residual bleed.',
            'semantic_sources':[
                'https://mvsep.com/algorithms/76',
                'https://raw.githubusercontent.com/nomadkaraoke/python-audio-separator/v0.44.2/audio_separator/models.json',
                'https://raw.githubusercontent.com/nomadkaraoke/python-audio-separator/v0.44.2/audio_separator/ensemble_presets.json'],
            'source_url':BASE+FILES[0]['name'],'config_source_url':BASE+FILES[1]['name'],
            'source_release_id':175147634,'source_release_tag':'model-configs','source_asset_ids':[f['github_asset_id'] for f in FILES],
            'source_release_updated':'2024-09-16','code_version':'audio-separator 0.44.2','authors':['aufr33','viperx'],
            'license':'unconfirmed','redistribution':'not bundled; weight license not established by code MIT license',
            'test_status':'download_verified_not_yet_rendered','local_segment_render':False,
            'warnings':['仅对合并人声运行第二步分离。输入若有伴奏残留，会进入背景组；结果需试听。','分组是模型估计，不能保证重唱者、所有和声或混响完全分开。']}


def register():
    manifest=registry_manifest()
    existing=RECORDS/'model-manifest.json'
    if existing.exists():
        old=read_json(existing)
        if old.get('model_hash')==manifest['model_hash'] and old.get('config_hash')==manifest['config_hash']:
            for key in ('test_status','verification','validation_scope','verified_devices','user_sample_verification'):
                if key in old: manifest[key]=old[key]
    write_json(existing,manifest)
    path=ROOT/'engine-envs/resources.json';config=read_json(path)
    config['separation']['models']=[m for m in config['separation']['models'] if m['id']!=MODEL_ID]+[manifest]
    config['separation']['default_harmony_model']=MODEL_ID
    write_json(path,config)
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download',action='store_true')
    parser.add_argument('--cancel-file',type=Path)
    args=parser.parse_args()
    if not args.download:
        print(json.dumps({'model':MODEL_ID,'total_bytes':sum(f['bytes'] for f in FILES),'files':FILES,
                          'action':'Run with --download to fetch and register; rendering never downloads.'},indent=2));return 0
    DESTINATION.mkdir(parents=True,exist_ok=True);RECORDS.mkdir(parents=True,exist_ok=True)
    try:
        for entry in FILES: download(entry,args.cancel_file)
        config=read_json(ROOT/'engine-envs/resources.json')
        # Static pickle-global inspection first; no unrestricted pickle execution.
        script="import torch,sys,json; p=sys.argv[1]; unsafe=torch.serialization.get_unsafe_globals_in_checkpoint(p); print(json.dumps(unsafe)); sys.exit(2 if unsafe else 0)"
        checked=subprocess.run([config['separation']['python'],'-c',script,str(DESTINATION/FILES[0]['name'])],
                               capture_output=True,text=True,env={**os.environ,'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'},check=True)
        manifest=register()
        write_json(RECORDS/'download-state.json',{'state':'completed','model_id':manifest['id'],'bytes':sum(f['bytes'] for f in FILES),
                                                 'sha256_verified':True,'unsafe_pickle_globals':json.loads(checked.stdout)})
        print(json.dumps({'state':'registered','model_id':manifest['id'],'task':manifest['task']}));return 0
    except KeyboardInterrupt:
        write_json(RECORDS/'download-state.json',{'state':'cancelled','resume_supported':True});return 130
    except Exception as exc:
        write_json(RECORDS/'download-state.json',{'state':'failed','message':str(exc),'resume_supported':True});raise


if __name__=='__main__':raise SystemExit(main())
