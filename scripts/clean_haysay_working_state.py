"""Run with the SONORA service stopped. Preserve project/audio data; clear retired catalog/cache."""
import sqlite3,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'engine-envs/haysay'
def main():
    path=ROOT/'runtime/sonora.sqlite';backup=ROOT/'runtime/backups/haysay-before.sqlite'
    if not backup.exists():
        with sqlite3.connect(path) as db,sqlite3.connect(backup) as dest:db.backup(dest)
    with sqlite3.connect(path) as db:
        assert not any(json.loads(b).get('state') in ['queued','preparing','running'] for (b,) in db.execute('select body from jobs'))
        counts={t:db.execute('select count(*) from '+t).fetchone()[0] for t in ['projects','artifacts','jobs','history']}
        db.execute('PRAGMA secure_delete=ON');db.execute('PRAGMA temp_store=MEMORY')
        for (table,) in db.execute("select name from sqlite_master where type='table' and name like 'catalog_%'").fetchall():
            if table!='catalog_meta':db.execute('delete from '+table)
        db.execute("delete from catalog_meta where key!='schema_version'")
        new_hashes={m['model_hash'] for m in json.loads((ROOT/'engine-envs/haysay-models.json').read_text(encoding='utf-8'))['models']};cleared=0
        for key,body in db.execute('select key,body from cache').fetchall():
            if 'model_hash' in body and not any(h in body for h in new_hashes):db.execute('delete from cache where key=?',(key,));cleared+=1
        db.commit();db.execute('PRAGMA wal_checkpoint(TRUNCATE)');db.execute('VACUUM');assert db.execute('pragma integrity_check').fetchone()[0]=='ok'
        assert counts=={t:db.execute('select count(*) from '+t).fetchone()[0] for t in counts}
    OUT.mkdir(exist_ok=True);(OUT/'state-cleanup.json').write_text(json.dumps(dict(preserved_counts=counts,old_conversion_cache_entries_removed=cleared,backup=str(backup),database_integrity='ok'),indent=2),encoding='utf-8');print('Preserved',counts,'removed old conversion cache',cleared)
if __name__=='__main__':main()
