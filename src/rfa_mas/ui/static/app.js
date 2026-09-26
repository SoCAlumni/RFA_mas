"use strict";
// Plain DOM only: every value is rendered with textContent. No HTML injection,
// no eval, no browser storage, no credentials other than the same-origin cookie.
(() => {
  let csrfToken = null;
  let currentSession = null;
  let pendingWorkId = null;
  const byId = (id) => document.getElementById(id);

  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function newKey(prefix) {
    return prefix + "-" + crypto.randomUUID();
  }

  async function ensureCsrf() {
    if (csrfToken === null) {
      const response = await fetch("/ui/api/csrf", { credentials: "same-origin", cache: "no-store" });
      csrfToken = (await response.json()).csrf_token;
    }
    return csrfToken;
  }

  async function api(method, path, body, idempotencyKey) {
    const headers = { Accept: "application/json" };
    const init = { method, headers, credentials: "same-origin", cache: "no-store", redirect: "error" };
    if (method !== "GET") {
      headers["X-RFA-CSRF"] = await ensureCsrf();
      if (body !== undefined) {
        headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(body);
      }
      if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
    }
    let response;
    try {
      response = await fetch(path, init);
    } catch (_error) {
      throw {
        code: method === "GET" ? "upstream_unavailable" : "outcome_unknown",
        message: "요청 결과를 확인할 수 없습니다. 자동 재시도하지 않습니다. 상태를 다시 조회하세요.",
        query_required: true,
      };
    }
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      const details = (data && data.details) || {};
      throw {
        code: (data && data.code) || "error",
        message: (data && data.message) || "요청을 처리할 수 없습니다.",
        upstream_code: details.upstream_code || "",
        query_required: Boolean(details.query_required),
      };
    }
    return data;
  }

  function showError(target, error) {
    clear(target);
    const box = el("div", null, "error");
    box.append(el("strong", "오류: " + (error.code || "error")));
    if (error.upstream_code) box.append(el("span", " (" + error.upstream_code + ")"));
    box.append(el("p", error.message || "요청을 처리할 수 없습니다."));
    if (error.query_required) {
      box.append(el("p", "조회 필요: 성공으로 추정하지 않으며 자동 재시도/재게시하지 않습니다.", "warning"));
    }
    target.append(box);
  }

  function badge(text, kind) {
    return el("span", text, "badge " + (kind || ""));
  }

  async function loadStatus() {
    const target = byId("status");
    try {
      const status = await api("GET", "/ui/api/status");
      clear(target);
      target.append(badge("UI: " + status.ui.mode, "local"));
      target.append(badge("core: " + (status.core.reachable ? "연결됨" : "연결 안 됨"), status.core.reachable ? "ok" : "bad"));
      const review = status.review;
      const reviewText = review.configured
        ? "검토: " + (review.reachable ? (review.mode || "unknown") + " / " + (review.authority || "") : "연결 안 됨")
        : "검토: 미설정";
      target.append(badge(reviewText, "mock"));
      target.append(badge("실험: " + status.features.experiments, "notrun"));
      Object.entries(status.gates).forEach(([name, value]) => target.append(badge(name + ": " + value, "notrun")));
      const unsupported = byId("unsupported");
      clear(unsupported);
      Object.entries(status.features)
        .filter(([, value]) => value === "disabled" || value === "not_run")
        .forEach(([name, value]) => {
          const item = el("li");
          const button = el("button", name);
          button.type = "button";
          button.disabled = true;
          item.append(button, el("span", " " + value, "muted"));
          unsupported.append(item);
        });
      const reviewOn = status.features.manual_review === "enabled";
      byId("reload-reviews").disabled = !reviewOn;
    } catch (error) {
      showError(target, error);
    }
  }

  async function loadSessions() {
    const list = byId("sessions");
    try {
      const sessions = await api("GET", "/ui/api/sessions");
      clear(list);
      sessions.forEach((session) => {
        const item = el("li");
        const button = el("button", session.session_id);
        button.type = "button";
        button.addEventListener("click", () => selectSession(session.session_id));
        item.append(button, el("span", " " + session.updated_at, "muted"));
        list.append(item);
      });
    } catch (error) {
      showError(list, error);
    }
  }

  async function selectSession(sessionId) {
    currentSession = sessionId;
    byId("work-session").textContent = "선택한 세션: " + sessionId;
    byId("submit-work").disabled = false;
    const target = byId("session-detail");
    try {
      const detail = await api("GET", "/ui/api/sessions/" + encodeURIComponent(sessionId));
      clear(target);
      const messages = el("ul");
      detail.messages.forEach((message) => messages.append(el("li", message.role + ": " + message.content)));
      const runs = el("ul");
      detail.runs.forEach((run) => {
        const item = el("li", run.run_id + " — " + run.status);
        const refresh = el("button", "검토 상태 다시 조회");
        refresh.type = "button";
        refresh.disabled = run.status !== "waiting_approval";
        refresh.addEventListener("click", () => refreshReview(run.run_id));
        item.append(refresh);
        runs.append(item);
      });
      target.append(el("h3", "메시지"), messages, el("h3", "실행"), runs);
    } catch (error) {
      showError(target, error);
    }
  }

  function renderRun(result) {
    const target = byId("work-result");
    clear(target);
    target.append(el("p", "상태: " + result.status + " / " + result.stop_reason));
    target.append(badge(result.simulated ? "mock/simulated" : "local", result.simulated ? "mock" : "local"));
    if (result.draft) {
      target.append(el("h3", "DRAFT v" + result.draft.version + " → " + result.draft.target.audience));
      target.append(el("pre", result.draft.content));
    }
    if (result.review) {
      target.append(el("p", "검토: " + result.review.decision + " — " + result.review.safe_reason));
    }
    if (result.status === "waiting_approval") {
      target.append(el("p", "검토 대기: 아래 수동 검토에서 결정한 뒤 '검토 상태 다시 조회'를 누르세요.", "warning"));
      const refresh = el("button", "검토 상태 다시 조회");
      refresh.type = "button";
      refresh.addEventListener("click", () => refreshReview(result.run_id));
      target.append(refresh);
    }
    (result.errors || []).forEach((error) => target.append(el("p", error.code + ": " + error.message, "error")));
  }

  async function submitWork() {
    if (!currentSession) return;
    const body = {
      query: byId("work-query").value,
      target_audience: byId("work-audience").value,
      client_request_id: pendingWorkId || (pendingWorkId = newKey("ui-work")),
    };
    const domain = byId("work-domain").value;
    if (domain) body.domain_id = domain;
    try {
      const result = await api("POST", "/ui/api/sessions/" + encodeURIComponent(currentSession) + "/work", body);
      pendingWorkId = null;
      renderRun(result);
      await selectSession(currentSession);
      await loadReviews();
    } catch (error) {
      showError(byId("work-result"), error);
    }
  }

  async function refreshReview(runId) {
    try {
      renderRun(await api("POST", "/ui/api/runs/" + encodeURIComponent(runId) + "/refresh-review", {}));
      if (currentSession) await selectSession(currentSession);
    } catch (error) {
      showError(byId("work-result"), error);
    }
  }

  async function loadNotes() {
    const list = byId("notes");
    try {
      const notes = await api("GET", "/ui/api/notes");
      clear(list);
      notes.forEach((note) => {
        list.append(el("li", note.document.title + " [" + note.document.audience + ", rev " + note.revision_number + "]"));
      });
    } catch (error) {
      showError(list, error);
    }
  }

  async function createNote() {
    const body = {
      domain_id: byId("note-domain").value,
      title: byId("note-title").value,
      content: byId("note-content").value,
    };
    try {
      await api("POST", "/ui/api/notes", body, newKey("ui-note"));
      clear(byId("note-result"));
      byId("note-result").append(el("p", "저장됨(비공개)."));
      await loadNotes();
    } catch (error) {
      showError(byId("note-result"), error);
    }
  }

  function decisionBody(view, decision) {
    return {
      draft_version: view.version,
      content_hash: view.content_hash,
      payload_hash: view.payload_hash,
      target: view.target,
      decision,
    };
  }

  function renderReceipt(target, publication) {
    const receipt = publication.receipt;
    target.append(badge("모의 게시 영수증 (mock, 실제 게시 아님)", "mock"));
    target.append(el("p", "게시 상태: " + receipt.status + " / " + (receipt.external_result_ref || "결과 참조 없음")));
    if (receipt.status === "outcome_unknown") {
      target.append(el("p", "결과 불확실: 조회만 가능하며 자동 재게시하지 않습니다.", "warning"));
    }
  }

  function renderReview(view) {
    const card = el("article", null, "review");
    card.append(el("h3", view.draft_id + " v" + view.version + " (" + view.contract + ")"));
    card.append(badge(view.mode + " / " + view.authority, "mock"));
    card.append(el("p", "결정: " + view.decision + (view.decided_by ? " by " + view.decided_by : "")));
    card.append(el("p", "대상: " + view.target.audience + " / " + view.target.channel + " / " + view.target.destination));
    card.append(el("pre", view.content));
    const output = el("div");
    if (view.decision === "pending" && !view.superseded) {
      [["approved", "승인"], ["revision_requested", "수정 요청"], ["rejected", "거절"]].forEach(([decision, label]) => {
        const button = el("button", label);
        button.type = "button";
        button.addEventListener("click", async () => {
          try {
            await api("POST", "/ui/api/reviews/" + encodeURIComponent(view.draft_id) + "/decision", decisionBody(view, decision), newKey("ui-decision"));
            await loadReviews();
          } catch (error) {
            showError(output, error);
          }
        });
        card.append(button);
      });
    }
    if (view.approval && view.approval.decision === "approved") {
      const publish = el("button", "모의 게시 (mock receipt)");
      publish.type = "button";
      publish.addEventListener("click", async () => {
        publish.disabled = true;
        try {
          const publication = await api("POST", "/ui/api/publications", {
            approval_id: view.approval.approval_id,
            draft_id: view.draft_id,
            version: view.version,
            payload_hash: view.payload_hash,
          }, newKey("ui-publish"));
          clear(output);
          renderReceipt(output, publication);
        } catch (error) {
          showError(output, error);
        }
      });
      card.append(publish);
    }
    card.append(output);
    return card;
  }

  async function loadReviews() {
    const target = byId("reviews");
    if (byId("reload-reviews").disabled) return;
    try {
      const views = await api("GET", "/ui/api/reviews");
      clear(target);
      if (views.length === 0) target.append(el("p", "검토할 초안이 없습니다.", "muted"));
      views.forEach((view) => target.append(renderReview(view)));
    } catch (error) {
      showError(target, error);
    }
  }

  document.addEventListener("DOMContentLoaded", async () => {
    byId("create-session").addEventListener("click", async () => {
      try {
        const session = await api("POST", "/ui/api/sessions");
        await loadSessions();
        await selectSession(session.session_id);
      } catch (error) {
        showError(byId("session-detail"), error);
      }
    });
    byId("reload-sessions").addEventListener("click", loadSessions);
    byId("submit-work").addEventListener("click", submitWork);
    byId("create-note").addEventListener("click", createNote);
    byId("reload-reviews").addEventListener("click", loadReviews);
    await loadStatus();
    await Promise.all([loadSessions(), loadNotes(), loadReviews()]);
  });
})();
