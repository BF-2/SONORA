"""Recheck original and reused resource hashes against bootstrap ledger."""
from pathlib import Path
import json
import os
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'workers'))
from common import sha256,write_json


def main():
    config=json.loads((ROOT/'engine-envs/resources.json').read_text(encoding='utf-8'))
    linked=json.loads((ROOT/'engine-envs/resource-links.json').read_text(encoding='utf-8'))['files']
    checks=[];downloaded=[]
    def check(source,path,digest):
        source,path=Path(source),Path(path)
        checks.append({'source':str(source),'path':str(path),'expected_sha256':digest,
                       'original_matches':sha256(source)==digest,'linked_matches':sha256(path)==digest,
                       'same_file':os.path.samefile(source,path)})
    for file in linked:check(file['source'],file['path'],file['sha256'])
    for model in config['models']:
        check(model['origin_path'],model['path'],model['model_hash'])
        if model.get('index_path'):
            check(Path(model['origin_path']).with_name(Path(model['index_path']).name),model['index_path'],model['index_hash'])
    for model in config['separation']['models']:
        if model.get('task')=='lead_backing' and model.get('source_url'):
            for path,expected in ((model['path'],model['model_hash']),(model['config_path'],model['config_hash'])):
                downloaded.append({'path':path,'expected_sha256':expected,'matches':sha256(path)==expected,'source_url':model.get('source_url')})
            continue
        source=Path(config['applio']['original_root']).parent/'separator-models'
        check(source/Path(model['path']).name,model['path'],model['model_hash'])
        check(source/Path(model['config_path']).name,model['config_path'],model['config_hash'])
    for model in config.get('svs',{}).get('models',[]):
        for relative,entry in model['components'].items():check(entry['source'],Path(model['root'])/relative,entry['sha256'])
    for entry in json.loads((ROOT/'engine-envs/media-tools.json').read_text(encoding='utf-8')).values():
        check(entry['source'],entry['path'],entry['sha256'])
    report={'checked_files':len(checks)+len(downloaded),'hardlinks':sum(c['same_file'] for c in checks),
            'all_original_hashes_match':all(c['original_matches'] for c in checks),
            'all_reused_hashes_match':all(c['linked_matches'] for c in checks) and all(d['matches'] for d in downloaded),'files':checks,'downloaded_files':downloaded}
    write_json(ROOT/'engine-envs/integrity-verification.json',report)
    print(json.dumps({k:v for k,v in report.items() if k not in ('files','downloaded_files')}))
    assert report['all_original_hashes_match'] and report['all_reused_hashes_match']


if __name__=='__main__':main()
