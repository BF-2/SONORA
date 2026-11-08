"""Plan removal of retired voice registries and test resources from this checkout."""
import json,re,sqlite3
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'engine-envs/haysay';OUT.mkdir(exist_ok=True)
AUDIT=Path('E:/voice/maintenance/sonora_haysay_20261104/retired-evidence')
OLD=re.compile(r'(?:_pony_(?:singing|speech)_(?:r\d|v\d|20\d)|retrain_under18_20\d|twilight_sparkle_singing\.pth|Applio[\\/]+logs[\\/]+(?:twilight|rainbow|fluttershy|rarity|pinkie|applejack))',re.I)
def main():
    moves=[];seen=set();roots=[ROOT/'runtime',ROOT/'tests/results',ROOT/'apps/web/qa',*ROOT.glob('sonora-test-*')]
    for base in roots:
        if not base.exists():continue
        for p in base.rglob('*'):
            if not p.is_file() or p.is_symlink():continue
            rel=p.relative_to(ROOT)
            if rel.parts[:2]==('runtime','haysay-acceptance') or p.name.startswith('haysay-'):continue
            if p.name in ['sonora.sqlite','sonora.sqlite-wal','sonora.sqlite-shm'] and p.parent==ROOT/'runtime':continue
            reason=None
            if p.suffix.lower() in ['.pth','.index']:reason='retired RVC test weight or index'
            elif '.sqlite' in p.name:reason='retired isolated database or backup; not production'
            elif p.suffix.lower() in ['.json','.jsonl','.log','.md','.txt','.csv','.html','.py','.js','.cjs'] and p.stat().st_size<20*1024*1024:
                try:
                    if OLD.search(p.read_text(encoding='utf-8',errors='ignore')):reason='retired voice reference in generated state or evidence'
                except OSError:pass
            if reason and str(p) not in seen:
                moves.append(dict(source=str(p.resolve()),destination=str(AUDIT/rel),reason=reason));seen.add(str(p))
    (OUT/'cleanup-plan.json').write_text(json.dumps(moves,ensure_ascii=False,indent=2),encoding='utf-8');print('Planned exact files',len(moves))
if __name__=='__main__':main()
