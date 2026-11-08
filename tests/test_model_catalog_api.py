"""HTTP shape/pagination checks for the catalog router, isolated from live projects."""
import importlib
import json
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from services.orchestrator.catalog_api import make_catalog_router
from services.orchestrator.model_catalog import ModelCatalog
from tests.test_model_catalog import checkpoint, index_file, fake_cpu_probe


@pytest.fixture
def api(tmp_path,monkeypatch):
    root=tmp_path/'roots';run=root/'twilight_pony_singing_r1';run.mkdir(parents=True)
    (run/'model_info.json').write_text(json.dumps({'embedder_model':'contentvec','speakers_id':1}))
    for epoch in (200,225,250):checkpoint(run/f'{epoch}.pth',epoch,epoch*58)
    index_file(run/(run.name+'.index'))
    catalog=ModelCatalog(tmp_path/'db.sqlite',approved_roots=[root],legacy_models=[],resource_config={});catalog.scan()
    monkeypatch.setattr(catalog,'_cpu_probe',fake_cpu_probe)
    monkeypatch.setattr('services.orchestrator.catalog_api.ModelCatalog',lambda _:catalog)
    app=FastAPI();app.include_router(make_catalog_router(SimpleNamespace(path=catalog.path)))
    @app.exception_handler(ValueError)
    async def bad(_,error):return JSONResponse({'detail':str(error)},400)
    @app.exception_handler(KeyError)
    async def missing(_,error):return JSONResponse({'detail':'Not found'},404)
    return TestClient(app),catalog


def test_pagination_voice_run_version_and_validation(api):
    client,catalog=api
    response=client.get('/api/v1/catalog/models',params={'limit':2,'offset':1});assert response.status_code==200
    page=response.json();assert page['total']==3 and len(page['models'])==2
    model=page['models'][0]
    assert model['epoch']==225 and model['model_hash'] is None
    voice=client.get('/api/v1/catalog/voices').json()['voices'][0]
    assert client.get('/api/v1/catalog/runs',params={'voice_id':voice['id']}).json()['runs'][0]['id']==model['run_id']
    checked=client.post(f"/api/v1/catalog/models/{model['id']}/validate").json()
    assert checked['status']=='ready' and checked['model_hash']
    assert checked['speaker_ids']==[0] and checked['embedding_speaker_capacity']==109
    indices=client.get(f"/api/v1/catalog/models/{model['id']}/indices").json()['indices']
    assert indices[0]['compatibility']['status']=='unknown'
    bound=client.post(f"/api/v1/catalog/models/{model['id']}/bind-index",json={'index_id':indices[0]['id']})
    assert bound.status_code==200 and bound.json()['index_match']['status']=='verified'
    updated=client.patch(f"/api/v1/catalog/voices/{voice['id']}",json={'favorite':True,'alias':'自定义声音'}).json()
    assert updated['favorite'] and updated['alias']=='自定义声音'
    default=client.post(f"/api/v1/catalog/voices/{voice['id']}/default",json={'model_id':model['id']}).json()
    assert default['default_model_id']==model['id']


def test_http_constraints_and_nonexistent_ids(api):
    client,_=api
    assert client.get('/api/v1/catalog/models',params={'limit':100000}).status_code==422
    assert client.get('/api/v1/catalog/models/missing').status_code==404
    assert client.post('/api/v1/catalog/import-batch',json={'paths':[]}).status_code==400
    assert client.post('/api/v1/catalog/scan',json={'roots':['C:/Windows']}).status_code==400
    assert client.get('/api/v1/catalog/scans/latest').status_code==200


def test_generated_catalog_types_match_schema_hash():
    import hashlib
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]/'packages/contracts'
    digest=hashlib.sha256((root/'model-catalog.schema.json').read_bytes()).hexdigest()
    for path in (root/'model-catalog.types.ts',root/'model_catalog_types.py'):
        assert digest in path.read_text(encoding='utf-8').splitlines()[0]
