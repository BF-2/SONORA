"""Link existing local resources into SONORA. Original files are never modified.

Only immutable files are hardlinked. No installs, downloads, training, or model execution.
Run with a standard Python; safe checkpoint probes run in the existing RVC runtime.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'workers'))
from common import sha256, write_json


def link_file(source, target):
    target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists():
        if os.path.samefile(source,target): return "hardlink"
        if sha256(source)==sha256(target): return "existing_copy"
        raise RuntimeError(f"Refusing to overwrite changed resource: {target}")
    try:
        os.link(source,target)
        return "hardlink"
    except OSError:
        shutil.copy2(source,target)
        return "copy"


def media_tools(source):
    result={}; records={}
    for name in ('ffmpeg','ffprobe'):
        located=shutil.which(name)
        original=Path(located).resolve() if located else source/'Applio'/(name+'.exe')
        target=ROOT/'resources/tools'/(name+'.exe')
        method=link_file(original,target)
        version=subprocess.check_output([str(target),'-version'],text=True,encoding='utf-8',errors='replace')
        records[name]={'source':str(original),'path':str(target),'sha256':sha256(target),
                       'method':method,'version_output':version,
                       'license_status':'GPL build; inspect full build flags before redistribution' if '--enable-gpl' in version else 'Review actual build license'}
        result[name]=str(target)
    write_json(ROOT/'engine-envs/media-tools.json',records)
    return result


def svs_resources(source):
    bank=source/'open-source-song-workflow/apps/OpenUtau/Singers/Nishiren Diffsinger v2.0'
    if not (bank/'dsmain/acoustic.onnx').is_file():
        return {'status':'missing_voicebank','models':[]}
    target=ROOT/'resources/svs/nishiren-diffsinger-v2'
    files={}; total=hashlib.sha256()
    for file in sorted(bank.rglob('*')):
        if not file.is_file(): continue
        relative=file.relative_to(bank)
        if file.is_symlink(): raise ValueError('Unexpected voicebank symlink')
        method=link_file(file,target/relative); digest=sha256(file)
        key=str(relative).replace('\\','/')
        files[key]={'sha256':digest,'size':file.stat().st_size,'method':method,'source':str(file)}
        total.update(key.encode()+digest.encode())
    manifest={'id':'nishiren-diffsinger-v2','name':'Nishiren Gard AI DiffSinger 2.0','root':str(target),
              'engine_id':'diffsinger-onnx','model_hash':total.hexdigest(),'components':files,
              'source_url':'https://github.com/Gardanana/Nishiren-AI-Diffsinger/releases/tag/v2.0',
              'license_url':'https://github.com/Gardanana/Nishiren-AI-Diffsinger/blob/v2.0/terms-of-service.md',
              'license':'Custom; attribution required; commercial use requires author approval; no bundled redistribution',
              'sample_rate':44100,'hop_size':512,'languages':['ja'],'test_status':'unverified',
              'capabilities':{'svs':True,'notes':True,'phoneme_timing':True,'external_f0':False,'streaming':False,
                              'local_segment_render':False,'voice_conversion':False,'cancel_mode':'terminate_process'}}
    return {'python':str(source/'audio-separator-venv/Scripts/python.exe'),'default_model':manifest['id'],'models':[manifest]}


def pitch_resources(source,install=False):
    env=ROOT/'engine-envs/pitch/.venv';python=env/'Scripts/python.exe'
    base_python=source/'Applio/.venv/Scripts/python.exe'
    if install:
        uv=shutil.which('uv')
        if not uv: raise RuntimeError('uv is required for explicit pitch overlay installation')
        if not python.exists(): subprocess.run([uv,'venv','--python',str(base_python),str(env)],check=True)
        site=env/'Lib/site-packages'
        (site/'sonora-rvc-base.pth').write_text(str(source/'Applio/.venv/Lib/site-packages')+'\n',encoding='utf-8')
        subprocess.run([uv,'pip','install','--python',str(python),'--no-deps','--only-binary=:all:','--require-hashes',
                        '-r',str(ROOT/'engine-envs/pitch/requirements-overlay.lock')],check=True)
    if not python.exists(): return {'python':str(base_python),'pyworld_status':'not_installed'}
    return {'python':str(python),'pyworld_status':'installed','pyworld_version':'0.3.5',
            'base_site_packages':str(source/'Applio/.venv/Lib/site-packages'),
            'overlay_lock_hash':sha256(ROOT/'engine-envs/pitch/requirements-overlay.lock'),
            'base_environment_lock_hash':sha256(ROOT/'engine-envs/locked-environments.json') if (ROOT/'engine-envs/locked-environments.json').exists() else None}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=Path,default=ROOT.parent)
    parser.add_argument('--install-pitch-overlay',action='store_true',help='Explicitly install pinned pyworld into a project-only overlay (network)')
    args=parser.parse_args()
    source=args.source.resolve(); resources=ROOT/'resources'; applio=source/'Applio'
    python=applio/'.venv/Scripts/python.exe'
    snapshot=resources/'applio'
    files=[]
    # Snapshot only inference code/config/licenses and needed pretrained assets.
    for directory in ['rvc/infer','rvc/lib','rvc/configs','rvc/models/embedders/contentvec','rvc/models/predictors']:
        for file in (applio/directory).rglob('*'):
            if file.is_file() and '__pycache__' not in file.parts and file.suffix!='.pyc': files.append(file)
    files.extend(p for p in [applio/'LICENSE',applio/'TERMS_OF_USE.md',applio/'requirements.txt'] if p.is_file())
    source_hash=hashlib.sha256(); links=[]
    for file in sorted(files):
        relative=file.relative_to(applio)
        method=link_file(file,snapshot/relative)
        digest=sha256(file)
        source_hash.update(str(relative).replace('\\','/').encode()+digest.encode())
        links.append({'source':str(file),'path':str(snapshot/relative),'sha256':digest,'method':method})
    # Latest inference checkpoint per trained model folder; retain all originals.
    checkpoints=[]
    for folder in sorted((applio/'logs').iterdir()):
        if not folder.is_dir(): continue
        choices=[p for p in folder.glob('*.pth') if not re.match(r'^[GD]_\d+',p.name)]
        def epoch(p):
            match=re.search(r'_(\d+)e_',p.name)
            return int(match.group(1)) if match else -1
        if choices: checkpoints.append(max(choices,key=lambda p:(epoch(p),p.name)))
    result=subprocess.run([str(python),str(ROOT/'workers/rvc/probe_model.py'),*map(str,checkpoints)],capture_output=True,text=True,encoding='utf-8',env={**os.environ,'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'},check=True)
    probes=json.loads(result.stdout); models=[]
    for item in probes:
        origin=Path(item.pop('path')); model_root=resources/'models'/origin.parent.name
        target=model_root/origin.name
        method=link_file(origin,target)
        index=next(iter(sorted(origin.parent.glob('*.index'))),None)
        manifest={'id':origin.parent.name,'name':origin.parent.name.replace('_',' '),'path':str(target),
                  'origin_path':str(origin),'origin_url':None,'license':'unconfirmed','code_license':'MIT (Applio)',
                  'engine_revision':subprocess.check_output(['git','-C',str(applio),'rev-parse','HEAD'],text=True).strip(),
                  'resource_method':method,'inference_schema_version':1,**item}
        if index:
            link_file(index,model_root/index.name)
            manifest.update(index_path=str(model_root/index.name),index_hash=sha256(index))
        if not item['ok']:
            manifest.update(engine_id='rvc',test_status='incompatible',capabilities={},model_hash=sha256(origin))
        models.append(manifest)
    separator_models=[]
    for ckpt in sorted((source/'separator-models').glob('*.ckpt')):
        if ckpt.name=='mel_band_roformer_vocals_fv4_gabox.ckpt': yaml_name='config_mel_band_roformer_vocals_gabox.yaml'
        elif ckpt.name=='melband_roformer_big_beta6x.ckpt': yaml_name='config_melbandroformer_big_beta6x.yaml'
        else: continue
        yaml=ckpt.with_name(yaml_name)
        if not yaml.is_file(): continue
        dest=resources/'separation'/ckpt.name
        link_file(ckpt,dest); link_file(yaml,dest.with_name(yaml_name))
        separator_models.append({'id':ckpt.stem,'name':ckpt.stem,'path':str(dest),'config_path':str(dest.with_name(yaml_name)),
                                 'model_hash':sha256(ckpt),'config_hash':sha256(yaml),'license':'unconfirmed',
                                 'task':'vocals_instrumental','native_stem_roles':{'Vocals':'vocal','Instrumental':'instrumental'},
                                 'stem_roles':{'Vocals':'vocal','Instrumental':'instrumental'},'sample_rate':44100,'test_status':'unverified'})
    harmony_manifest=ROOT/'engine-envs/harmony/model-manifest.json'
    default_harmony_model=None
    if harmony_manifest.exists():
        harmony=json.loads(harmony_manifest.read_text(encoding='utf-8'))
        if harmony.get('task')=='lead_backing' and Path(harmony['path']).is_file() and Path(harmony['config_path']).is_file():
            if sha256(harmony['path'])==harmony['model_hash'] and sha256(harmony['config_path'])==harmony['config_hash']:
                separator_models.append(harmony);default_harmony_model=harmony['id']
    config={'schema_version':1,'applio':{'python':str(python),'root':str(snapshot),'revision':models[0]['engine_revision'] if models else None,
                                       'source_hash':source_hash.hexdigest(),'original_root':str(applio)},
            'separation':{'python':str(source/'audio-separator-venv/Scripts/python.exe'),'version':'0.44.2','model_dir':str(resources/'separation'),
                          'default_model':'mel_band_roformer_vocals_fv4_gabox','default_harmony_model':default_harmony_model,'models':separator_models},
            'models':models,'svs':svs_resources(source),'pitch':pitch_resources(source,args.install_pitch_overlay),**media_tools(source)}
    write_json(ROOT/'engine-envs/resources.json',config)
    write_json(ROOT/'engine-envs/resource-links.json',{'source_hash':source_hash.hexdigest(),'files':links})
    environment_locks={}
    for name,executable in [('rvc',python),('separation',Path(config['separation']['python']))]:
        freeze=subprocess.run([str(executable),'-c',"import importlib.metadata as m; print('\\n'.join(sorted(d.metadata['Name']+'=='+d.version for d in m.distributions() if d.metadata['Name'])))"],capture_output=True,text=True,encoding='utf-8',check=True)
        (ROOT/'engine-envs'/f'{name}-freeze.txt').write_text(freeze.stdout,encoding='utf-8')
        environment_locks[name]={'python':str(executable),'packages':dict(line.split('==',1) for line in freeze.stdout.splitlines() if '==' in line)}
    write_json(ROOT/'engine-envs/locked-environments.json',environment_locks)
    if config['pitch'].get('pyworld_status')=='installed':
        config['pitch']['base_environment_lock_hash']=sha256(ROOT/'engine-envs/locked-environments.json')
        write_json(ROOT/'engine-envs/resources.json',config)
    print(json.dumps({'models':len(models),'compatible':sum(m['ok'] for m in models),'separator_models':len(separator_models),'linked_snapshot_files':len(links),'config':str(ROOT/'engine-envs/resources.json')}))


if __name__=='__main__': main()
