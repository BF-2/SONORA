"""Catalog security/state contracts; synthetic metadata ZIPs never run inference."""
import copy
import json
import os
from pathlib import Path
import pickle
import shutil
import sqlite3
import struct
import threading
import zipfile

import pytest
from services.orchestrator.model_catalog import ModelCatalog, sha256
from workers.catalog_probe import static_checkpoint, static_index, safe_pickle_metadata


class TensorFixture:pass


def _pickle(value):
    if isinstance(value,TensorFixture):
        return b'ctorch._utils\n_rebuild_tensor_v2\n'+_pickle((None,0,(109,256),(256,1),False,{}))+b'R'
    if value is None:return b'N'
    if value is True:return b'\x88'
    if value is False:return b'\x89'
    if isinstance(value,str):
        data=value.encode();return b'X'+struct.pack('<I',len(data))+data
    if isinstance(value,int):return b'J'+struct.pack('<i',value)
    if isinstance(value,list):return b']('+b''.join(_pickle(x) for x in value)+b'e'
    if isinstance(value,tuple):return b'('+b''.join(_pickle(x) for x in value)+b't'
    if isinstance(value,dict):return b'}('+b''.join(_pickle(k)+_pickle(v) for k,v in value.items())+b'u'
    raise TypeError(type(value))


def checkpoint(path,epoch=200,step=11600,training=False,model_name=None,unknown=False):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    value={'model':{},'optimizer':{},'iteration':step} if training else {
        'weight':{'emb_g.weight':TensorFixture()},'config':[1]*15+[40000],
        'version':'v2','f0':1,'speakers_id':1,'embedder_model':'contentvec',
        'model_name':model_name or path.parent.name,
    }
    if not training and not unknown:value.update(epoch=epoch,step=step)
    with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('archive/data.pkl',b'\x80\x02'+_pickle(value)+b'.')
        archive.writestr('archive/data/0',b'not-real-tensor-storage')
    return path


def index_file(path,dimension=768):
    path=Path(path);path.write_bytes(b'IwFl'+struct.pack('<i',dimension)+b'\0'*56);return path


@pytest.fixture
def catalog(tmp_path):
    roots=tmp_path/'models';run=roots/'twilight_pony_singing_r1';run.mkdir(parents=True)
    (run/'model_info.json').write_text(json.dumps({'speakers_id':1,'embedder_model':'contentvec'}))
    (run/'config.json').write_text(json.dumps({'data':{'sample_rate':40000},'model':{'spk_embed_dim':109}}))
    return ModelCatalog(tmp_path/'project.sqlite',approved_roots=[roots],legacy_models=[],resource_config={}),run


def fake_cpu_probe(path,index=False):
    # This metadata fixture proves catalog routing only, never a valid neural weight.
    if index:return {'ok':True,**static_index(path),'fully_validated':True,'ntotal':8,'trained':True}
    return {'ok':True,**static_checkpoint(path)}


def test_initial_scan_all_epochs_training_separate_and_no_tensor_load(catalog,monkeypatch):
    c,run=catalog
    for epoch in (200,225,250,275,300):checkpoint(run/f'voice_{epoch}e_{epoch*58}s.pth',epoch,epoch*58)
    checkpoint(run/'G_52000.pth',step=52000,training=True)
    checkpoint(run/'D_52000.pth',step=52000,training=True)
    checkpoint(run/'unlabelled.pth',unknown=True)
    index_file(run/(run.name+'.index'))
    monkeypatch.setattr('services.orchestrator.model_catalog.sha256',lambda *a,**k: (_ for _ in ()).throw(AssertionError('Initial scan must not hash full files')))
    scan=c.scan();assert scan['state']=='succeeded' and scan['tensor_storage_loaded'] is False
    models=c.list_models(limit=100)['models']
    assert {m['epoch'] for m in models}=={None,200,225,250,275,300}
    assert all(m['model_hash'] is None for m in models)
    assert all(m['speaker_ids']==[0] and m['embedding_speaker_capacity']==109 for m in models)
    checkpoints=c.list_models(kind='training_checkpoint')['models']
    assert len(checkpoints)==2 and all(m['status']=='training_only' for m in checkpoints)
    assert all(m['epoch'] is None and m['global_step'] is None and m['training_iteration']==52000 for m in checkpoints)
    assert all(m['filename_counter_hint']==52000 for m in checkpoints)
    assert c.list_models(q='Epoch 225')['total']==1


