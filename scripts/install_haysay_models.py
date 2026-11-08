"""Install only the six hash-verified Hay Say RVC models into SONORA."""
import json,re,shutil,sys,subprocess,os
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from services.orchestrator.model_catalog import sha256,ModelCatalog
NAMES={'twilight_sparkle':'暮光闪闪 · Twilight Sparkle','rainbow_dash':'云宝 · Rainbow Dash','fluttershy':'柔柔 · Fluttershy','rarity':'珍奇 · Rarity','pinkie_pie':'碧琪 · Pinkie Pie','applejack':'苹果嘉儿 · Applejack'}
MODEL_ROOT=ROOT/'resources/models/haysay'
def prepare():
    source=Path('E:/voice/M6_RVC_compare_public_20261104');models=json.loads((source/'models.json').read_text(encoding='utf-8'))
    downloads=json.loads((source/'downloads.json').read_text())+json.loads((Path('E:/voice/RVC_pretrained_compare_20261104')/'downloads.json').read_text())
    records=[]
    config_path=ROOT/'engine-envs/resources.json';config=json.loads(config_path.read_text(encoding='utf-8'));python=config['applio']['python']
    for m in models:
        if m['kind']!='downloaded' or not m['id'].endswith('_hay'):continue
        role=m['role'];folder=MODEL_ROOT/role;folder.mkdir(parents=True,exist_ok=True)
        for source_key,hash_key in [('model_path','sha256'),('index_path','index_sha256')]:
            src=Path(m[source_key]);expected=m[hash_key];d=next(d for d in downloads if d['local_path']==str(src))
            assert sha256(src)==expected==d['sha256']==d['lfs']['oid']
            dst=folder/src.name
            if dst.exists():assert sha256(dst)==expected
            else:shutil.copy2(src,dst)
            assert sha256(dst)==expected
        profile=dict(model_sha256=m['sha256'],status='verified_local_inference',supported_speaker_ids=[0],embedder_model='contentvec',
            evidence=dict(source='Hay Say public role model and paired index; fixed ContentVec/RMVPE/sid=0 local conversion verification',repository=m['repo'],revision=m['revision'],verification=str(source/'audio-verification.json')),
            note='Supported local inference profile, not a claim that all 109 embedding rows were trained.')
        (folder/'model_info.json').write_text(json.dumps(dict(provider='haysay',role=role,source_url=m['source_url'],inference_profile=profile),ensure_ascii=False,indent=2),encoding='utf-8')
        readme=Path(m['model_path']).parent/'README.txt'
        if readme.exists():shutil.copy2(readme,folder/'SOURCE.txt')
        result=subprocess.run([python,str(ROOT/'workers/rvc/probe_model.py'),str(folder/Path(m['model_path']).name)],capture_output=True,text=True,encoding='utf-8',env={**os.environ,'PYTHONUTF8':'1','CUDA_VISIBLE_DEVICES':''},check=True)
        probe=json.loads(result.stdout)[0];assert probe.pop('ok');probe.pop('path',None)
        epoch_match=re.fullmatch(r'(\d+)epoch',m.get('info') or '')
        record={**probe,'id':'haysay_'+role,'name':NAMES[role]+' · Hay Say','provider':'haysay','role':role,
            'path':str(folder/Path(m['model_path']).name),'origin_path':str(folder/Path(m['model_path']).name),
            'index_path':str(folder/Path(m['index_path']).name),'index_hash':m['index_sha256'],'model_hash':m['sha256'],
            'origin_url':m['source_url'],'source_repository':m['repo'],'source_revision':m['revision'],
            'license':'OpenRAIL per source README' if 'Marup' in (readme.read_text() if readme.exists() else '') else 'unconfirmed; retain source README',
            'engine_revision':config['applio']['revision'],'inference_schema_version':1,'speaker_ids':[0],'speaker_ids_trained':[0],
            'speaker_metadata_status':'verified_inference_profile','embedder_model':'contentvec','embedder_verified':True,'inference_profile':profile,
            'epoch':int(epoch_match[1]) if epoch_match else None,'epoch_source':'checkpoint_info' if epoch_match else 'unknown',
            'resource_method':'private_copy','test_status':'safe_probe_passed'}
        records.append(record);print('INSTALLED',record['id'],record['sample_rate'],flush=True)
    assert len(records)==6
    config['models']=records;config['catalog']={**config.get('catalog',{}),'roots':[str(MODEL_ROOT)],'provider':'haysay'}
    config_path.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
    (ROOT/'engine-envs/haysay-models.json').write_text(json.dumps(dict(provider='haysay',models=records),ensure_ascii=False,indent=2),encoding='utf-8')
    return records
def seed(db_path):
    catalog=ModelCatalog(db_path)
    resolved=[]
    for m in json.loads((ROOT/'engine-envs/haysay-models.json').read_text(encoding='utf-8'))['models']:
        model=catalog.resolve_model(m['id']);assert model['index_match']['status']=='verified' and model['index_match']['fully_validated']
        assert model['model_hash']==m['model_hash'] and model['index_hash']==m['index_hash'] and model['speaker_ids']==[0]
        catalog.update_voice(model['voice_id'],dict(name=NAMES[m['role']],alias=NAMES[m['role']],tags=['Hay Say','M6'],description='Hay Say 公开 RVC 角色权重；配套索引已核验。'))
        catalog.set_default(model['voice_id'],m['id']);resolved.append(model);print('VERIFIED',m['id'],flush=True)
    return resolved
if __name__=='__main__':
    if '--seed-only' not in sys.argv:prepare()
    if '--prepare-only' not in sys.argv:seed(ROOT/'runtime/sonora.sqlite')
