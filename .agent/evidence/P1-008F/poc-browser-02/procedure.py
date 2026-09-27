import json
from datetime import UTC, datetime
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

origin = 'http://127.0.0.1:18780'
checks = []
started = datetime.now(UTC).isoformat()
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless=True)
    context = browser.new_context()
    outbound, errors = [], []
    def route(r):
        if r.request.url.startswith(origin + '/'):
            r.continue_()
        else:
            outbound.append(r.request.url.split('?')[0]); r.abort()
    context.route('**/*', route)
    page = context.new_page()
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(origin + '/ui/')
    expect(page.locator('#status')).to_contain_text('연결됨')
    page.locator('#create-session').click()
    expect(page.locator('#submit-work')).to_be_enabled()
    page.locator('#note-title').fill('PoC 합성 메모')
    page.locator('#note-content').fill('POC-BROWSER-PRIVATE-901')
    page.locator('#create-note').click()
    expect(page.locator('#note-result')).to_contain_text('저장됨')
    checks.append('create session and save private synthetic note')
    page.locator('#work-domain').select_option('triv3')
    page.locator('#work-audience').select_option('public')
    page.locator('#work-query').fill('TRIV3 공개 트랙')
    page.locator('#submit-work').click()
    expect(page.locator('#work-result')).to_contain_text('waiting_approval')
    assert 'POC-BROWSER-PRIVATE-901' not in page.locator('#work-result').inner_text()
    pending_card = page.locator('#reviews article').filter(has=page.get_by_role('button', name='승인', exact=True))
    draft_id = pending_card.locator('h3').inner_text().split()[0]
    pending_card.get_by_role('button', name='승인', exact=True).click()
    expect(page.locator('#reviews')).to_contain_text('approved')
    page.locator('#work-result').get_by_role('button', name='검토 상태 다시 조회', exact=True).click()
    expect(page.locator('#work-result')).to_contain_text('completed')
    page.locator('#reviews article').filter(has=page.get_by_role('heading', name=draft_id, exact=False)).get_by_role('button', name='모의 게시 (mock receipt)', exact=True).click()
    expect(page.locator('#reviews')).to_contain_text('게시 상태: succeeded')
    checks.append('public draft -> manual approval -> resume -> mock publication receipt')
    assert not errors and not outbound
    assert page.evaluate('localStorage.length + sessionStorage.length') == 0
    checks.append('no page errors, external outbound or browser storage')
    page.screenshot(path='/tmp/rfa-poc-browser-0927/ui.png', full_page=True)
    result = dict(status='passed', source_commit='6c92479', started_at=started,
                  finished_at=datetime.now(UTC).isoformat(), browser=browser.version,
                  mode='real browser/local mock product', checks=checks,
                  limits='Not real publication, NVIDIA, OpenShell or teammate services')
    Path('/tmp/rfa-poc-browser-0927/browser-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False))
    browser.close()
