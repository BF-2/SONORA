import copy
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from .config import DATA, TIMEBASE
from .contracts import validate_project

def now():
    return datetime.now(timezone.utc).isoformat()

def uid(prefix):
    return prefix + '_' + uuid.uuid4().hex[:20]

def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)

def edit_state(project):
    value=copy.deepcopy(project)
    for key in ('revision','updated_at','playhead','mode','view_preferences','last_user_audio_edit'):value.pop(key,None)
    value.get('parameters',{}).pop('active_voice_part',None)
    value.get('parameters',{}).get('parameter_sources',{}).pop('active_voice_part',None)
    return value

class Conflict(ValueError):
    pass

class Store:
    def __init__(self, path=None):
        self.path = str(path or DATA / 'sonora.sqlite')
        self.lock = threading.RLock()
        with self.connect() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > 1:
                raise RuntimeError('数据库来自更新的 SONORA，拒绝写入。')
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, project_id TEXT, idem TEXT, body TEXT NOT NULL, UNIQUE(project_id,idem));
            CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS history(project_id TEXT, position INTEGER, body TEXT, PRIMARY KEY(project_id,position));
            CREATE TABLE IF NOT EXISTS cursors(project_id TEXT PRIMARY KEY, position INTEGER);
            CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, body TEXT NOT NULL);
            PRAGMA user_version=1;
            ''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def get(self, table, identifier):
        if table not in ('projects', 'artifacts', 'jobs'): raise ValueError('table')
        with self.connect() as db:
            row = db.execute(f'SELECT body FROM {table} WHERE id=?', (identifier,)).fetchone()
        if row is None: raise KeyError(identifier)
        return json.loads(row[0])

    def all(self, table):
        if table not in ('projects', 'artifacts', 'jobs'): raise ValueError('table')
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute(f'SELECT body FROM {table} ORDER BY rowid DESC')]

    def put(self, table, value):
        if table not in ('projects', 'artifacts', 'jobs'): raise ValueError('table')
        with self.lock, self.connect() as db:
            if table == 'jobs':
                db.execute('INSERT INTO jobs(id,project_id,idem,body) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body', (value['id'], value['project_id'], value['idempotency_key'], encode(value)))
            else:
                db.execute(f'INSERT INTO {table}(id,body) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body', (value['id'], encode(value)))
        return value

    def new_project(self, name, task='speech'):
        p = dict(id=uid('project'), name=name, task=task, schema_version=1, revision=1, timebase=TIMEBASE,
                 created_at=now(), updated_at=now(), assets=[], tracks=[], selection={'start':0,'end':0},playhead=0, mode='quick',
                 parameters={'pitch_algorithm':'rmvpe','transpose':0,'index_rate':0,'protect':0.33}, pitch_edits=[], notes=[],lyrics=[],candidates=[],selected_candidate_id=None,voice_parts={},view_preferences={},model_bindings={'tracks':{},'clips':{}},comparisons=[],exports=[],graph=[],warnings=[])
        from .production import initialize
        initialize(p)
        with self.lock,self.connect() as db:
            db.execute('INSERT INTO projects(id,body) VALUES(?,?)',(p['id'],encode(p)))
            self._checkpoint(db,p)
        return p

    def checkpoint(self, p):
        with self.lock, self.connect() as db:
            self._checkpoint(db,p)

    def _checkpoint(self,db,p):
        row = db.execute('SELECT position FROM cursors WHERE project_id=?',(p['id'],)).fetchone()
        position = row[0] + 1 if row else 0
        db.execute('DELETE FROM history WHERE project_id=? AND position>=?',(p['id'],position))
        db.execute('INSERT INTO history VALUES(?,?,?)',(p['id'],position,encode(p)))
        db.execute('INSERT INTO cursors VALUES(?,?) ON CONFLICT(project_id) DO UPDATE SET position=excluded.position',(p['id'],position))

    def change(self, identifier, mutation, revision=None, history=True, auto_archive=False):
        with self.lock,self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT body FROM projects WHERE id=?',(identifier,)).fetchone()
            if row is None:raise KeyError(identifier)
            p=json.loads(row[0])
            if revision is not None and revision != p['revision']: raise Conflict('工程已更新，请刷新后重试。')
            before=edit_state(p)
            previous_state=copy.deepcopy(p)
            previous_candidates={c['id'] for c in p.get('candidates',[])}
            mutation(p)
            from .production import synchronize, enabled
            synchronize(p)
            if enabled(p) and previous_state.get('assets')==p.get('assets') and any(previous_state.get(k)!=p.get(k) for k in ('parameters','voice_parts','pitch_edits','notes','model_bindings')):
                from .production import routed_tracks
                if routed_tracks(previous_state)==routed_tracks(p):
                    # Accepted audio is immutable: editing tomorrow's candidate
                    # does not invalidate today's final mix.
                    old_nodes={n['id']:n for n in previous_state['graph'] if n['type'] in ('mix','export','export_stems')}
                    p['graph']=[copy.deepcopy(old_nodes.get(n['id'],n)) for n in p['graph']]
            if auto_archive and not enabled(p) and p.get('view_preferences',{}).get('auto_archive_candidates',True) and any(c['id'] not in previous_candidates for c in p.get('candidates',[])):
                from .candidate_archive import archive_older
                archive_older(p)
            p['revision'] += 1
            p['updated_at'] = now()
            validate_project(p)
            db.execute('UPDATE projects SET body=? WHERE id=?',(encode(p),identifier))
            if history and edit_state(p)!=before: self._checkpoint(db,p)
            return p

    def history(self, identifier, direction, revision):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self.get('projects',identifier)
            if current['revision'] != revision: raise Conflict('工程版本已更新。')
            pos = db.execute('SELECT position FROM cursors WHERE project_id=?',(identifier,)).fetchone()[0]
            checkpoint=db.execute('SELECT body FROM history WHERE project_id=? AND position=?',(identifier,pos)).fetchone()
            baseline=edit_state(json.loads(checkpoint[0])) if checkpoint else edit_state(current)
            target=pos
            while True:
                target += -1 if direction=='undo' else 1
                row=db.execute('SELECT body FROM history WHERE project_id=? AND position=?',(identifier,target)).fetchone()
                if not row:return current
                if edit_state(json.loads(row[0]))!=baseline:break
            value = json.loads(row[0]); value['revision'] = current['revision']+1; value['updated_at']=now();value['last_user_audio_edit']=uid('edit')
            if current.get('production_version')==1:
                from .production import initialize,synchronize
                initialize(value);synchronize(value)
            for key in ('playhead','mode','view_preferences'):
                if key in current:value[key]=copy.deepcopy(current[key])
            if 'active_voice_part' in current['parameters']:value['parameters']['active_voice_part']=current['parameters']['active_voice_part']
            db.execute('UPDATE projects SET body=? WHERE id=?',(encode(value),identifier))
            db.execute('UPDATE cursors SET position=? WHERE project_id=?',(target,identifier))
            return value

    def event(self, kind, job=None, data=None):
        body={'type':kind,'job_id':job['id'] if job else None,'project_id':job['project_id'] if job else None,'data':data or {},'created_at':now()}
        with self.lock,self.connect() as db:
            seq=db.execute('INSERT INTO events(body) VALUES(?)',(encode(body),)).lastrowid
        return dict(sequence=seq,**body)

    def events(self, after):
        with self.connect() as db:
            rows=db.execute('SELECT sequence,body FROM events WHERE sequence>? ORDER BY sequence LIMIT 500',(after,)).fetchall()
        return [dict(sequence=r[0],**json.loads(r[1])) for r in rows]

    def idem(self, project_id, key):
        with self.connect() as db:
            row=db.execute('SELECT body FROM jobs WHERE project_id=? AND idem=?',(project_id,key)).fetchone()
        return json.loads(row[0]) if row else None

    def cache_get(self,key):
        with self.connect() as db:
            row=db.execute('SELECT body FROM cache WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None

    def cache_put(self,key,value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO cache VALUES(?,?)',(key,encode(value)))