def test_static_pickle_is_not_executed_and_danger_is_quarantined(catalog,tmp_path):
    c,run=catalog;marker=tmp_path/'must-not-exist'
    class Dangerous:
        def __reduce__(self):return eval,(f"open({str(marker)!r},'w').write('bad')",)
    path=run/'evil.pth'
    with zipfile.ZipFile(path,'w') as archive:archive.writestr('archive/data.pkl',pickle.dumps(Dangerous(),protocol=2))
    scan=c.scan();assert scan['counts']['unknown']==1
    bad=c.list_models(kind='unknown')['models'][0]
    assert bad['status']=='quarantined' and not marker.exists()
    with pytest.raises(ValueError):c.validate(bad['id'])
    assert not marker.exists()


def test_forged_plain_tensor_shape_does_not_count_as_inference_export(tmp_path):
    p=tmp_path/'forged.pth'
    with zipfile.ZipFile(p,'w') as archive:
        archive.writestr('archive/data.pkl',pickle.dumps({'weight':{'emb_g.weight':{'__tensor__':True,'shape':[109,256]}},'config':[1]*15+[40000]},protocol=2))
    with pytest.raises(ValueError,match='embedding'):static_checkpoint(p)


def test_zip_path_escape_and_metadata_bomb_rejected(tmp_path):
    for filename,member,data in [('escape.pth','../data.pkl',b'N.'),('bomb.pth','archive/data.pkl',b'X'*5_000_000)]:
        path=tmp_path/filename
        with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as archive:archive.writestr(member,data)
        with pytest.raises(ValueError):static_checkpoint(path)


def test_incremental_scan_skips_unchanged_metadata_and_preserves_history(catalog,monkeypatch):
    c,run=catalog;path=checkpoint(run/'version.pth');c.scan()
    old=c.list_models()['models'][0]
    monkeypatch.setattr('services.orchestrator.model_catalog.static_checkpoint',lambda p: (_ for _ in ()).throw(AssertionError('unchanged file reparsed')))
    assert c.scan()['counts']['inference_export']==1
    path.unlink();c.scan()
    assert c.get_model(old['id'])['status']=='missing'
    assert c.get_model(old['id'])['epoch']==200


def test_bounded_and_cancelled_scan_does_not_mark_unvisited_missing(catalog):
    c,run=catalog
    for n in range(5):checkpoint(run/f'v{n}.pth',epoch=n)
    first=c.scan(max_files=2);assert first['state']=='partial' and first['scanned_files']==2
    ids=[m['id'] for m in c.list_models()['models']]
    cancelled=threading.Event();cancelled.set();last=c.scan(cancel=cancelled)
    assert last['state']=='cancelled'
    assert all(c.get_model(identifier)['status']=='discovered' for identifier in ids)


def test_scan_rejects_unapproved_root_and_symbolic_escape(catalog,tmp_path):
    c,run=catalog;outside=tmp_path/'outside';outside.mkdir();checkpoint(outside/'foreign.pth')
    with pytest.raises(ValueError):c.scan(roots=[outside])
    try:(run/'escape.pth').symlink_to(outside/'foreign.pth')
    except OSError:pytest.skip('Symlink privilege unavailable')
    assert c.scan()['scanned_files']==0


def test_changed_known_weight_quarantined_new_identity_discovered(catalog,monkeypatch):
    c,run=catalog;path=checkpoint(run/'version.pth');c.scan()
    monkeypatch.setattr(c,'_cpu_probe',fake_cpu_probe)
    old=c.validate(c.list_models()['models'][0]['id']);old_hash=old['model_hash']
    checkpoint(path,epoch=225,step=13050)
    with pytest.raises(ValueError,match='哈希'):c.validate(old['id'])
    c.scan();models=c.list_models(limit=100)['models']
    assert len(models)==2
    assert c.get_model(old['id'])['model_hash']==old_hash
    fresh=next(m for m in models if m['id']!=old['id']);assert fresh['epoch']==225
    assert c.validate(fresh['id'])['model_hash']!=old_hash


def test_unhashed_changed_file_cannot_redefine_discovered_id(catalog,monkeypatch):
    c,run=catalog;path=checkpoint(run/'version.pth');c.scan();old=c.list_models()['models'][0]
    checkpoint(path,epoch=300,step=17400)
    monkeypatch.setattr(c,'_cpu_probe',fake_cpu_probe)
    with pytest.raises(ValueError,match='扫描后'):c.validate(old['id'])
    assert c.get_model(old['id'])['epoch']==200


