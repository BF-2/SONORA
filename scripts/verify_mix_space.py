"""Isolated real-test7 audio/API/browser acceptance; no production project edits."""
import os
import sys
import json
import shutil
import subprocess
import time
import uuid
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT.parent / 'sonora-mix-spatial-20261105'
DATA = ROOT / 'runtime' / 'mix-space-qa' / 'data'
SNAPSHOT = ROOT.parent / 'sonora-test7-resonance-20261105/snapshot/test7-project.json'
PYTHON = ROOT / '.venv/Scripts/python.exe'


def seed():
    sys.path.insert(0, str(ROOT))
    from services.orchestrator.store import Store
    store = Store()
    p = json.loads(SNAPSHOT.read_text(encoding='utf-8-sig'))
    p['name'] = 'test7 · 空间效果隔离验收'; p['exports'] = []
    p['selection'] = {'start': 46 * 48000, 'end': 62 * 48000}
    p.setdefault('view_preferences', {})['stage'] = 4
    for a in p['assets']:
        src = Path(a['path']); dest = DATA / 'cache' / a['sha256'][:2] / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists(): shutil.copyfile(src, dest)
        if src.suffix.lower() in ('.wav','.flac'):
            waveform=src.with_suffix('.waveform.json')
            if waveform.exists():shutil.copyfile(waveform,dest.with_suffix('.waveform.json'))
            else:
                from services.orchestrator import media
                dest.with_suffix('.waveform.json').write_text(json.dumps(media.analyze(dest)),encoding='utf-8')
        a['path'] = str(dest); store.put('artifacts', a)
    store.put('projects', p)


