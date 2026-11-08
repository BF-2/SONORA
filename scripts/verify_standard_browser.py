"""Headless UI acceptance against an isolated service and restored test project.

Run with a Python environment containing Playwright; backend uses project .venv.
"""
import json
import os
import re
import subprocess
import time
import uuid
import urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'runtime' / 'standard-workflow-acceptance'


def main():
    verification = json.loads((OUT / 'verification.json').read_text(encoding='utf-8'))
    assert verification['passed'], 'Run verify_standard_workflow.py first.'
    token = uuid.uuid4().hex
    port = 8879
    base = f'http://127.0.0.1:{port}'
    env = {**os.environ, 'SONORA_DATA': str(OUT / 'data'), 'SONORA_SESSION_TOKEN': token, 'SONORA_PORT': str(port)}
    log = (OUT / 'browser-service.log').open('w', encoding='utf-8')
    server = subprocess.Popen([str(ROOT / '.venv/Scripts/python.exe'), '-m', 'services.orchestrator.app', '--port', str(port)],
        cwd=ROOT, env=env, stdout=log, stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    result = {'passed': False, 'checks': [], 'page_errors': [], 'production_database_modified': False}
    def api(path, body=None):
        request = urllib.request.Request(base + '/api/v1' + path, data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    try:
        for _ in range(150):
            try:
                api('/health'); break
            except Exception:
                if server.poll() is not None:
                    raise RuntimeError('Isolated UI backend exited; see browser-service.log')
                time.sleep(.2)
        # Separate UI clone keeps the real-engine verification project unchanged.
        recipe = api('/projects/' + verification['project_id'] + '/recipe')
        boundary = 'sonora_' + uuid.uuid4().hex
        payload = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="browser.json"\r\nContent-Type: application/json\r\n\r\n'.encode()
                   + json.dumps(recipe).encode() + f'\r\n--{boundary}--\r\n'.encode())
        request = urllib.request.Request(base + '/api/v1/projects/restore', data=payload,
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'multipart/form-data; boundary=' + boundary})
        with urllib.request.urlopen(request, timeout=30) as response:
            project = json.load(response)
        pid = project['id']; result['project_id'] = pid
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True,
                executable_path='C:/Users/ROG/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe')
            page = browser.new_page(viewport={'width': 1920, 'height': 1080})
            page.on('pageerror', lambda e: result['page_errors'].append(str(e)))
            page.add_init_script('localStorage.setItem("sonora-project", ' + json.dumps(pid) + ');')
            page.goto(base + '/#token=' + token + '&endpoint=' + base + '/api/v1')
            page.get_by_role('button', name=re.compile('音高与歌词')).first.wait_for()

            def stage(name):
                page.get_by_role('button', name=re.compile(name)).first.click()
                page.wait_for_timeout(800)

            stage('音高与歌词')
            page.get_by_role('button', name='Follower 伴唱 · Lock', exact=True).click()
            select = page.get_by_label('当前 F0 编辑版本')
            select.wait_for()
            assert select.locator('option').count() == 3
            options = select.locator('option').evaluate_all('(els) => els.map(e => e.value)')
            select.select_option(options[0]); page.wait_for_timeout(650)
            assert api('/projects/' + pid)['voice_parts']['backing_vocal']['pitch_edits'][0]['semitones'] == -2
            select.select_option(options[2]); page.wait_for_timeout(650)
            page.get_by_role('button', name='+ 新建 Edit 4', exact=True).click()
            expect(select.locator('option')).to_have_count(4)
            page.get_by_role('button', name='保存版本', exact=True).click()
            page.wait_for_timeout(600)
            page.screenshot(path=str(OUT / 'ui-follower-f0.png'), full_page=True)
            page.get_by_role('button', name='Main 主唱 · Lock', exact=True).click()
            page.wait_for_timeout(700)
            assert select.locator('option').count() == 1
            result['checks'].append('Follower Edit 1/2/3/4 create/save/switch; Main has its own curve version')

            stage('生成与验收')
            workspace = page.get_by_label('单轨处理工作区')
            workspace.get_by_role('button', name='1 · 选择输入', exact=True).click()
            workspace.get_by_role('button', name='继续 · 2 · 编辑输入', exact=True).click()
            assert page.get_by_role('button', name=re.compile('打开曲线编辑器')).is_visible()
            workspace.get_by_role('button', name='继续 · 3 · 选择模型', exact=True).click()
            page.get_by_text('声音与具体 Epoch / checkpoint', exact=True).wait_for()
            workspace.get_by_role('button', name='继续 · 4 · 设置参数', exact=True).click()
            page.get_by_text('RVC 参数 · 默认值 / 推荐设置 / 高级能力', exact=True).wait_for()
            page.wait_for_timeout(700)
            page.screenshot(path=str(OUT / 'ui-parameters.png'), full_page=True)
            workspace.get_by_role('button', name='继续 · 5 · Generate / Check', exact=True).click()
            before = api('/projects/' + pid)
            page.get_by_role('button', name='Generate · 生成当前单轨候选', exact=True).click()
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                current = api('/projects/' + pid)
                if len(current['candidates']) > len(before['candidates']):
                    break
                time.sleep(.4)
            assert len(current['candidates']) == len(before['candidates']) + 1
            candidate = current['candidates'][-1]
            assert current['final_list'] == before['final_list'] and candidate['review_status'] == 'pending'
            row = page.locator('[data-candidate-id="' + candidate['id'] + '"]'); row.wait_for(timeout=20000)
            row.get_by_role('button', name='试听版本', exact=True).click()
            page.get_by_role('button', name='A · 原始输入', exact=True).click()
            page.get_by_role('button', name='B · 当前候选', exact=True).click()
            page.get_by_role('button', name='播放 / 继续', exact=True).click()
            page.wait_for_function('document.querySelector("audio")?.currentTime > 0.2')
            page.get_by_role('button', name='A · 原始输入', exact=True).click()
            page.wait_for_function('document.querySelector("audio")?.currentTime > 0.3')
            page.get_by_role('button', name='暂停', exact=True).click()
            row.get_by_role('button', name='重试 · 返回编辑', exact=True).click()
            page.get_by_role('button', name=re.compile('打开曲线编辑器')).wait_for()
            assert len(api('/projects/' + pid)['separation_runs']) == 2
            workspace.get_by_role('button', name='5 · Generate / Check', exact=True).click()
            row.get_by_role('button', name='接受 · 加入 Final List', exact=True).click()
            page.wait_for_timeout(800)
            assert len(api('/projects/' + pid)['final_list']) == len(before['final_list']) + 1
            page.screenshot(path=str(OUT / 'ui-check.png'), full_page=True)
            result['checks'].append('five sequential steps; Generate produces pending candidate; A/B; retry without separation; explicit acceptance')

            stage('成品与混音')
            names = page.get_by_label('成品轨道名称', exact=True)
            assert names.count() == 4
            names.last.fill('浏览器验收 · 独立成品')
            names.last.press('Tab'); page.wait_for_timeout(900)
            page.reload(); names.last.wait_for(); assert names.last.input_value() == '浏览器验收 · 独立成品'
            last = page.locator('[data-final-id]').last
            last.get_by_role('checkbox').check(); page.wait_for_timeout(800)
            last.get_by_role('button', name='删除成品条目', exact=True).click(); page.wait_for_timeout(800)
            assert names.count() == 3
            assert len(api('/projects/' + pid)['final_list']) == 3
            page.locator('[data-final-id]').first.get_by_role('button', name='独立试听', exact=True).click()
            play_mix = page.get_by_role('button', name='播放当前混音选区', exact=True)
            expect(play_mix).to_be_enabled(timeout=30000)
            play_mix.click()
            page.wait_for_function('document.querySelector("audio")?.currentTime > 0.2')
            page.get_by_role('button', name='暂停混音试听', exact=True).click()
            page.get_by_role('button', name='渲染完整混音', exact=True).click()
            full_mix = page.get_by_role('button', name='监听完整混音 · 全长', exact=True)
            expect(full_mix).to_be_enabled(timeout=30000)
            full_mix.click()
            page.wait_for_function('document.querySelector("audio")?.duration > 12.04')
            page.screenshot(path=str(OUT / 'ui-final-list.png'), full_page=True)
            result['checks'].append('Final List independent rename/mute/delete and actual solo playback; full mix preview; refresh restoration')
            assert not result['page_errors'], result['page_errors']
            result['passed'] = True
            browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill(); server.wait(timeout=10)
        log.close()
        (OUT / 'browser-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': result['passed'], 'checks': len(result['checks']), 'page_errors': result['page_errors']}, ensure_ascii=True))


if __name__ == '__main__':
    main()