def test_dedup_hash_keeps_all_old_ids_and_paths(catalog,monkeypatch):
    c,run=catalog;first=checkpoint(run/'one.pth');shutil.copyfile(first,run/'copy.pth');c.scan()
    monkeypatch.setattr(c,'_cpu_probe',fake_cpu_probe)
    a,b=c.list_models()['models'];a=c.validate(a['id']);b=c.validate(b['id'])
    assert a['model_hash']==b['model_hash'] and b['alias_of']==a['id']
    assert c.list_models()['total']==1
    assert c.list_models(include_aliases=True)['total']==2
    assert c.resolve_model(b['id'],index_id='')['id']==b['id']
    assert len(c.get_model(a['id'])['original_paths'])==2


def test_index_same_folder_is_not_matching_source(catalog,monkeypatch):
    c,run=catalog;checkpoint(run/'voice.pth');index_file(run/'unrelated.index');c.scan()
    monkeypatch.setattr(c,'_cpu_probe',fake_cpu_probe)
    model=c.validate(c.list_models()['models'][0]['id']);index=c.model_indices(model['id'])['indices'][0]
    assert index['compatibility']['status']=='unknown'
    with pytest.raises(ValueError,match='未验证'):c.resolve_model(model['id'],index['id'])
    bound=c.bind_index(model['id'],index['id'])
    assert bound['index_match']['status']=='verified'
    assert bound['index_match']['evidence'][0]['kind']=='user_explicit_binding'


def test_named_training_run_index_dimension_and_encoder_verified(catalog,monkeypatch):
    c,run=catalog;checkpoint(run/'voice.pth');index_file(run/(run.name+'.index'));index_file(run/'wrong-dim.index',256);c.scan()
    monkeypatch.setattr(c,'_cpu_probe',fake_cpu_probe)
    model=c.list_models()['models'][0]
    indices=c.model_indices(model['id'])['indices'];named=next(i for i in indices if i['content_dim']==768)
    assert named['compatibility']['status']=='unknown', 'A matching filename is not proof of training provenance'
    c.bind_index(model['id'],named['id'])
    resolved=c.resolve_model(model['id'])
    assert resolved['index_match']['status']=='verified' and resolved['index_match']['fully_validated']
    bad=next(x for x in c.model_indices(model['id'])['indices'] if x['content_dim']==256)
    with pytest.raises(ValueError,match='维度'):c.bind_index(model['id'],bad['id'])


def test_index_provenance_transfers_only_across_same_declared_run(catalog,monkeypatch):
    c,run=catalog;checkpoint(run/'a.pth',200);checkpoint(run/'b.pth',225)
    checkpoint(run/'foreign.pth',250,model_name='another_training_run')
    index_file(run/(run.name+'.index'));c.scan();monkeypatch.setattr(c,'_cpu_probe',fake_cpu_probe)
    models=c.list_models()['models'];a=next(m for m in models if m['epoch']==200);b=next(m for m in models if m['epoch']==225)
    foreign=next(m for m in models if m['epoch']==250);index=c.model_indices(a['id'])['indices'][0]
    c.bind_index(a['id'],index['id'])
    resolved=c.resolve_model(b['id'])
    assert resolved['index_match']['status']=='verified'
    assert resolved['index_match']['evidence'][0]['kind']=='shared_registered_training_run'
    assert resolved['index_match']['evidence'][0]['anchor_model_id']==a['id']
    assert c.model_indices(foreign['id'])['indices'][0]['compatibility']['status']=='unknown'
    with pytest.raises(ValueError,match='未验证'):c.resolve_model(foreign['id'],index['id'])


def test_empty_untrained_index_never_becomes_ready(catalog,monkeypatch):
    c,run=catalog;index_file(run/'empty.index');c.scan();index=c.list_models(kind='index')['models'][0]
    monkeypatch.setattr(c,'_cpu_probe',lambda *a,**k:{'ok':True,'content_dim':768,'ntotal':0,'trained':False,'fully_validated':True})
    with pytest.raises(ValueError,match='没有特征'):c.validate(index['id'])
    assert c.get_model(index['id'])['status']=='quarantined'


