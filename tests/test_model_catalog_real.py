"""Opt-in local metadata/CPU validation; no GPU rendering or downloads."""
import argparse
import json
from pathlib import Path
import sqlite3
import time
import uuid
from services.orchestrator.model_catalog import ModelCatalog, sha256, utc
from services.orchestrator.config import ROOT, resources


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--run-real-metadata',action='store_true');args=parser.parse_args()
    if not args.run_real_metadata:parser.error('--run-real-metadata is required')
    output=ROOT/'tests/results/catalog';output.mkdir(parents=True,exist_ok=True)
    database=output/('verification-'+uuid.uuid4().hex[:8]+'.sqlite')
    with sqlite3.connect(database) as db:
        db.executescript('PRAGMA user_version=1;CREATE TABLE legacy_proof(id TEXT PRIMARY KEY,body TEXT);')
        db.execute('INSERT INTO legacy_proof VALUES(?,?)',('historical-selection','immutable-model-hash-and-id'))
    baseline={m['id']:m['model_hash'] for m in resources()['models']}
    catalog=ModelCatalog(database)
    scan=catalog.scan(max_files=5000,max_seconds=60)
    assert scan['state']=='succeeded'
    run=next(r for r in catalog.list_runs(q='twilight_sparkle_pony_singing_r18m')['runs'] if r['name']=='twilight_sparkle_pony_singing_r18m_20260914')
    models=catalog.list_models(run_id=run['id'])['models']
    assert sorted(m['epoch'] for m in models)==[200,225,250,275,300]
    assert all(m['speaker_ids']==[0] and m['embedding_speaker_capacity']==109 for m in models)
    chosen=[next(m for m in models if m['epoch']==epoch) for epoch in (200,225)]
    index=next(i for i in catalog.model_indices(chosen[0]['id'])['indices'] if i['compatibility']['status']=='verified')
    source_paths={Path(m['origin_path']) for m in chosen}|{Path(index['origin_path'])}
    before={str(path):sha256(path) for path in source_paths}
    resolved=[]
    for model in chosen:
        started=time.perf_counter();manifest=catalog.resolve_model(model['id'])
        resolved.append({**{k:manifest.get(k) for k in ('id','voice_id','run_id','epoch','global_step','model_hash','speaker_ids','embedding_speaker_capacity','embedder_model','embedder_verified','index_id','index_hash','index_match')},'validation_seconds':time.perf_counter()-started})
    after={str(path):sha256(path) for path in source_paths}
    assert before==after
    assert resolved[0]['model_hash']!=resolved[1]['model_hash']
    assert resolved[0]['index_hash']==resolved[1]['index_hash']
    assert all(x['index_match']['status']=='verified' and x['index_match']['fully_validated'] for x in resolved)
    assert all(catalog.get_model(identifier)['model_hash']==digest for identifier,digest in baseline.items())
    with sqlite3.connect(database) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==1
        assert db.execute('SELECT body FROM legacy_proof').fetchone()[0]=='immutable-model-hash-and-id'
    report={'schema_version':1,'created_at':utc(),'passed':True,
        'scope':'Read-only metadata scan + selected restricted CPU weight/FAISS validation. No neural audio rendering, GPU calls, downloads, training or source writes.',
        'scan':scan,'run':{'id':run['id'],'name':run['name'],'versions':[{'id':m['id'],'epoch':m['epoch'],'global_step':m['global_step'],'epoch_source':m['epoch_source'],'step_source':m['step_source']} for m in models]},
        'selected_versions':resolved,'original_sha256_before':before,'original_sha256_after':after,
        'legacy_ids_and_hashes_preserved':len(baseline),'main_user_version_preserved':1,'database':str(database)}
    (output/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'scan_counts':scan['counts'],'scan_seconds':scan['elapsed_seconds'],
                      'epochs':[m['epoch'] for m in models],'legacy_preserved':len(baseline),'original_hashes_unchanged':before==after,
                      'selected_versions':resolved},ensure_ascii=True))


if __name__=='__main__':main()
