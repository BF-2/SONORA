"""Explicit read-only validation of the installed six-role Hay Say catalog."""
import argparse,json,tempfile
from pathlib import Path
from services.orchestrator.config import ROOT,resources
from services.orchestrator.model_catalog import ModelCatalog,sha256

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--run-real-metadata',action='store_true');a=parser.parse_args()
    if not a.run_real_metadata:parser.error('--run-real-metadata is required')
    models=resources()['models'];assert len(models)==6 and all(m['provider']=='haysay' for m in models)
    with tempfile.TemporaryDirectory() as d:
        catalog=ModelCatalog(Path(d)/'catalog.sqlite');resolved=[]
        for m in models:
            value=catalog.resolve_model(m['id']);assert value['speaker_ids']==[0] and value['index_match']['fully_validated']
            assert sha256(value['path'])==m['model_hash'] and sha256(value['index_path'])==m['index_hash']
            resolved.append({'id':m['id'],'model_hash':m['model_hash'],'index_hash':m['index_hash']})
        assert catalog.list_models()['total']==6
    print(json.dumps({'passed':True,'models':resolved}))
if __name__=='__main__':main()
