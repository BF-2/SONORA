"""Copy a completed local QA project into the main workspace without replacing work.

Only content-verified cache/export files and committed records are imported.
Original job ownership and numerical provenance remain unchanged. No worker runs.
"""
from pathlib import Path
import argparse,copy,hashlib,json,os,sqlite3,datetime

ROOT=Path(__file__).resolve().parents[1]
def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(4*1024*1024),b''):h.update(b)
    return h.hexdigest()
def link(source,target,expected=None):
    source=Path(source);target=Path(target)
    if expected and digest(source)!=expected:raise ValueError('Source checksum mismatch: '+str(source))
    target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists():
        if digest(target)!=digest(source):raise ValueError('Refusing to overwrite different target: '+str(target))
        return
    try:os.link(source,target)
    except OSError:
        import shutil
        shutil.copyfile(source,target)
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-data',required=True);parser.add_argument('--project-id',required=True);args=parser.parse_args()
    source=Path(args.source_data).resolve();target=(ROOT/'runtime').resolve()
    if source==target or not source.is_relative_to(ROOT):raise ValueError('Expected separate task-local source runtime')
    src=sqlite3.connect(f'file:{(source/"sonora.sqlite").as_posix()}?mode=ro',uri=True);dst=sqlite3.connect(target/'sonora.sqlite',timeout=30)
    src.execute('BEGIN')
    if dst.execute('PRAGMA user_version').fetchone()[0]!=1:raise ValueError('Unsupported destination schema')
    pid=args.project_id
    if dst.execute('SELECT 1 FROM projects WHERE id=?',(pid,)).fetchone():raise ValueError('Project already exists; no user work replaced')
    row=src.execute('SELECT body FROM projects WHERE id=?',(pid,)).fetchone()
    if not row:raise ValueError('Source project missing')
    p=json.loads(row[0]);jobs={r[0]:json.loads(r[1]) for r in src.execute('SELECT id,body FROM jobs WHERE project_id=?',(pid,))}
    if any(j['state'] not in ('succeeded','failed','cancelled','blocked') for j in jobs.values()):raise ValueError('Source has active jobs')
    artifacts={};paths={}
    def collect(aid):
        if aid in artifacts:return
        r=src.execute('SELECT body FROM artifacts WHERE id=?',(aid,)).fetchone()
        if not r:raise ValueError('Missing ancestor '+aid)
        a=json.loads(r[0]);artifacts[aid]=a
        path=Path(a['path']).resolve()
        if a['commit_state']!='committed' or not path.is_relative_to(source/'cache'):raise ValueError('Uncommitted or out-of-scope artifact')
        destination=target/path.relative_to(source);link(path,destination,a['sha256']);paths[str(path)]=str(destination)
        waveform=path.with_suffix('.waveform.json')
        if waveform.is_file():link(waveform,destination.with_suffix('.waveform.json'))
        if a.get('original_snapshot'):
            original=Path(a['original_snapshot']).resolve()
            if original.is_relative_to(source/'cache') and original.exists():
                new=target/original.relative_to(source);link(original,new,a.get('original_hash'));paths[str(original)]=str(new)
        for ref in a.get('input_artifact_ids',[]):collect(ref)
        if a.get('cache_source_artifact_id'):collect(a['cache_source_artifact_id'])
        if a.get('job_id') and a['job_id'] not in jobs:
            jr=src.execute('SELECT body FROM jobs WHERE id=?',(a['job_id'],)).fetchone()
            if jr:jobs[a['job_id']]=json.loads(jr[0])
    for a in p['assets']:collect(a['id'])
    for e in p['exports']:
        path=Path(e['path']).resolve()
        if not path.is_relative_to(source/'exports'):raise ValueError('Export outside source scope')
        destination=target/'exports'/'harmony-89-115'/path.name;link(path,destination,e['sha256']);paths[str(path)]=str(destination)
    def remap(value):
        if isinstance(value,dict):return {k:remap(v) for k,v in value.items()}
        if isinstance(value,list):return [remap(v) for v in value]
        if isinstance(value,str):return paths.get(value,value)
        return value
    backup=target/'backups'/('before-harmony-import-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.sqlite')
    backup.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(backup) as backup_db:dst.backup(backup_db)
    encode=lambda obj:json.dumps(remap(obj),ensure_ascii=False,separators=(',',':'))
    with dst:
        dst.execute('BEGIN IMMEDIATE')
        for a in artifacts.values():
            old=dst.execute('SELECT body FROM artifacts WHERE id=?',(a['id'],)).fetchone()
            if old and json.loads(old[0])['sha256']!=a['sha256']:raise ValueError('Artifact identity conflict')
            dst.execute('INSERT OR IGNORE INTO artifacts(id,body) VALUES(?,?)',(a['id'],encode(a)))
        for j in jobs.values():dst.execute('INSERT OR IGNORE INTO jobs(id,project_id,idem,body) VALUES(?,?,?,?)',(j['id'],j['project_id'],j['idempotency_key'],encode(j)))
        p['imported_verified_runtime']=str(source)
        dst.execute('INSERT INTO projects(id,body) VALUES(?,?)',(pid,encode(p)))
        history=src.execute('SELECT position,body FROM history WHERE project_id=?',(pid,)).fetchall()
        for pos,body in history:dst.execute('INSERT INTO history VALUES(?,?,?)',(pid,pos,encode(json.loads(body))))
        cursor=src.execute('SELECT position FROM cursors WHERE project_id=?',(pid,)).fetchone()
        if cursor:dst.execute('INSERT INTO cursors VALUES(?,?)',(pid,cursor[0]))
    print(json.dumps({'project_id':pid,'artifacts':len(artifacts),'jobs':len(jobs),'backup':str(backup),'no_existing_project_replaced':True},ensure_ascii=True))
if __name__=='__main__':main()
