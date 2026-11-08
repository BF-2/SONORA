"""Real Hay Say acceptance entrypoint; delegates to the current six-role API check."""
import runpy,sys
from pathlib import Path
if __name__=='__main__':
    if '--run-real' not in sys.argv:raise SystemExit('Pass --run-real to execute the six Hay Say models with their indexes.')
    runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/verify_haysay.py'),run_name='__main__')
