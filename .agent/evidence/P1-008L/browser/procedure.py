"""Fresh Chrome against the temporary 18780 server (synthetic data only, no external traffic)."""
import json
from datetime import UTC, datetime
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

origin = "http://127.0.0.1:18780"
out = Path("/tmp/rfa-teams-browser-0927")
out.mkdir(exist_ok=True)
started = datetime.now(UTC).isoformat()
checks = []
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless=True)
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    outbound, errors = [], []
    def allowed(route):
        if route.request.url.startswith(origin + "/"):
            route.continue_()
        else:
            outbound.append(route.request.url.split("?")[0]); route.abort()
    context.route("**/*", allowed)
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(origin + "/ui/")
    expect(page.locator("#status")).to_contain_text("연결됨")
    expect(page.locator("#team-count")).to_have_text("2")
    checks.append("sidebar team count 2")
    page.locator("#tab-teams").click()
    expect(page.locator("#page-title")).to_have_text("팀 에이전트")
    cards = page.locator(".team-card")
    expect(cards).to_have_count(2)
    checks.append("two team cards")
    aurora = cards.filter(has_text="오로라 벤치마크")
    nebula = cards.filter(has_text="네뷸라 INT4")
    expect(aurora).to_contain_text("Benchmark 팀")
    expect(aurora).to_contain_text("TRIV3")
    expect(aurora).to_contain_text("팀 준비됨 · ready")
    expect(aurora).to_contain_text("local runtime · local")
    for role in ("supervisor", "paper_scout", "experiment_runner", "result_analyst"):
        expect(aurora.locator(".member-list li", has_text=role)).to_have_count(1)
    expect(aurora).to_contain_text("tool: metric_compare")
    expect(aurora).to_contain_text("최근 실행 (1)")
    expect(aurora).to_contain_text("simulated(mock)")
    expect(aurora).to_contain_text("연결된 KB (10)")
    expect(aurora.locator(".source-list").first.locator(".source-row")).to_have_count(10)
    expect(aurora).to_contain_text("실행에서 인용된 근거")
    expect(aurora.locator(".source-row", has_text="인용:").first).to_be_visible()
    checks.append("benchmark card: roles, tools, run, 10 linked, cited evidence")
    expect(nebula).to_contain_text("Research 팀")
    expect(nebula).to_contain_text("양자화 연구")
    for role in ("supervisor", "source_scout", "evidence_reviewer"):
        expect(nebula.locator(".member-list li", has_text=role)).to_have_count(1)
    expect(nebula).to_contain_text("연결된 KB (6)")
    checks.append("research card: roles, 6 linked")
    page.screenshot(path=str(out / "teams-desktop.png"), full_page=True)
    # Unlink one source, then re-link it from the subject matches.
    first_linked = aurora.locator(".source-list").first.locator(".source-row").first
    title = first_linked.locator(".text-button").first.inner_text()
    first_linked.get_by_role("button", name="해제").click()
    aurora = page.locator(".team-card").filter(has_text="오로라 벤치마크")
    expect(aurora).to_contain_text("연결된 KB (9)")
    expect(aurora).to_contain_text("주제 일치 자료 · 미연결 (1)")
    aurora.locator(".source-row", has_text=title).get_by_role("button", name="연결").click()
    aurora = page.locator(".team-card").filter(has_text="오로라 벤치마크")
    expect(aurora).to_contain_text("연결된 KB (10)")
    expect(aurora).to_contain_text("주제 일치 자료 · 미연결 (0)")
    checks.append("unlink then re-link from subject matches: " + title)
    # Clicking a linked source opens it in the KB tab.
    aurora.locator(".source-list").first.locator(".source-row").first.locator(".text-button").first.click()
    expect(page.locator("#page-title")).to_have_text("내 KB")
    expect(page.locator("#note-detail h3")).to_be_visible()
    checks.append("linked source opens KB detail: " + page.locator("#note-detail h3").inner_text())
    # Reload restores the tab data (server-side ledger, no browser storage).
    page.reload()
    expect(page.locator("#team-count")).to_have_text("2")
    page.locator("#tab-teams").click()
    expect(page.locator(".team-card")).to_have_count(2)
    checks.append("reload keeps 2 teams")
    storage = page.evaluate("() => [localStorage.length, sessionStorage.length]")
    page.set_viewport_size({"width": 390, "height": 844})
    overflow = page.evaluate("() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")
    page.screenshot(path=str(out / "teams-mobile.png"), full_page=False)
    checks.append("mobile 390px no horizontal overflow: " + str(not overflow))
    browser_version = browser.version
    browser.close()
result = {"status": "passed" if not errors and not outbound and not overflow and storage == [0, 0] else "failed",
          "started_at": started, "finished_at": datetime.now(UTC).isoformat(), "browser": browser_version,
          "checks": checks, "js_errors": errors, "outbound": outbound, "browser_storage": storage, "overflow": overflow}
(out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
print(json.dumps(result, ensure_ascii=False, indent=2))