def test_alias_favorite_and_default_do_not_replace_old_selection(catalog,monkeypatch):
    c,run=catalog;checkpoint(run/'a.pth',200);checkpoint(run/'b.pth',225);c.scan()
    monkeypatch.setattr(c,'_cpu_probe',fake_cpu_probe)
    a,b=c.list_models()['models'];a=c.validate(a['id']);b=c.validate(b['id'])
    selection=c.record_selection(a['id'],project_id='immutable-project',index_id='')
    c.update_voice(a['voice_id'],{'alias':'我的声音','favorite':True,'tags':['歌声']})
    c.set_default(a['voice_id'],b['id'])
    assert selection['model_artifact_id']==a['id'] and selection['model_hash']==a['model_hash']
    voice=c.list_voices(q='我的声音',favorite=True)['voices'][0]
    assert voice['default_model_id']==b['id']
    assert c.get_model(a['id'])['id']==a['id']


def test_migration_preserves_main_database_and_legacy_id(tmp_path):
    path=tmp_path/'main.sqlite';run=tmp_path/'models'/'legacy';run.mkdir(parents=True);weight=checkpoint(run/'legacy.pth')
    with sqlite3.connect(path) as db:
        db.executescript('PRAGMA user_version=1;CREATE TABLE projects(id TEXT PRIMARY KEY,body TEXT);')
        db.execute('INSERT INTO projects VALUES(?,?)',('project-old','{"model_id":"old-29-id","notes":[1,2,3]}'))
    legacy={'id':'old-29-id','path':str(weight),'origin_path':str(weight),'model_hash':sha256(weight),'name':'legacy model'}
    c=ModelCatalog(path,approved_roots=[run.parent],legacy_models=[legacy],resource_config={})
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==1
        assert json.loads(db.execute('SELECT body FROM projects').fetchone()[0])['notes']==[1,2,3]
    assert c.get_model('old-29-id')['model_hash']==legacy['model_hash']
    assert list((tmp_path/'backups').glob('catalog-before-v1-*.sqlite'))
    with sqlite3.connect(path) as db:db.execute("UPDATE catalog_meta SET value='999' WHERE key='schema_version'")
    with pytest.raises(ValueError):ModelCatalog(path,approved_roots=[run.parent],legacy_models=[],resource_config={})


def test_relink_requires_matching_hash_and_retains_identity(catalog,monkeypatch):
    c,run=catalog;original=checkpoint(run/'version.pth');c.scan();monkeypatch.setattr(c,'_cpu_probe',fake_cpu_probe)
    model=c.validate(c.list_models()['models'][0]['id'])
    moved=run/'moved.pth';original.rename(moved);c.scan()
    assert c.get_model(model['id'])['status']=='missing'
    linked=c.relink(model['id'],moved)
    assert linked['id']==model['id'] and linked['model_hash']==model['model_hash']
    other=checkpoint(run/'other.pth',epoch=225)
    with pytest.raises(ValueError,match='哈希不匹配'):c.relink(model['id'],other)


def test_initial_and_explicit_scans_coalesce_without_tensor_loading(catalog,monkeypatch):
    c,run=catalog;checkpoint(run/'version.pth')
    entered=threading.Event();release=threading.Event();original=c.scan
    def bounded_scan(**options):
        entered.set();assert release.wait(5);return original(**options)
    monkeypatch.setattr(c,'scan',bounded_scan)
    initial=c.ensure_initial_scan();assert entered.wait(2)
    duplicate=c.start_scan();assert duplicate['id']==initial['id'] and duplicate['coalesced']
    assert c.ensure_initial_scan()['id']==initial['id']
    release.set()
    import time
    deadline=time.monotonic()+5
    while c.latest_scan()['state'] in ('running','queued') and time.monotonic()<deadline:time.sleep(.02)
    assert c.latest_scan()['state']=='succeeded'
    assert c.ensure_initial_scan()['id']==initial['id']


def test_missing_file_returning_with_same_identity_is_discovered(catalog):
    c,run=catalog;path=checkpoint(run/'version.pth');c.scan()
    model=c.list_models()['models'][0]
    moved=run/'temporarily-away.bin';path.rename(moved);c.scan()
    assert c.get_model(model['id'])['status']=='missing'
    moved.rename(path);c.scan()
    assert c.get_model(model['id'])['status']=='discovered'
