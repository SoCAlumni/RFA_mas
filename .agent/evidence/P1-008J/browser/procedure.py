"""Actual fresh Chrome; synthetic local data on the temporary 18780 server; no external traffic."""
import json
from datetime import UTC, datetime
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

origin = "http://127.0.0.1:18780"
out = Path("/tmp/rfa-state-browser-0927")
out.mkdir(exist_ok=True)
started = datetime.now(UTC).isoformat()
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless=True)
    context = browser.new_context(viewport={"width":1440,"height":1000})
    outbound, errors, live_snapshots = [], [], []
    def allowed(route):
        if route.request.url.startswith(origin+"/"):
            route.continue_()
        else:
            outbound.append(route.request.url.split("?")[0]); route.abort()
    context.route("**/*", allowed)
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(origin+"/ui/")
    expect(page.locator("#status")).to_contain_text("연결됨")
    card = page.locator("#process-card")
    expect(card).to_be_visible()
    expect(page.locator("#process-title")).to_contain_text("아직 처리한 요청이 없어요")
    page.locator("#create-session").click()
    def send(text, expected):
        page.locator("#chat-input").fill(text)
        page.get_by_role("button", name="메시지 보내기").click()
        # Live: the card switches to 진행 중 before the answer is complete.
        expect(page.locator("#process-status")).to_have_text("진행 중", timeout=5000)
        live_snapshots.append(page.locator("#process-list li").count())
        expect(page.locator("#send-message")).to_be_enabled(timeout=30000)
        expect(page.locator("#process-status")).to_have_text("완료")
        expect(page.locator("#process-title")).to_have_text(text)
        answer=page.locator(".message.assistant").last
        expect(answer).to_contain_text(expected)
        return answer
    send("메모: 헬리오스 벤치마크 계획은 초안 상태입니다.","저장했어요")
    expect(page.locator("#process-list li")).to_have_count(4)
    expect(card).to_contain_text("의도: 저장 요청")
    expect(card).to_contain_text("적합한 담당 없음")
    send("헬리오스 지연 벤치마크 결과를 검증해줘","Task")
    items = page.locator("#process-list li")
    expect(items).to_have_count(7)
    expect(card).to_contain_text("새 Task 팀 구성 중: benchmark")
    expect(card).to_contain_text("예정 역할: supervisor, paper_scout, experiment_runner, result_analyst")
    expect(card).to_contain_text("새 Task 팀 구성 완료")
    expect(card).to_contain_text("구성: 새로 생성 · benchmark")
    for role in ("supervisor","paper_scout","experiment_runner","result_analyst"):
        expect(card.locator(".stage-detail", has_text=role+":")).to_have_count(2)  # composition + result rows
    expect(card).to_contain_text("실험값 simulated(mock)")
    send("헬리오스 지연 결과 알려줘","Task")
    expect(page.locator("#process-list li")).to_have_count(6)
    expect(card).to_contain_text("기존 Task 팀 구성 확인")
    expect(card).to_contain_text("근거: 기존 Task 목표와 주제 일치 · 검토한 내 Task 팀 1개")
    expect(card).to_contain_text("구성: 기존 팀 재사용")
    # The card stays above the bubbles, independent of the answer text, while scrolling.
    box_card = card.bounding_box(); box_msgs = page.locator("#messages").bounding_box()
    assert box_card["y"] < box_msgs["y"]
    page.screenshot(path=str(out/"state-desktop.png"),full_page=False)
    page.reload()
    expect(page.locator(".message.assistant")).to_have_count(3)
    expect(page.locator("#process-title")).to_have_text("헬리오스 지연 결과 알려줘")
    expect(page.locator("#process-list li")).to_have_count(6)
    expect(card).to_contain_text("기존 팀 재사용")
    page.set_viewport_size({"width":390,"height":844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(out/"state-mobile.png"),full_page=False)
    assert not errors and not outbound
    result={"status":"passed","started_at":started,"finished_at":datetime.now(UTC).isoformat(),
            "browser":browser.version,"live_stage_counts_at_first_observation":live_snapshots,
            "checks":["card visible with empty state","live 진행 중 before completion","note: 4 stages incl. intent/routing reason",
            "new task: 7 stages incl. team_spawn planned roles, team composition (4 roles), team_result per-role rows, simulated flag",
            "reuse: 6 stages incl. existing composition + routing candidates","card positioned above bubbles",
            "reload restores last turn state list","mobile no overflow"],
            "page_errors":errors,"external_requests":outbound,"mode":"actual Chrome / local mock"}
    (out/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False)); browser.close()
