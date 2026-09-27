"""Actual fresh Chrome; synthetic local data; no external traffic."""
import json
from datetime import UTC, datetime
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

origin = "http://127.0.0.1:18780"
out = Path("/tmp/rfa-routing-browser-0927")
out.mkdir(exist_ok=True)
started = datetime.now(UTC).isoformat()
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless=True)
    context = browser.new_context(viewport={"width":1440,"height":1000})
    outbound, errors, streams = [], [], []
    def allowed(route):
        if route.request.url.startswith(origin+"/"):
            route.continue_()
        else:
            outbound.append(route.request.url.split("?")[0]); route.abort()
    context.route("**/*", allowed)
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("response", lambda response: streams.append(response) if response.url.endswith("/chat/stream") else None)
    page.goto(origin+"/ui/")
    expect(page.locator("#status")).to_contain_text("연결됨")
    expect(page.locator("#chat-domain")).to_have_value("")
    page.locator("#create-session").click()
    def send(text, expected):
        page.locator("#chat-input").fill(text)
        page.get_by_role("button", name="메시지 보내기").click()
        expect(page.locator("#send-message")).to_be_enabled(timeout=20000)
        answer=page.locator(".message.assistant").last
        expect(answer).to_contain_text(expected)
        expect(answer.locator(".stage-timeline li")).to_have_count(4)
        return answer
    note=send("메모: 오로라 라우팅 시연의 마감은 10월 3일입니다.","저장했어요")
    expect(note.locator(".assignee")).to_contain_text("비서 직접 처리")
    answer=send("오로라 라우팅 마감 알려줘","10월 3일")
    expect(answer.locator(".assignee")).to_contain_text("비서 직접 처리")
    answer=send("양자화 자료 알려줘","양자화 연구 담당")
    expect(answer.locator(".assignee")).to_contain_text("도메인 담당")
    answer=send("안드로메다 점심 메뉴 알려줘","관련 근거를 찾지 못했어요")
    expect(answer.locator(".assignee")).to_contain_text("비서 직접 처리")
    page.screenshot(path=str(out/"routing-desktop.png"),full_page=True)
    assert len(streams)==4
    for response in streams:
        assert response.status==200 and "application/x-ndjson" in response.headers["content-type"]
        # Fetch consumes streaming bodies. Chrome may evict those CDP resources;
        # inspect the actual rendered per-event timeline, plus the transport header.
    for timeline in page.locator(".stage-timeline").all():
        expect(timeline.locator("li")).to_have_count(4)
        expect(timeline).to_contain_text("담당자 확인:")
    page.reload()
    expect(page.locator(".message.assistant")).to_have_count(4)
    expect(page.locator(".stage-timeline")).to_have_count(4)
    page.set_viewport_size({"width":390,"height":844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(out/"routing-mobile.png"),full_page=True)
    assert not errors and not outbound
    result={"status":"passed","started_at":started,"finished_at":datetime.now(UTC).isoformat(),
            "browser":browser.version,"checks":["automatic default","note/query assistant fallback","domain assignee displayed",
            "unmatched question not forced","4 real NDJSON stages + result for each request","history replay","mobile no overflow"],
            "page_errors":errors,"external_requests":outbound,"mode":"actual Chrome / local mock"}
    (out/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False)); browser.close()
