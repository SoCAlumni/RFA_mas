"""Synthetic fresh Chrome smoke; no external requests or real user profile."""
import json
from datetime import UTC, datetime
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

origin = 'http://127.0.0.1:18780'
out = Path('/tmp/rfa-chat-poc-browser-0927')
started = datetime.now(UTC).isoformat()
checks = []
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless=True)
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    errors, outbound = [], []
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
    expect(page.locator('#welcome')).to_be_visible()
    page.screenshot(path=str(out / 'welcome.png'), full_page=True)
    page.locator('#chat-input').fill('아틀라스 프로젝트의 마감은 10월 2일입니다.')
    page.get_by_role('button', name='메시지 보내기').click()
    expect(page.locator('#messages')).to_contain_text('내 KB에 비공개로 저장했어요')
    page.locator('#chat-input').fill('아틀라스 프로젝트 마감 알려줘')
    page.get_by_role('button', name='메시지 보내기').click()
    expect(page.locator('.message.assistant').last).to_contain_text('10월 2일')
    expect(page.locator('.message.assistant').last).to_contain_text('내 자료 검색')
    expect(page.locator('.message.assistant').last.locator('.bubble')).to_contain_text('저장된 자료에서 찾았어요')
    checks.append('declarative note auto-stored; question auto-routed to private core retrieval')
    page.screenshot(path=str(out / 'chat.png'), full_page=True)
    page.get_by_role('button', name='내 KB', exact=False).click()
    expect(page.locator('#panel-kb')).to_be_visible()
    page.locator('#kb-search').fill('아틀라스')
    page.locator('.note-card').first.click()
    expect(page.locator('#note-detail')).to_contain_text('10월 2일')
    expect(page.locator('#note-detail')).to_contain_text('비공개')
    page.screenshot(path=str(out / 'kb.png'), full_page=True)
    checks.append('KB tab filter, full body, private label and source metadata visible')
    page.reload()
    expect(page.locator('#messages')).to_contain_text('10월 2일')
    expect(page.locator('.message.assistant')).to_have_count(2)
    checks.append('reload restores server-side conversation without duplicate turns')
    page.locator('#chat-input').fill('TRIV3 공개 초안 만들어줘')
    page.get_by_role('button', name='메시지 보내기').click()
    expect(page.locator('.message.assistant').last).to_contain_text('공개용 초안')
    assert '10월 2일' not in page.locator('.message.assistant').last.inner_text()
    page.locator('#tab-reviews').click()
    card = page.locator('.review').filter(has=page.get_by_role('button', name='승인', exact=True)).last
    draft_id = card.get_attribute('data-draft-id')
    card.get_by_role('button', name='승인', exact=True).click()
    approved = page.locator('.review[data-draft-id="' + draft_id + '"]')
    expect(approved).to_contain_text('approved')
    approved.get_by_role('button', name='모의 게시 (mock receipt)', exact=True).click()
    expect(approved).to_contain_text('게시 상태: succeeded')
    checks.append('public answer excludes private note; manual approval then mock receipt')
    page.locator('#tab-chat').click()
    page.locator('#chat-input').fill('메모: <img src=x onerror="alert(1)"> 합성 안전 렌더링')
    page.get_by_role('button', name='메시지 보내기').click()
    expect(page.locator('.message.assistant').last).to_contain_text('저장했어요')
    assert page.locator('#messages img').count() == 0
    checks.append('HTML-like note remains text')
    page.set_viewport_size({'width':390, 'height':844})
    expect(page.locator('#chat-input')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    page.screenshot(path=str(out / 'mobile.png'), full_page=True)
    page.locator('#tab-kb').click()
    expect(page.locator('#kb-search')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    checks.append('390px mobile chat + KB navigation; no horizontal overflow')
    assert not errors and not outbound, {'errors':errors,'outbound':outbound}
    assert page.evaluate('localStorage.length + sessionStorage.length') == 0
    result = dict(status='passed', started_at=started, finished_at=datetime.now(UTC).isoformat(),
                  browser=browser.version, checks=checks, page_errors=errors, outbound=outbound,
                  mode='actual Chrome/local mock', limits='not NVIDIA, real publication or teammate UI')
    (out / 'browser-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result,ensure_ascii=False))
    browser.close()
