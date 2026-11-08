"""Inventory actual local dependencies, hashes and license metadata, no network."""
from pathlib import Path
import importlib.metadata as metadata
import json,hashlib,subprocess
ROOT=Path(__file__).resolve().parents[1]
core=[]
for dist in sorted(metadata.distributions(),key=lambda d:d.metadata['Name'].lower()):
    core.append({'name':dist.metadata['Name'],'version':dist.version,'license_expression':dist.metadata.get('License-Expression'),'license_metadata':dist.metadata.get('License'),'homepage':dist.metadata.get('Home-page')})
lock=json.loads((ROOT/'apps/web/package-lock.json').read_text(encoding='utf-8'))
web=[{'path':key,**{k:v for k,v in val.items() if k in ('version','resolved','integrity','license')}} for key,val in lock['packages'].items() if key]
out={'format':'SONORA component inventory v1 (not a certified legal opinion)','core_python':core,'web_npm':web,'rust_lock':'apps/desktop/Cargo.lock','rust_licenses':'apps/desktop/dependencies.windows.json','engine_versions':'engine-envs/locked-environments.json','model_registry':'engine-envs/resources.json (local, excluded from source distribution)','ffmpeg':'engine-envs/media-tools.json','weights_bundled_for_distribution':False}
(ROOT/'docs/sbom.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
print('Wrote docs/sbom.json',len(core),'Python distributions',len(web),'npm entries')
