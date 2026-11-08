"""Incremental, read-only RVC inventory with independent SQLite schema.

Initial discovery reads stat/config and a bounded symbolic pickle description,
never tensor storage. Hashing and restricted CPU loading happen on selection.
"""
from __future__ import annotations
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from workers.catalog_probe import static_checkpoint, static_index, MAX_MODEL_BYTES
from .config import ROOT, resources, ALLOWED_ROOTS

CATALOG_VERSION=1
PART_DEFAULT_ROOT=ROOT/'resources/models/haysay'
_SCANS={}
_SCAN_LOCK=threading.RLock()
_INITIAL_SCANS=set()


def utc():return datetime.now(timezone.utc).isoformat()
def identity(prefix,value):return prefix+'_'+hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:24]
def pathkey(path):return os.path.normcase(str(Path(path).resolve()))
def signature(path):
    stat=Path(path).stat();return f'{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}'
def encoded(value):return json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':'))
def sha256(path,cancel=None,deadline=None):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(4*1024*1024),b''):
            if cancel and cancel.is_set():raise ValueError('操作已取消')
            if deadline and time.monotonic()>deadline:raise ValueError('哈希计算超时')
            digest.update(block)
    return digest.hexdigest()


def read_metadata(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>1024*1024:return {}
    try:
        value=json.loads(path.read_text(encoding='utf-8-sig'))
        return value if isinstance(value,dict) else {}
    except (ValueError,OSError):return {}


def trained_metadata(raw,run):
    """Export scalar evidence outranks embedding capacity and filename hints."""
    result=copy.deepcopy(raw);info=run.get('model_info',{});capacity=int(raw.get('embedding_speaker_capacity') or 0)
    profile=info.get('inference_profile',{})
    pinned=bool(profile.get('model_sha256') and profile['model_sha256']==(raw.get('model_hash') or raw.get('sha256'))
                and profile.get('status')=='verified_local_inference')
    count=raw.get('speakers_id');origin='checkpoint'
    if type(count) is not int:
        count=info.get('speakers_id');origin='verified_training_metadata'
    ids=raw.get('speaker_ids')
    if isinstance(ids,list) and ids and all(type(x) is int and 0<=x<capacity for x in ids):
        result.update(speaker_ids=sorted(set(ids)),speaker_ids_trained=sorted(set(ids)),speaker_metadata_status='checkpoint')
    elif type(count) is int and 0<count<=capacity:
        result.update(speaker_ids=list(range(count)),speaker_ids_trained=list(range(count)),speaker_metadata_status=origin)
    elif pinned and isinstance(profile.get('supported_speaker_ids'),list) and profile['supported_speaker_ids'] and all(type(x) is int and 0<=x<capacity for x in profile['supported_speaker_ids']):
        supported=sorted(set(profile['supported_speaker_ids']))
        result.update(speaker_ids=supported,speaker_ids_trained=supported,speaker_metadata_status='verified_inference_profile',
                      speaker_metadata_note='Locally exercised inference IDs only; embedding capacity does not establish training coverage.')
    else:result.update(speaker_ids=[],speaker_ids_trained=[],speaker_metadata_status='unknown')
    embedder=raw.get('embedder_model') or info.get('embedder_model') or (profile.get('embedder_model') if pinned else None)
    if embedder in ('contentvec','contentvec_base'):
        result.update(embedder_model='contentvec',embedder_verified=True,
                      embedder_evidence={'source':'checkpoint' if raw.get('embedder_model') else 'training_metadata' if info.get('embedder_model') else 'hash_pinned_inference_profile','path':run.get('metadata_path')})
    else:result.update(embedder_model=embedder,embedder_verified=False,embedder_evidence={'source':'unknown' if not embedder else 'unsupported_encoder'})
    result['capabilities']={'voice_conversion':True,'singing_conversion':True,'external_f0':bool(raw.get('f0_conditioned')),
        'svs':False,'streaming':False,'local_segment_render':False,'training':False,'cancel_mode':'terminate_process',
        'speaker_ids':result['speaker_ids'],'embedder':['contentvec'] if result['embedder_verified'] else []}
    return result


class ModelCatalog:
    def __init__(self,db_path,approved_roots=None,legacy_models=None,resource_config=None):
        self.path=str(Path(db_path).resolve());self.config=copy.deepcopy(resource_config if resource_config is not None else resources())
        configured=self.config.get('catalog',{}).get('roots') or [str(PART_DEFAULT_ROOT)]
        self.approved_roots=[Path(p).resolve() for p in (approved_roots if approved_roots is not None else configured)]
        self.lock=threading.RLock()
        self._migrate()
        self._seed_legacy(legacy_models if legacy_models is not None else self.config.get('models',[]))

    def connect(self):
        db=sqlite3.connect(self.path,timeout=30);db.row_factory=sqlite3.Row;return db

    def _migrate(self):
        path=Path(self.path);path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalog_meta'").fetchone()
            version=int(db.execute("SELECT value FROM catalog_meta WHERE key='schema_version'").fetchone()[0]) if exists else 0
            if version>CATALOG_VERSION:raise ValueError('模型目录来自更新版本，拒绝写入')
            if version==CATALOG_VERSION:return
            original_user_version=db.execute('PRAGMA user_version').fetchone()[0]
            backup_dir=path.parent/'backups';backup_dir.mkdir(exist_ok=True)
            backup=backup_dir/('catalog-before-v1-'+uuid.uuid4().hex[:10]+'.sqlite')
            with sqlite3.connect(backup) as destination:db.backup(destination)
            db.executescript('''
                CREATE TABLE IF NOT EXISTS catalog_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS catalog_voices(id TEXT PRIMARY KEY,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS catalog_runs(id TEXT PRIMARY KEY,voice_id TEXT NOT NULL,path TEXT NOT NULL UNIQUE,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS catalog_artifacts(id TEXT PRIMARY KEY,kind TEXT NOT NULL,voice_id TEXT,run_id TEXT,status TEXT NOT NULL,hash TEXT,body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS catalog_artifacts_hash ON catalog_artifacts(hash);
                CREATE INDEX IF NOT EXISTS catalog_artifacts_run ON catalog_artifacts(run_id,kind);
                CREATE TABLE IF NOT EXISTS catalog_paths(artifact_id TEXT NOT NULL,path TEXT NOT NULL,signature TEXT NOT NULL,last_seen TEXT,PRIMARY KEY(artifact_id,path));
                CREATE INDEX IF NOT EXISTS catalog_paths_path ON catalog_paths(path);
                CREATE TABLE IF NOT EXISTS catalog_bindings(model_id TEXT NOT NULL,index_id TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(model_id,index_id));
                CREATE TABLE IF NOT EXISTS catalog_scans(id TEXT PRIMARY KEY,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS catalog_selections(id TEXT PRIMARY KEY,model_id TEXT NOT NULL,body TEXT NOT NULL);
            ''')
            db.execute("INSERT OR REPLACE INTO catalog_meta VALUES('schema_version',?)",(str(CATALOG_VERSION),))
            db.execute("INSERT OR REPLACE INTO catalog_meta VALUES('migration_backup',?)",(str(backup),))
            assert db.execute('PRAGMA user_version').fetchone()[0]==original_user_version

    def _get(self,table,identifier):
        if table not in ('catalog_artifacts','catalog_voices','catalog_runs','catalog_scans'):raise ValueError('Invalid table')
        with self.connect() as db:row=db.execute(f'SELECT body FROM {table} WHERE id=?',(identifier,)).fetchone()
        if not row:raise KeyError(identifier)
        return json.loads(row[0])

    def _put(self,value):
        with self.connect() as db:
            db.execute('INSERT INTO catalog_artifacts VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,voice_id=excluded.voice_id,run_id=excluded.run_id,status=excluded.status,hash=excluded.hash,body=excluded.body',
                (value['id'],value['kind'],value.get('voice_id'),value.get('run_id'),value['status'],value.get('model_hash') or value.get('sha256'),encoded(value)))
        return value

    def _safe_path(self,value,scan=False):
        supplied=Path(value)
        resolved=supplied.resolve(strict=True)
        roots=self.approved_roots if scan else self.approved_roots+[Path(p).resolve() for p in ALLOWED_ROOTS]
        if not any(resolved==r or resolved.is_relative_to(r) for r in roots):raise ValueError('路径不在已授权的模型目录内')
        if resolved.is_file() and resolved.stat().st_size>MAX_MODEL_BYTES:raise ValueError('文件超过 2 GiB 预算')
        return resolved

    def _run(self,directory):
        directory=Path(directory).resolve();key=pathkey(directory);run_id=identity('run',key)
        info=read_metadata(directory/'model_info.json');config=read_metadata(directory/'config.json')
        name=directory.name
        suggested=re.split(r'_pony_(?:singing|speech)|_rvc(?:$|_)',name,1)[0] or name
        voice_id=identity('voice',suggested.casefold())
        with self.connect() as db:
            prior=db.execute('SELECT body FROM catalog_runs WHERE id=?',(run_id,)).fetchone()
            if prior:voice_id=json.loads(prior[0])['voice_id']
            voice=db.execute('SELECT body FROM catalog_voices WHERE id=?',(voice_id,)).fetchone()
            if not voice:
                value={'id':voice_id,'name':suggested.replace('_',' '),'alias':'','description':'','tags':[],
                    'favorite':False,'last_used_at':None,'default_model_id':None,'grouping_evidence':'filename_inferred','created_at':utc(),'updated_at':utc()}
                db.execute('INSERT INTO catalog_voices VALUES(?,?)',(voice_id,encoded(value)))
            value={'id':run_id,'voice_id':voice_id,'name':name,'path':str(directory),'grouping_evidence':'filename_inferred',
                'model_info':info,'metadata_path':str(directory/'model_info.json') if info else None,
                'config_path':str(directory/'config.json') if config else None,'config':config,
                'embedder_model':info.get('embedder_model'),'sample_rate':config.get('data',{}).get('sample_rate'),
                'created_at':json.loads(prior[0]).get('created_at',utc()) if prior else utc(),'updated_at':utc()}
            db.execute('INSERT INTO catalog_runs VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                       (run_id,voice_id,key,encoded(value)))
        return value

    def _path(self,identifier,path,scan_id=None):
        with self.connect() as db:db.execute('INSERT OR REPLACE INTO catalog_paths VALUES(?,?,?,?)',(identifier,pathkey(path),signature(path),scan_id))

    def _seed_legacy(self,models):
        for legacy in models:
            if not legacy.get('id') or not legacy.get('path'):continue
            try:self._get('catalog_artifacts',legacy['id']);continue
            except KeyError:pass
            path=Path(legacy['path']);origin=Path(legacy.get('origin_path') or path)
            run=self._run(origin.parent)
            raw={};reason=None
            try:raw=static_checkpoint(path)
            except (ValueError,OSError,KeyError,TypeError) as error:reason=str(error)
            metadata=trained_metadata({**raw,'model_hash':legacy.get('model_hash')},run) if raw.get('kind')=='inference_export' else {}
            value={**copy.deepcopy(legacy),**metadata,'id':legacy['id'],'model_artifact_id':legacy['id'],
                'kind':'inference_export','voice_id':run['voice_id'],'run_id':run['id'],'name':legacy.get('name',path.stem),
                'path':str(path),'origin_path':str(origin),'status':'ready' if metadata and path.is_file() else 'missing' if not path.is_file() else 'quarantined',
                'reason':reason,'metadata_evidence':'legacy_registered','legacy_id':legacy['id'],'created_at':utc(),'updated_at':utc(),
                'validated_at':None,'selected_index_id':None,'version_label':self._version(metadata,legacy['id'])}
            self._put(value)
            for candidate in {path,origin}:
                if candidate.is_file():self._path(value['id'],candidate)
            index_path=legacy.get('index_path')
            if index_path and Path(index_path).is_file():
                try:
                    index=self._register_path(Path(index_path),run=run,known_hash=legacy.get('index_hash'))
                    original_index=origin.parent/Path(index_path).name
                    index.update(origin_path=str(original_index) if original_index.is_file() else index.get('origin_path'),legacy_association=True,
                                 run_link_verified=True,provenance='legacy_manifest')
                    self._put(index)
                    if original_index.is_file():self._path(index['id'],original_index)
                    evidence={'kind':'legacy_manifest','legacy_model_id':legacy['id'],'model_hash':legacy.get('model_hash'),'index_hash':legacy.get('index_hash')}
                    self._bind_record(value['id'],index['id'],evidence)
                    value['selected_index_id']=index['id'];self._put(value)
                except (ValueError,OSError):pass

    @staticmethod
    def _version(metadata,identifier):
        values=[]
        if metadata.get('epoch') is not None:values.append('Epoch '+str(metadata['epoch']))
        if metadata.get('global_step') is not None:values.append('Step '+str(metadata['global_step']))
        return ' · '.join(values) if values else '未知 Epoch · '+identifier[-8:]

    def _register_path(self,path,scan_id=None,run=None,known_hash=None):
        path=Path(path);key=pathkey(path);stamp=signature(path)
        with self.connect() as db:
            existing=db.execute('SELECT a.body,p.signature FROM catalog_paths p JOIN catalog_artifacts a ON a.id=p.artifact_id WHERE p.path=? ORDER BY a.rowid DESC',(key,)).fetchall()
        for row in existing:
            prior=json.loads(row[0])
            if row[1]==stamp:
                if prior['status']=='missing':
                    prior.update(status='training_only' if prior['kind']=='training_checkpoint' else 'discovered' if prior['kind'] in ('index','inference_export') else 'quarantined',
                                 reason='文件已重新发现；选中后校验原哈希' if prior['kind'] in ('index','inference_export') else prior.get('reason'),updated_at=utc())
                    self._put(prior)
                self._path(prior['id'],path,scan_id);return prior
        for row in existing:
            prior=json.loads(row[0]);prior.update(status='quarantined',reason='文件内容或时间戳变化；原版本身份及历史保留，请校验新版本',updated_at=utc());self._put(prior)
        run=run or self._run(path.parent);identifier=identity('index' if path.suffix.lower()=='.index' else 'model',key+'|'+stamp)
        try:
            raw=static_index(path) if path.suffix.lower()=='.index' else static_checkpoint(path)
            if raw['kind']=='inference_export':raw=trained_metadata(raw,run)
        except Exception as error:raw={'kind':'unknown','status':'quarantined','reason':str(error)[:1000],'epoch':None,'global_step':None}
        value={**raw,'id':identifier,'model_artifact_id':identifier,'name':path.stem,'filename':path.name,'path':str(path),
            'origin_path':str(path),'voice_id':run['voice_id'],'run_id':run['id'],'file_size':path.stat().st_size,
            'file_mtime_ns':path.stat().st_mtime_ns,'stat_signature':stamp,'model_hash':known_hash if raw['kind']=='inference_export' else None,
            'sha256':known_hash,'license':'unconfirmed','origin_url':None,'created_at':utc(),'updated_at':utc(),
            'validated_at':None,'selected_index_id':None,'reason':raw.get('reason'),'metadata_evidence':raw.get('metadata_evidence','static_header'),
            'version_label':self._version(raw,identifier)}
        if raw['kind']=='index':
            value.update(embedder_model=run.get('embedder_model'),run_name_match=self._index_named_for_run(path,run),run_link_verified=False,
                         provenance='filename_inferred' if self._index_named_for_run(path,run) else 'unknown')
        self._put(value);self._path(identifier,path,scan_id);return value

    @staticmethod
    def _index_named_for_run(path,run):
        stem=Path(path).stem.casefold();name=run['name'].casefold()
        return stem==name or bool(re.fullmatch(r'(?:added|trained)_.*_'+re.escape(name)+r'(?:_v[12])?',stem))

    def scan(self,roots=None,max_files=5000,max_seconds=30,cancel=None,scan_id=None):
        max_files=max(1,min(10_000,int(max_files)));max_seconds=max(.05,min(120,float(max_seconds)))
        chosen=[self._safe_path(p,scan=True) for p in (roots if roots is not None else self.approved_roots) if Path(p).exists()]
        started=time.monotonic();deadline=started+max_seconds;counts={};seen=set();processed=0;skipped=0;budget=False
        scan_id=scan_id or identity('scan',uuid.uuid4().hex)
        for root in chosen:
            if not root.is_dir():raise ValueError('扫描根必须为目录')
            directories=[root]
            for child in root.iterdir():
                if time.monotonic()>deadline or len(directories)>512:budget=True;break
                if child.is_dir() and not child.is_symlink() and not getattr(child,'is_junction',lambda:False)():directories.append(child)
            for directory in directories:
                if budget:break
                for path in directory.iterdir():
                    if (cancel and cancel.is_set()) or time.monotonic()>deadline or processed>=max_files:budget=True;break
                    if not path.is_file() or path.suffix.lower() not in ('.pth','.pt','.index'):continue
                    if path.is_symlink():skipped+=1;continue
                    try:
                        safe=self._safe_path(path,scan=True);value=self._register_path(safe,scan_id)
                        seen.add(pathkey(safe));processed+=1;counts[value['kind']]=counts.get(value['kind'],0)+1
                    except (OSError,ValueError):skipped+=1
                self._save_scan(scan_id,{'id':scan_id,'state':'running','scanned_files':processed,'counts':counts,'skipped':skipped,'roots':[str(r) for r in chosen],'started_at':utc(),'elapsed_seconds':time.monotonic()-started})
            if budget:break
        # A bounded/cancelled scan must never mark unvisited files missing.
        if not budget:
            with self.connect() as db:rows=db.execute('SELECT DISTINCT a.body FROM catalog_artifacts a JOIN catalog_paths p ON a.id=p.artifact_id').fetchall()
            for row in rows:
                value=json.loads(row[0]);origin=Path(value.get('origin_path') or value['path'])
                if any(origin.is_relative_to(r) for r in chosen) and not origin.is_file() and not Path(value['path']).is_file():
                    value.update(status='missing',reason='模型文件已移走；历史引用保留，可按哈希重新链接',updated_at=utc());self._put(value)
        result={'id':scan_id,'state':'cancelled' if cancel and cancel.is_set() else 'partial' if budget else 'succeeded',
            'scanned_files':processed,'counts':counts,'skipped':skipped,'roots':[str(r) for r in chosen],
            'budget_exhausted':budget,'elapsed_seconds':time.monotonic()-started,'tensor_storage_loaded':False,'hashing':'lazy_selected_only','finished_at':utc()}
        self._save_scan(scan_id,result);return result

    def _save_scan(self,identifier,value):
        with self.connect() as db:db.execute('INSERT OR REPLACE INTO catalog_scans VALUES(?,?)',(identifier,encoded(value)))

    def start_scan(self,**options):
        with _SCAN_LOCK:
            existing=next((identifier for path,identifier in _SCANS if path==self.path),None)
            if existing:return {**self.scan_status(existing),'coalesced':True}
            identifier=identity('scan',uuid.uuid4().hex);token=threading.Event()
            initial={'id':identifier,'state':'queued','scanned_files':0,'counts':{},'requested_options':options,'created_at':utc()};self._save_scan(identifier,initial)
            _SCANS[(self.path,identifier)]=token
        def work():
            try:self.scan(scan_id=identifier,cancel=token,**options)
            except Exception as error:self._save_scan(identifier,{**initial,'state':'failed','error':str(error)})
            finally:
                with _SCAN_LOCK:_SCANS.pop((self.path,identifier),None)
        threading.Thread(target=work,daemon=True,name='sonora-catalog-scan').start();return initial

    def latest_scan(self):
        with self.connect() as db:row=db.execute('SELECT body FROM catalog_scans ORDER BY rowid DESC LIMIT 1').fetchone()
        return json.loads(row[0]) if row else None

    def ensure_initial_scan(self,**options):
        """At most one initial metadata scan per process/database; explicit rescans remain available."""
        with _SCAN_LOCK:
            if self.path in _INITIAL_SCANS:return self.latest_scan()
            _INITIAL_SCANS.add(self.path)
            return self.start_scan(**options)

    def scan_status(self,identifier):return self._get('catalog_scans',identifier)
    def cancel_scan(self,identifier):
        state=self.scan_status(identifier)
        with _SCAN_LOCK:
            token=_SCANS.get((self.path,identifier))
            if token:token.set();state['cancel_requested']=True;self._save_scan(identifier,state)
        return state

    def list_models(self,q='',voice_id=None,run_id=None,status=None,kind='inference_export',offset=0,limit=100,favorite=None,include_aliases=False,sort='version'):
        offset=max(0,int(offset));limit=max(1,min(250,int(limit)));where=[];args=[]
        for field,value in [('voice_id',voice_id),('run_id',run_id),('status',status),('kind',kind)]:
            if value and value!='all':where.append('a.'+field+'=?');args.append(value)
        if q:
            where.append('(a.body LIKE ? OR v.body LIKE ?)');args += ['%'+str(q)[:200]+'%']*2
        if favorite is not None:where.append("json_extract(v.body,'$.favorite')=?");args.append(bool(favorite))
        if not include_aliases:where.append("(json_extract(a.body,'$.alias_of') IS NULL OR NOT EXISTS (SELECT 1 FROM catalog_artifacts canonical WHERE canonical.id=json_extract(a.body,'$.alias_of') AND canonical.run_id=a.run_id AND canonical.voice_id=a.voice_id AND canonical.status='ready'))")
        clause=' WHERE '+' AND '.join(where) if where else ''
        if sort not in ('version','recent','name'):raise ValueError('未知目录排序方式')
        order={
            'version':"a.run_id,COALESCE(json_extract(a.body,'$.epoch'),-1),COALESCE(json_extract(a.body,'$.global_step'),-1),a.id",
            'recent':"COALESCE(json_extract(a.body,'$.last_used_at'),'') DESC,a.id",
            'name':"json_extract(a.body,'$.name'),a.id",
        }[sort]
        with self.connect() as db:
            total=db.execute('SELECT COUNT(*) FROM catalog_artifacts a LEFT JOIN catalog_voices v ON a.voice_id=v.id'+clause,args).fetchone()[0]
            rows=db.execute('SELECT a.body FROM catalog_artifacts a LEFT JOIN catalog_voices v ON a.voice_id=v.id'+clause+' ORDER BY '+order+' LIMIT ? OFFSET ?',args+[limit,offset]).fetchall()
        return {'models':[self._present(json.loads(row[0])) for row in rows],'total':total,'offset':offset,'limit':limit}

    def _present(self,value):
        result=copy.deepcopy(value)
        with self.connect() as db:rows=db.execute('SELECT path FROM catalog_paths WHERE artifact_id=?',(value['id'],)).fetchall()
        result['original_paths']=[r[0] for r in rows]
        result['can_validate']=result['kind']=='inference_export' and result['status'] not in ('missing','quarantined')
        result['can_render']=result['kind']=='inference_export' and result['status']=='ready'
        return result

    def get_model(self,identifier,ensure_ready=False):
        return self.validate(identifier) if ensure_ready else self._present(self._get('catalog_artifacts',identifier))

    def list_voices(self,q='',favorite=None,offset=0,limit=100):
        with self.connect() as db:
            rows=db.execute('SELECT body FROM catalog_voices').fetchall();counts=dict(db.execute("SELECT voice_id,COUNT(DISTINCT COALESCE(hash,id)) FROM catalog_artifacts WHERE kind='inference_export' GROUP BY voice_id").fetchall())
        values=[{**json.loads(r[0]),'version_count':counts.get(json.loads(r[0])['id'],0)} for r in rows]
        if q:values=[v for v in values if q.casefold() in encoded(v).casefold()]
        if favorite is not None:values=[v for v in values if v['favorite']==favorite]
        values.sort(key=lambda v:v.get('alias') or v['name'])
        values.sort(key=lambda v:v.get('last_used_at') or '',reverse=True)
        values.sort(key=lambda v:not v['favorite'])
        total=len(values);return {'voices':values[max(0,offset):max(0,offset)+min(250,max(1,limit))],'total':total,'offset':offset,'limit':limit}

    def list_runs(self,voice_id=None,q='',offset=0,limit=100):
        with self.connect() as db:rows=db.execute('SELECT body FROM catalog_runs'+(' WHERE voice_id=?' if voice_id else ''),(voice_id,) if voice_id else ()).fetchall()
        values=[json.loads(r[0]) for r in rows]
        if q:values=[v for v in values if q.casefold() in encoded(v).casefold()]
        for v in values:v.pop('config',None);v.pop('model_info',None)
        return {'runs':values[max(0,offset):max(0,offset)+min(250,max(1,limit))],'total':len(values),'offset':offset,'limit':limit}

    def update_voice(self,identifier,patch):
        if set(patch)-{'alias','name','description','tags','favorite','cover_artifact_id','preview_artifact_id'}:raise ValueError('不可修改声音身份；只允许别名和用户元数据')
        voice=self._get('catalog_voices',identifier)
        for key in ('alias','name','description'):
            if key in patch and (not isinstance(patch[key],str) or len(patch[key])>2000):raise ValueError('声音文本字段过长')
        if 'tags' in patch and (not isinstance(patch['tags'],list) or len(patch['tags'])>30 or any(not isinstance(t,str) or len(t)>80 for t in patch['tags'])):raise ValueError('标签格式无效')
        if 'favorite' in patch and type(patch['favorite']) is not bool:raise ValueError('favorite 必须是布尔值')
        voice.update(copy.deepcopy(patch),updated_at=utc())
        with self.connect() as db:db.execute('UPDATE catalog_voices SET body=? WHERE id=?',(encoded(voice),identifier))
        return voice

    def set_default(self,voice_id,model_id):
        model=self.get_model(model_id)
        if model['voice_id']!=voice_id or model['kind']!='inference_export':raise ValueError('默认版本必须属于此声音且为推理导出')
        voice=self._get('catalog_voices',voice_id);voice.update(default_model_id=model_id,updated_at=utc())
        with self.connect() as db:db.execute('UPDATE catalog_voices SET body=? WHERE id=?',(encoded(voice),voice_id))
        return voice

    def _bind_record(self,model_id,index_id,evidence):
        with self.connect() as db:db.execute('INSERT OR REPLACE INTO catalog_bindings VALUES(?,?,?)',(model_id,index_id,encoded({'evidence':evidence,'created_at':utc()})))

    def index_compatibility(self,model,index):
        reasons=[];evidence=[];status='unknown'
        if index.get('content_dim')!=model.get('content_dim'):return {'status':'incompatible','reasons':['索引维度与模型内容特征维度不一致'],'evidence':[]}
        if index['status'] in ('quarantined','missing'):return {'status':'incompatible','reasons':['索引文件无效或缺失'],'evidence':[]}
        if not model.get('embedder_verified'):reasons.append('模型内容编码器尚未确认')
        if index.get('embedder_model') and index.get('embedder_model')!=model.get('embedder_model'):return {'status':'incompatible','reasons':['索引与模型的内容编码器不同'],'evidence':[]}
        with self.connect() as db:
            binding=db.execute('SELECT body FROM catalog_bindings WHERE model_id=? AND index_id=?',(model['id'],index['id'])).fetchone()
            anchors=db.execute('SELECT a.body,b.body FROM catalog_bindings b JOIN catalog_artifacts a ON a.id=b.model_id WHERE b.index_id=? AND a.run_id=?',(index['id'],model['run_id'])).fetchall()
        run=self._get('catalog_runs',model['run_id'])
        model_link=model.get('model_name')==run['name'] or model.get('legacy_id')==run['name']
        if binding:evidence.append(json.loads(binding[0])['evidence'])
        elif model['run_id']==index['run_id'] and model_link and anchors:
            anchor,association=next(((json.loads(a),json.loads(b)) for a,b in anchors if json.loads(a).get('embedder_model')==model.get('embedder_model') and
                json.loads(a).get('content_dim')==model.get('content_dim') and json.loads(a).get('status')=='ready'),(None,None))
            if anchor:
                evidence.append({'kind':'shared_registered_training_run','run_id':run['id'],'model_name':model.get('model_name'),
                    'anchor_model_id':anchor['id'],'anchor_model_hash':anchor.get('model_hash'),'index_hash':index.get('sha256'),
                    'association':association['evidence'],'summary':'模型内嵌实验标识、编码器及维度与已登记索引的实验一致'})
            else:reasons.append('训练实验的已登记关联目前不可验证')
        else:reasons.append('未验证索引训练来源；同一文件夹不等于匹配，请明确绑定')
        if evidence and model.get('embedder_verified') and (index.get('embedder_model')==model.get('embedder_model') or binding):
            status='verified';reasons=[]
        elif not index.get('embedder_model'):reasons.append('索引内容编码器来源未知')
        return {'status':status,'reasons':reasons,'evidence':evidence,'model_run_id':model['run_id'],'index_run_id':index['run_id'],
                'content_dim':index.get('content_dim'),'embedder_model':model.get('embedder_model'),'fully_validated':bool(index.get('fully_validated'))}

    def model_indices(self,identifier):
        model=self.get_model(identifier)
        with self.connect() as db:rows=db.execute("SELECT body FROM catalog_artifacts WHERE kind='index' ORDER BY run_id=? DESC,id",(model['run_id'],)).fetchall()
        indices=[json.loads(row[0]) for row in rows]
        return {'indices':[{**i,'compatibility':self.index_compatibility(model,i)} for i in indices],'selected_index_id':model.get('selected_index_id')}

    def _cpu_probe(self,path,index=False):
        python=self.config.get('applio',{}).get('python')
        if not python or not Path(python).is_file():raise ValueError('缺少已锁定的 RVC CPU 探测环境')
        script=ROOT/'workers/catalog_probe.py' if index else ROOT/'workers/rvc/probe_model.py'
        command=[python,str(script),str(path)]+(['--index'] if index else [])
        result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',timeout=90,
            creationflags=0x08000000 if os.name=='nt' else 0,env={**os.environ,'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1','TORCH_FORCE_WEIGHTS_ONLY_LOAD':'1','CUDA_VISIBLE_DEVICES':''})
        try:value=json.loads(result.stdout);value=value[0] if isinstance(value,list) else value
        except ValueError:raise ValueError('安全 CPU 探测失败：'+result.stderr[-500:])
        if result.returncode or not value.get('ok'):raise ValueError('安全 CPU 探测失败：'+str(value.get('error',result.stderr[-500:])))
        return value

    def validate(self,identifier):
        model=self._get('catalog_artifacts',identifier)
        if model['kind'] not in ('inference_export','index'):raise ValueError('此资源不是可验证的推理导出或索引，训练 G/D 不会直接加载')
        path=Path(model['path'])
        try:
            if not path.is_file():raise ValueError('文件缺失，请按原哈希重新链接')
            before=signature(path)
            expected=model.get('model_hash') or model.get('sha256')
            if not expected and model.get('stat_signature') and before!=model['stat_signature']:raise ValueError('文件自扫描后发生变化，请重新扫描；不替换此前发现的版本身份')
            digest=sha256(path,deadline=time.monotonic()+90)
            if expected and digest!=expected:raise ValueError('文件哈希已变化，旧版本已隔离；请扫描新增内容，不能替换历史身份')
            if model.get('validated_at') and model['status']=='ready' and model.get('stat_signature')==before:
                return self._present(model)
            probe=self._cpu_probe(path,index=model['kind']=='index')
            if signature(path)!=before:raise ValueError('探测期间文件发生变化，拒绝注册')
            if model['kind']=='inference_export':
                run=self._get('catalog_runs',model['run_id']);metadata=trained_metadata({**model,**probe},run)
                for key in ('path','id','model_artifact_id','created_at','legacy_id','name','voice_id','run_id','origin_path','selected_index_id'):
                    if key in model:metadata[key]=model[key]
                model.update(metadata,model_hash=digest,sha256=digest,engine_revision=self.config.get('applio',{}).get('revision'),inference_schema_version=1)
                if not model.get('speaker_ids'):raise ValueError('缺少已训练说话人 ID 元数据；embedding 容量不是说话人数')
                if not model.get('embedder_verified'):raise ValueError('内容编码器未知或不受支持，请补充可信训练元数据')
                if not model.get('f0_conditioned'):raise ValueError('当前锁定的 RVC 执行器需要 F0 条件模型；此文件保留但不能直接运行')
            else:
                if not probe.get('trained') or int(probe.get('ntotal',0))<=0:raise ValueError('索引尚未训练或没有特征；请使用有效索引或明确关闭检索')
                model.update(probe,sha256=digest,model_hash=None)
            model.update(status='ready',reason=None,validated_at=utc(),updated_at=utc(),stat_signature=before)
            # Hash dedup retains every ID and original path; no history row is deleted.
            with self.connect() as db:duplicates=db.execute('SELECT body FROM catalog_artifacts WHERE hash=? AND kind=? AND id!=? ORDER BY rowid',(digest,model['kind'],identifier)).fetchall()
            canonical=next((json.loads(r[0]) for r in duplicates if not json.loads(r[0]).get('alias_of') and json.loads(r[0])['status']=='ready'),None)
            if canonical:model['alias_of']=canonical['id']
            self._put(model);self._path(identifier,path)
            if canonical:
                with self.connect() as db:
                    for row in db.execute('SELECT path,signature,last_seen FROM catalog_paths WHERE artifact_id=?',(identifier,)).fetchall():
                        db.execute('INSERT OR IGNORE INTO catalog_paths VALUES(?,?,?,?)',(canonical['id'],*row))
            return self._present(model)
        except Exception as error:
            model.update(status='missing' if not path.is_file() else 'quarantined',reason=str(error),updated_at=utc());self._put(model);raise ValueError(str(error))

    def bind_index(self,model_id,index_id):
        model=self.validate(model_id);index=self.validate(index_id)
        if model['kind']!='inference_export' or index['kind']!='index':raise ValueError('需要模型与 Index 资源')
        before=self.index_compatibility(model,index)
        if before['status']=='incompatible':raise ValueError('；'.join(before['reasons']))
        self._bind_record(model_id,index_id,{'kind':'user_explicit_binding','model_hash':model['model_hash'],'index_hash':index['sha256'],
            'content_dim':index['content_dim'],'embedder_model':model['embedder_model']})
        model['selected_index_id']=index_id;self._put(model);return self.resolve_model(model_id,index_id)

    def resolve_model(self,identifier,index_id=None):
        model=self.validate(identifier)
        if model['kind']!='inference_export':raise ValueError('训练检查点不能直接用于推理')
        index_id=index_id if index_id is not None else model.get('selected_index_id')
        if index_id is None:
            matching=[i for i in self.model_indices(identifier)['indices'] if i['compatibility']['status']=='verified']
            if len(matching)==1:index_id=matching[0]['id']
        result={**model,'id':identifier,'model_artifact_id':identifier,'canonical_id':model.get('alias_of',identifier),'version_id':identifier,
                'index_id':None,'index_path':None,'index_hash':None,'index_match':{'status':'unknown','reasons':['未选择已验证索引'],'evidence':[]}}
        if index_id:
            index=self.validate(index_id);match=self.index_compatibility(model,index)
            if match['status']!='verified':raise ValueError('索引来源或维度未验证：'+'；'.join(match['reasons']))
            result.update(index_id=index_id,index_path=index['path'],index_hash=index['sha256'],index_match=match)
        voice=self._get('catalog_voices',model['voice_id']);voice['last_used_at']=utc();voice['last_used_model_id']=identifier
        model['last_used_at']=voice['last_used_at'];self._put(model)
        result['last_used_at']=model['last_used_at']
        with self.connect() as db:db.execute('UPDATE catalog_voices SET body=? WHERE id=?',(encoded(voice),voice['id']))
        return result

    def relink(self,identifier,path):
        model=self._get('catalog_artifacts',identifier);path=self._safe_path(path)
        expected=model.get('model_hash') or model.get('sha256')
        if not expected:raise ValueError('原版本尚无确认哈希，不能按文件名重新链接；请作为新版本导入')
        if sha256(path,deadline=time.monotonic()+90)!=expected:raise ValueError('哈希不匹配，拒绝替换旧版本')
        model.update(path=str(path),status='discovered',reason=None,updated_at=utc());self._put(model);self._path(identifier,path)
        return self.validate(identifier)

    def import_batch(self,paths):
        if not isinstance(paths,list) or not 1<=len(paths)<=100:raise ValueError('每批请选择 1–100 个模型/索引文件')
        results=[]
        for value in paths:
            try:
                path=self._safe_path(value)
                if not path.is_file() or path.suffix.lower() not in ('.pth','.pt','.index'):raise ValueError('仅接受独立 RVC 权重或索引文件，不执行压缩包/安装脚本')
                results.append({'path':str(path),'ok':True,'model':self._present(self._register_path(path))})
            except (ValueError,OSError) as error:results.append({'path':str(value),'ok':False,'error':str(error)})
        return {'results':results}

    def record_selection(self,model_id,project_id=None,track_id=None,clip_id=None,index_id=None,speaker_id=0,preset_id=None):
        model=self.resolve_model(model_id,index_id)
        if speaker_id not in model['speaker_ids']:raise ValueError('说话人 ID 未在训练元数据中验证')
        value={'id':identity('selection',uuid.uuid4().hex),'model_artifact_id':model_id,'model_hash':model['model_hash'],
            'voice_id':model['voice_id'],'run_id':model['run_id'],'project_id':project_id,'track_id':track_id,'clip_id':clip_id,
            'index_id':model['index_id'],'index_hash':model['index_hash'],'speaker_id':speaker_id,'preset_id':preset_id,'created_at':utc()}
        with self.connect() as db:db.execute('INSERT INTO catalog_selections VALUES(?,?,?)',(value['id'],model_id,encoded(value)))
        return value
