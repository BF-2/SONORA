import json,re
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'engine-envs/haysay'
def main():
    session=json.loads((ROOT/'runtime/six-stage-main-session.json').read_text())
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True,executable_path='C:/Users/ROG/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe')
        page=b.new_page(viewport={'width':1440,'height':1050});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto('http://127.0.0.1:8765/#token='+session['token']);page.wait_for_timeout(1600)
        buttons=page.get_by_role('button').all_text_contents()
        print('MODEL_CONTROLS',[(x.strip()) for x in buttons if any(y in x for y in ['声音','模型'])][:20])
        stage=page.get_by_role('button',name=re.compile('声音转换')).first
        if stage.count():stage.click();page.wait_for_timeout(800)
        manage=page.get_by_role('button',name='打开模型管理').first
        if not manage.count():manage=page.get_by_role('button',name=re.compile('声音与模型')).first
        if manage.count():manage.click();page.wait_for_timeout(1200)
        body=page.locator('body').inner_text()
        assert 'Hay Say' in body,body[:300]
        assert not any(x in body for x in ['pony_singing_r18m','pony_speech_v1','therealvul'])
        page.screenshot(path=str(OUT/'sonora-models.png'),full_page=True)
        assert not errors,errors
        (OUT/'browser-verification.json').write_text(json.dumps({'passed':True,'no_page_errors':True,'Hay_Say_visible':True,'old_model_names_absent':True},indent=2),encoding='utf-8');b.close()
if __name__=='__main__':main()
