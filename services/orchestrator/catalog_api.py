"""Authenticated by the existing /api middleware; no independent open service."""
from fastapi import APIRouter, HTTPException, Query
from .model_catalog import ModelCatalog


def make_catalog_router(store):
    catalog=ModelCatalog(store.path)
    router=APIRouter(prefix='/api/v1/catalog',tags=['model-catalog'])
    router.catalog=catalog

    @router.get('/voices')
    def voices(q:str='',favorite:bool|None=None,offset:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=250)):
        return catalog.list_voices(q,favorite,offset,limit)

    @router.get('/runs')
    def runs(voice_id:str|None=None,q:str='',offset:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=250)):
        return catalog.list_runs(voice_id,q,offset,limit)

    @router.get('/models')
    def models(q:str='',voice_id:str|None=None,run_id:str|None=None,status:str|None=None,kind:str='inference_export',
               offset:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=250),favorite:bool|None=None,include_aliases:bool=False,sort:str='version'):
        return catalog.list_models(q,voice_id,run_id,status,kind,offset,limit,favorite,include_aliases,sort)

    @router.get('/models/{identifier}')
    def model(identifier:str):return catalog.get_model(identifier)

    @router.get('/models/{identifier}/indices')
    def indices(identifier:str):return catalog.model_indices(identifier)

    @router.post('/scan')
    def scan(body:dict|None=None):
        body=body or {}
        if set(body)-{'roots','max_files','max_seconds'}:raise ValueError('未知扫描选项')
        roots=body.get('roots')
        if roots is not None and (not isinstance(roots,list) or len(roots)>8 or any(not isinstance(x,str) for x in roots)):raise ValueError('扫描根列表无效')
        for root in roots or []:catalog._safe_path(root,scan=True)
        return catalog.start_scan(**body)

    @router.get('/scans/latest')
    def latest_scan():return catalog.latest_scan() or {'state':'not_started','scanned_files':0,'counts':{}}

    @router.get('/scans/{identifier}')
    def scan_status(identifier:str):return catalog.scan_status(identifier)

    @router.post('/scans/{identifier}/cancel')
    def cancel_scan(identifier:str):return catalog.cancel_scan(identifier)

    @router.post('/models/{identifier}/validate')
    def validate(identifier:str):return catalog.validate(identifier)

    @router.patch('/voices/{identifier}')
    def edit_voice(identifier:str,body:dict):return catalog.update_voice(identifier,body)

    @router.post('/voices/{identifier}/default')
    def default_model(identifier:str,body:dict):return catalog.set_default(identifier,body['model_id'])

    @router.post('/models/{identifier}/bind-index')
    def bind_index(identifier:str,body:dict):return catalog.bind_index(identifier,body['index_id'])

    @router.post('/models/{identifier}/relink')
    def relink(identifier:str,body:dict):return catalog.relink(identifier,body['path'])

    @router.post('/import-batch')
    def import_batch(body:dict):return catalog.import_batch(body.get('paths'))

    return router