def main():
    OUT.mkdir(exist_ok=True); (OUT / 'reports').mkdir(exist_ok=True); DATA.mkdir(parents=True,exist_ok=True)
    token = uuid.uuid4().hex; port = 8881; base = f'http://127.0.0.1:{port}'
    env = {**os.environ, 'SONORA_DATA': str(DATA), 'SONORA_PORT': str(port), 'SONORA_SESSION_TOKEN': token, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'SONORA_DISABLE_MODEL_WORKERS': '1'}
    subprocess.run([str(PYTHON), '-B', str(__file__), '--seed'], cwd=ROOT, env=env, check=True)
    log = (OUT / 'reports/acceptance-server.log').open('w', encoding='utf-8')
    server = subprocess.Popen([str(PYTHON), '-B', '-m', 'services.orchestrator.app', '--port', str(port)], cwd=ROOT, env=env, stdout=log, stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    result = {'checks': [], 'errors': [], 'production_projects_modified': False}
    def api(path, body=None, method=None):
        req = urllib.request.Request(base + '/api/v1' + path, data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, method=method)
        with urllib.request.urlopen(req, timeout=30) as response: return json.load(response)
    pid = json.loads(SNAPSHOT.read_text(encoding='utf-8-sig'))['id']
    def wait(predicate, timeout=120):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            val = predicate()
            if val: return val
            time.sleep(.2)
        raise AssertionError('Acceptance timeout')
    try:
        for _ in range(100):
            try: api('/health'); break
            except Exception:
                if server.poll() is not None: raise RuntimeError('Backend failed; inspect acceptance-server.log')
                time.sleep(.2)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width':1920, 'height':1080})
            page.on('pageerror', lambda e: result['errors'].append(str(e)))
            page.add_init_script('localStorage.setItem("sonora-project",'+json.dumps(pid)+');localStorage.setItem("sonora-ui-theme","soft");')
            page.goto(base + '/#token=' + token + '&endpoint=' + base + '/api/v1')
            page.get_by_role('button', name='成品与混音', exact=False).first.click()
            # The selected converted channel controls the inspector.
            page.locator('button.channel-name').filter(has_text='候选 3').click()
            panel = page.get_by_label('混音效果设置', exact=True)
            panel.get_by_role('button', name='升级为立体声混响').click()
            wait(lambda: any(f.get('algorithm')=='diffuse-v1' for t in api('/projects/'+pid)['tracks'] if t.get('final_id') for f in t['effects']))
            result['checks'].append('Legacy reverb explicitly upgrades without automatic project migration')
            panel.get_by_role('button', name='应用 C 方案', exact=True).click()
            def final_track(): return next(t for t in api('/projects/'+pid)['tracks'] if t.get('final_id'))
            wait(lambda: any(f['type']=='vocal_restore' for f in final_track()['effects']))
            panel.get_by_role('button', name='C++ · 更强', exact=True).click()
            wait(lambda: next(f for f in final_track()['effects'] if f['type']=='vocal_space')['strength']==316)
            field = panel.get_by_label('C 人声空间 空间强度', exact=True); field.fill('175'); field.press('Tab')
            wait(lambda: next(f for f in final_track()['effects'] if f['type']=='vocal_space')['strength']==175)
            page.reload(); panel.wait_for()
            page.locator('button.channel-name').filter(has_text='候选 3').click()
            assert panel.get_by_label('C 人声空间 空间强度', exact=True).input_value() == '175'
            result['checks'].append('C preset, numeric strength, persistence and refreshed UI')
            panel.get_by_role('button', name='C 人声空间前移', exact=True).click()
            wait(lambda: [f['type'] for f in final_track()['effects']][-2:]==['vocal_space','vocal_restore'])
            panel.get_by_role('button', name='C 人声空间后移', exact=True).click()
            wait(lambda: [f['type'] for f in final_track()['effects']][-2:]==['vocal_restore','vocal_space'])
            panel.get_by_label('启用C 人声空间', exact=True).uncheck()
            wait(lambda: not next(f for f in final_track()['effects'] if f['type']=='vocal_space')['enabled'])
            panel.get_by_label('启用C 人声空间', exact=True).check()
            wait(lambda: next(f for f in final_track()['effects'] if f['type']=='vocal_space')['enabled'])
            result['checks'].append('Effect ordering and bypass persist through API')
            p = api('/projects/'+pid)
            job = api('/projects/'+pid+'/jobs', {'revision':p['revision'],'operation':'mix','idempotency_key':uuid.uuid4().hex,'parameters':{}})
            done = wait(lambda: (lambda j: j if j['state'] in ('succeeded','failed','cancelled','blocked') else None)(api('/jobs/'+job['id'])), 180)
            assert done['state']=='succeeded',done.get('error')
            p = api('/projects/'+pid); a = next(a for a in p['assets'] if a['id'] in done['artifact_ids'])
            result['full_mix']={'path':a['path'],'sample_count':a['sample_count'],'channels':a['channels']}
            assert a['sample_count']==6123520 and a['channels']==2
            result['checks'].append('Real test7 full restoration + C stereo chain renders 127.57 s')
            play=page.get_by_role('button',name='播放当前混音选区',exact=True)
            wait(lambda:play.is_enabled(),180)
            page.locator('audio').evaluate_all('(els)=>els.forEach(a=>a.muted=true)')
            play.click()
            page.wait_for_function('Array.from(document.querySelectorAll("audio")).some(a=>!a.paused && a.currentTime>.2)')
            page.get_by_role('button',name='暂停混音试听',exact=True).click()
            assert '文件访问失败' not in page.locator('body').inner_text()
            result['checks'].append('Real mixed selection playback advances; waveform files load')
            panel.scroll_into_view_if_needed()
            page.screenshot(path=str(OUT/'reports/mix-space-soft.png'))
            # Separate browser context to verify classic theme without reload init override.
            other=browser.new_page(viewport={'width':1920,'height':1080})
            other.add_init_script('localStorage.setItem("sonora-project",'+json.dumps(pid)+');localStorage.setItem("sonora-ui-theme","classic");')
            other.goto(base+'/#token='+token+'&endpoint='+base+'/api/v1')
            other.get_by_role('button',name='成品与混音',exact=False).first.click()
            other.locator('button.channel-name').filter(has_text='候选 3').click()
            other.get_by_label('混音效果设置',exact=True).wait_for()
            other.get_by_label('混音效果设置',exact=True).scroll_into_view_if_needed()
            other.screenshot(path=str(OUT/'reports/mix-space-classic.png'))
            result['checks'].append('Both soft and classic interfaces expose the same effects')
            assert not result['errors']; result['passed']=True; browser.close()
    finally:
        server.terminate()
        try: server.wait(timeout=10)
        except subprocess.TimeoutExpired: server.kill(); server.wait(timeout=10)
        log.close(); (OUT/'reports/browser-verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    if '--seed' in sys.argv: seed()
    else: main()
