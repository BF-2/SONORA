"""Browser acceptance for navigation, F0 erasure/undo and light-theme surfaces."""
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
import httpx
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'runtime/editor-followup'
DATA = ROOT / 'runtime/standard-workflow-acceptance/data'

DARK_SURFACES = """() => [...document.querySelectorAll('body *')].flatMap(el => {
  const r=el.getBoundingClientRect(), s=getComputedStyle(el);
  if(r.width<45 || r.height<24 || s.visibility==='hidden' || s.display==='none' || Number(s.opacity)===0) return [];
  if(document.querySelector('dialog[open]') && !el.closest('dialog')) return [];
  const c=s.backgroundColor.match(/[\\d.]+/g)?.map(Number) || [];
  if(c.length<3 || (c.length>3 && c[3]<.8) || Math.max(...c.slice(0,3))>95) return [];
  return [{tag:el.tagName,cls:el.className,color:s.backgroundColor,text:el.innerText?.slice(0,70)}];
})"""


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex; port = 8884; base = f'http://127.0.0.1:{port}'
    env = {**os.environ, 'SONORA_DATA': str(DATA), 'SONORA_SESSION_TOKEN': token, 'SONORA_PORT': str(port), 'SONORA_SKIP_CATALOG_SCAN': '1'}
    log = (OUT / 'browser-service.log').open('wb')
    server = subprocess.Popen([str(ROOT / '.venv/Scripts/python.exe'), '-m', 'services.orchestrator.app', '--port', str(port)],
        cwd=ROOT, env=env, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
    client = httpx.Client(base_url=base + '/api/v1', headers={'Authorization': 'Bearer ' + token}, timeout=20)
    def api(method, path, **kwargs):
        response = client.request(method, path, **kwargs); response.raise_for_status(); return response.json()
    report = {'passed': False, 'checks': [], 'page_errors': [], 'dark_surfaces': {}}
    try:
        for _ in range(100):
            try:api('GET', '/health'); break
            except httpx.TransportError:time.sleep(.2)
        source_id = json.loads((ROOT / 'runtime/standard-workflow-acceptance/verification.json').read_text(encoding='utf-8'))['project_id']
        recipe = api('GET', '/projects/' + source_id + '/recipe')
        project = api('POST', '/projects/restore', files={'file': ('ui-test.json', json.dumps(recipe).encode(), 'application/json')})
        pid = project['id']; report['project_id'] = pid
        def current():return api('GET', '/projects/' + pid)
        def wait_saved(predicate):
            for _ in range(100):
                p = current()
                if predicate(p):return p
                time.sleep(.1)
            raise AssertionError('UI save did not reach expected state')
        def target(p):
            part = p['voice_parts']['backing_vocal']
            return api('POST', f'/projects/{pid}/pitch-target', json={'pitch_artifact_id': part['parameters']['pitch_artifact_id'],
                'source_artifact_id': part['source_artifact_id'], 'parameters': part['parameters'],
                'pitch_edits': part['pitch_edits'], 'notes': part['notes']})
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path='C:/Users/ROG/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe')
            page = browser.new_page(viewport={'width': 1920, 'height': 1080})
            page.on('pageerror', lambda error: report['page_errors'].append(str(error)))
            page.add_init_script('localStorage.setItem("sonora-project", ' + json.dumps(pid) + ')')
            page.goto(base + '/#token=' + token + '&endpoint=' + base + '/api/v1')
            page.get_by_role('button', name='柔和 UI', exact=True).click()
            for index, label in enumerate(['导入与信息', '两级分离', '音高与歌词', '生成与验收', '成品与混音', '导出']):
                page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                page.locator('button.nav-step').nth(index).click()
                page.wait_for_timeout(700)
                assert page.evaluate('window.scrollY') < 2, label
                report['dark_surfaces'][label] = page.evaluate(DARK_SURFACES)
            page.locator('button.nav-step').nth(3).click()
            for label in ['2 · 编辑输入', '3 · 选择模型', '4 · 设置参数', '5 · Generate / Check']:
                page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                page.get_by_role('button', name=label, exact=True).click()
                page.wait_for_timeout(500)
                assert page.evaluate('window.scrollY') < 2, label
                report['dark_surfaces'][label] = page.evaluate(DARK_SURFACES)
            report['checks'].append('six workflow pages and generation subpages start at the top')
            for label in ['声音与模型', '引擎与设置', '队列']:
                page.get_by_role('button', name=label, exact=False).first.click()
                page.locator('dialog[open]').wait_for(); page.wait_for_timeout(400)
                report['dark_surfaces'][label] = page.evaluate(DARK_SURFACES)
                page.screenshot(path=str(OUT / ('light-' + label + '.png')))
                page.get_by_role('button', name='关闭弹窗', exact=True).click()
            page.locator('button.nav-step').nth(2).click()
            page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            page.get_by_role('button', name='Follower 伴唱 · Lock', exact=True).click()
            page.wait_for_timeout(600)
            assert page.evaluate('window.scrollY') < 2
            versions = page.get_by_label('当前 F0 编辑版本')
            first_edit = versions.input_value()
            min_gap = page.get_by_label('最小静音区间数值', exact=True)
            smoothing = page.get_by_label('静音边界平滑长度数值', exact=True)
            gate_toggle = page.get_by_label('当前 Edit 无音高区域静音', exact=True)
            min_gap.fill('120'); smoothing.fill('15'); smoothing.press('Tab')
            settings_a = {'enabled': True, 'min_silence_ms': 120, 'fade_ms': 15}
            wait_saved(lambda p: p['voice_parts']['backing_vocal']['parameters'].get('voicing_settings') == settings_a)
            count = versions.locator('option').count()
            page.get_by_role('button', name=f'+ 新建 Edit {count + 1}', exact=True).click()
            expect(versions.locator('option')).to_have_count(count + 1)
            second_edit = versions.input_value()
            min_gap.fill('200'); smoothing.fill('40'); gate_toggle.uncheck()
            settings_b = {'enabled': False, 'min_silence_ms': 200, 'fade_ms': 40}
            wait_saved(lambda p: p['voice_parts']['backing_vocal']['parameters'].get('voicing_settings') == settings_b)
            versions.select_option(first_edit)
            expect(gate_toggle).to_be_checked(); expect(min_gap).to_have_value('120'); expect(smoothing).to_have_value('15')
            versions.select_option(second_edit)
            expect(gate_toggle).not_to_be_checked(); expect(min_gap).to_be_disabled()
            page.reload(); versions.wait_for()
            expect(gate_toggle).not_to_be_checked(); expect(min_gap).to_have_value('200'); expect(smoothing).to_have_value('40')
            page.get_by_role('button', name='Main 主唱 · Lock', exact=True).click()
            expect(gate_toggle).to_be_checked(); expect(min_gap).to_have_value('0'); expect(smoothing).to_have_value('3')
            page.get_by_role('button', name='Follower 伴唱 · Lock', exact=True).click()
            expect(gate_toggle).not_to_be_checked(); expect(min_gap).to_have_value('200')
            versions.select_option(first_edit)
            expect(gate_toggle).to_be_checked()
            report['checks'].append('per-Edit gate switch, minimum gap and fade settings survive version/source switching and reload')
            report['voicing_edits'] = {'first': first_edit, 'second': second_edit, 'first_settings': settings_a, 'second_settings': settings_b}
            canvas = page.locator('canvas.pitch-canvas')
            def stroke(tool, left, right):
                page.get_by_role('button', name=tool, exact=True).click()
                canvas.scroll_into_view_if_needed(); box = canvas.bounding_box()
                width = box['width'] - 48; y = box['y'] + box['height'] * .45
                page.mouse.move(box['x'] + 48 + width * left, y)
                page.mouse.down(); page.mouse.move(box['x'] + 48 + width * right, y + 8, steps=14); page.mouse.up()
            prior = current(); original_count = len(prior['voice_parts']['backing_vocal']['pitch_edits'])
            stroke('连续绘制目标', .12, .22)
            drawn = wait_saved(lambda p: len(p['voice_parts']['backing_vocal']['pitch_edits']) > original_count)
            before = target(drawn)
            assert any(edit.get('locked') for edit in drawn['voice_parts']['backing_vocal']['pitch_edits'])
            stroke('擦除音高', .14, .2)
            erased = wait_saved(lambda p: p['voice_parts']['backing_vocal']['pitch_edits'][-1]['operation'] == 'unvoiced')
            after = target(erased); edit = erased['voice_parts']['backing_vocal']['pitch_edits'][-1]
            affected = [i for i in range(len(after['f0_hz'])) if edit['start_tick'] <= (i * after['hop'] + after['frame_center_offset']) * 48000 / after['sample_rate'] < edit['end_tick']]
            assert affected and all(after['f0_hz'][i] is None for i in affected)
            page.screenshot(path=str(OUT / 'pitch-eraser.png'))
            page.get_by_role('button', name='撤销音高编辑', exact=True).click()
            restored = wait_saved(lambda p: p['voice_parts']['backing_vocal']['pitch_edits'] == drawn['voice_parts']['backing_vocal']['pitch_edits'])
            assert target(restored)['f0_hz'] == before['f0_hz']
            page.get_by_role('button', name='重做', exact=True).last.click()
            wait_saved(lambda p: p['voice_parts']['backing_vocal']['pitch_edits'][-1]['operation'] == 'unvoiced')
            page.reload(); canvas.wait_for(); page.wait_for_timeout(800)
            assert target(current())['f0_hz'] == after['f0_hz']
            report['checks'].append('eraser clears locked and unlocked pitch; single-stroke undo/redo and reload preserve exact target')
            report['erase_ticks'] = [edit['start_tick'], edit['end_tick']]
            recipe = api('GET', f'/projects/{pid}/recipe')
            (OUT / 'browser-project.sonora.json').write_text(json.dumps(recipe, ensure_ascii=False), encoding='utf-8')
            report['dark_surfaces']['pitch-editor'] = page.evaluate(DARK_SURFACES)
            report['passed'] = not report['page_errors'] and not any(report['dark_surfaces'].values())
            browser.close()
    finally:
        client.close(); server.terminate(); server.wait(timeout=15); log.close()
        (OUT / 'browser-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True))
    assert report['passed'], 'Inspect browser-verification.json for remaining theme surfaces.'


if __name__ == '__main__':main()
