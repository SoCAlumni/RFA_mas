"""Actual fresh Chrome; synthetic local data on the temporary 18780 server; no external traffic."""
import json
from datetime import UTC, datetime
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

origin = "http://127.0.0.1:18780"
out = Path("/tmp/rfa-teams-browser-0927")
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
    select = page.locator("#chat-assignee")
    expect(select).to_have_value("")
    expect(page.locator("#assignee-tasks option")).to_have_count(1)
    expect(page.locator("#assignee-tasks option").first).to_contain_text("아직 Task 팀 없음")
    page.locator("#create-session").click()
    def send(text, expected, stages=4):
        page.locator("#chat-input").fill(text)
        page.get_by_role("button", name="메시지 보내기").click()
        expect(page.locator("#send-message")).to_be_enabled(timeout=30000)
        answer=page.locator(".message.assistant").last
        expect(answer).to_contain_text(expected)
        expect(answer.locator(".stage-timeline li")).to_have_count(stages)
        return answer
    note=send("메모: 헬리오스 벤치마크 계획은 초안 상태입니다.","저장했어요")
    expect(note.locator(".assignee")).to_contain_text("비서 직접 처리")
    expect(page.locator("#assignee-tasks option").first).to_contain_text("아직 Task 팀 없음")
    created=send("헬리오스 지연 벤치마크 결과를 검증해줘","Task")
    expect(created.locator(".assignee").first).to_contain_text("새 Task 팀")
    expect(created.locator(".stage-timeline")).to_contain_text("적합한 기존 Task 팀 없음")
    expect(created).to_contain_text("simulated")
    options = page.locator("#assignee-tasks option")
    expect(options).to_have_count(1)
    expect(options.first).to_contain_text("헬리오스 지연 벤치마크")
    expect(options.first).to_contain_text("Benchmark")
    assert not options.first.is_disabled()
    follow=send("헬리오스 지연 결과 알려줘","Task")
    expect(follow.locator(".assignee").first).to_contain_text("Task 팀 · 헬리오스")
    expect(page.locator("#assignee-tasks option")).to_have_count(1)
    # Explicit selection of the Task team for an unrelated question.
    task_value = options.first.get_attribute("value")
    select.select_option(task_value)
    explicit=send("마감이 언제야?","Task")
    expect(explicit.locator(".assignee").first).to_contain_text("Task 팀 · 헬리오스")
    expect(select).to_have_value(task_value)  # selection preserved after refresh of the list
    select.select_option("")
    other=send("안드로메다 점심 메뉴 알려줘","관련 근거를 찾지 못했어요")
    expect(other.locator(".assignee")).to_contain_text("비서 직접 처리")
    page.screenshot(path=str(out/"teams-desktop.png"),full_page=True)
    assert len(streams)==5
    for response in streams:
        assert response.status==200 and "application/x-ndjson" in response.headers["content-type"]
    page.reload()
    expect(page.locator(".message.assistant")).to_have_count(5)
    expect(page.locator("#assignee-tasks option")).to_have_count(1)
    page.set_viewport_size({"width":390,"height":844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(out/"teams-mobile.png"),full_page=True)
    assert not errors and not outbound
    result={"status":"passed","started_at":started,"finished_at":datetime.now(UTC).isoformat(),
            "browser":browser.version,"checks":["empty task list placeholder","note never creates a Task team",
            "explicit request creates one Task team (new_task, simulated)","dropdown lists the Task team first (Benchmark)",
            "related question auto-routed to the Task team","explicit selection routes unrelated question to the Task team",
            "auto/no-match still falls back to assistant","5 real NDJSON streams","history+list replay after reload","mobile no overflow"],
            "page_errors":errors,"external_requests":outbound,"mode":"actual Chrome / local mock"}
    (out/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False)); browser.close()
