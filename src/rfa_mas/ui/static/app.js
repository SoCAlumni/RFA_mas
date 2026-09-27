"use strict";
// All user/source strings use textContent. No browser persistence or HTML rendering.
(() => {
  let csrfToken = null, currentSession = null, busy = false, historyVersion = 0;
  let notes = [], selectedNote = null;
  const byId = (id) => document.getElementById(id);
  const labels = {store_note:"KB에 저장", query:"내 자료 검색", external_draft:"공개 초안", clarify:"확인 필요"};
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


  function switchTab(name) {
    ["chat","kb","reviews"].forEach((tab) => {
      byId("panel-" + tab).hidden = tab !== name;
      byId("tab-" + tab).classList.toggle("active", tab === name);
      if (tab === name) byId("tab-" + tab).setAttribute("aria-current","page");
      else byId("tab-" + tab).removeAttribute("aria-current");
    });
    byId("page-title").textContent = {chat:"비서 채팅",kb:"내 KB",reviews:"승인함"}[name];
    if (name === "kb") loadNotes();
    if (name === "reviews") loadReviews();
  }
  async function loadStatus() {
    try {
      const s = await api("GET","/ui/api/status");
      byId("status").textContent = s.core.reachable ? "로컬 연결됨 · mock" : "코어 연결 안 됨";
      byId("reload-reviews").disabled = s.features.manual_review !== "enabled";
    } catch(e) { byId("status").textContent = "연결 확인 필요"; showError(byId("global-error"),e); }
  }
  async function loadSessions() {
    const sessions = await api("GET","/ui/api/chat/sessions");
    clear(byId("sessions"));
    sessions.sort((a,b) => b.updated_at.localeCompare(a.updated_at));
    sessions.forEach((s) => {
      const item=el("li"), button=el("button",s.title || "새 대화");
      button.type="button"; button.classList.toggle("selected",s.session_id===currentSession);
      button.addEventListener("click",() => { if (!busy) selectSession(s.session_id); });
      item.append(button); byId("sessions").append(item);
    });
    return sessions;
  }
  function message(role,text) {
    const article=el("article",null,"message "+role);
    if(role==="assistant") article.append(el("div","RFA 비서","speaker"));
    article.append(el("div",text,"bubble")); return article;
  }
  function renderTurn(turn) {
    const list=byId("messages"); list.append(message("user",turn.text));
    const answer=message("assistant",displayReply(turn));
    answer.querySelector(".speaker").append(el("span",labels[turn.intent]||"응답","route-label"));
    if(turn.route && turn.route.label) {
      const kind={task:"Task 팀",domain:"도메인 담당",assistant:"비서"}[turn.route.kind];
      answer.append(el("p",kind+" · "+turn.route.label,"assignee"));
    }
    if(turn.stages && turn.stages.length) {
      const timeline=el("ol",null,"stage-timeline");
      turn.stages.forEach(s=>timeline.append(el("li",s.label)));
      answer.append(timeline);
    }
    if(turn.evidence && turn.evidence.length) {
      const detail=el("details",null,"run-detail"); detail.append(el("summary","검색 근거"));
      turn.evidence.forEach(ref=>detail.append(el("p",ref.source_id+" · "+ref.source_revision)));
      answer.append(detail);
    }
    const actions=el("div",null,"message-meta");
    if(turn.source_id) {
      const b=el("button","저장한 메모 보기 ↗","text-button"); b.type="button";
      b.addEventListener("click",async()=>{ switchTab("kb"); await selectNote(turn.source_id); });
      actions.append(b);
    }
    if(turn.run && turn.run.draft) {
      const detail=el("details",null,"run-detail"), summary=el("summary","근거 · 실행 정보");
      detail.append(summary,el("p","로컬 검색 · mock 답변 / "+turn.run.status));
      const refs=turn.run.draft.allowed_evidence || [];
      refs.forEach((ref)=>detail.append(el("p",ref.source_id+" · "+ref.source_revision)));
      detail.append(el("pre", turn.reply));
      answer.append(detail);
    }
    if(turn.intent==="external_draft" && turn.run && turn.run.draft) {
      const b=el("button","승인함에서 검토하기 →","text-button"); b.type="button";
      b.addEventListener("click",()=>switchTab("reviews")); actions.append(b);
      answer.append(el("p","공개용 초안입니다. 승인 전 게시되지 않으며, 게시도 로컬 mock입니다.","muted"));
    }
    answer.append(actions); list.append(answer);
  }
  function displayReply(turn) {
    const draft=turn.run && turn.run.draft;
    // Only shorten the known deterministic mock presentation for personal reading.
    // Original content stays in details. Public drafts and approved payloads are untouched.
    const mock=turn.run && (turn.run.adapters||[]).some(a=>a.port==="model" && a.adapter==="mock-model" && a.simulated);
    if(!draft || !mock || turn.intent!=="query") return turn.reply;
    const split=turn.reply.indexOf("\n\n허용된 근거:\n");
    if(split<0) return turn.reply;
    let text=turn.reply.slice(split+"\n\n허용된 근거:\n".length);
    const footer="\n\n이 초안은 합성/공개 fixture와 결정적 mock 모델로 생성되었습니다.";
    if(text.endsWith(footer)) text=text.slice(0,-footer.length);
    (draft.allowed_evidence||[]).forEach((ref)=>{
      const location=ref.location && (ref.location.section || ref.location.uri);
      text=text.replace(" ["+ref.source_id+"@"+ref.source_revision+" / "+location+"]","");
    });
    return "저장된 자료에서 찾았어요.\n\n"+text;
  }
  async function selectSession(id) {
    const version=++historyVersion; currentSession=id; switchTab("chat");
    try {
      const turns=await api("GET","/ui/api/sessions/"+encodeURIComponent(id)+"/chat");
      if(version!==historyVersion) return;
      const list=byId("messages"); clear(list);
      if(!turns.length) list.append(welcome.cloneNode(true));
      turns.forEach(renderTurn); wireExamples(); list.scrollTop=list.scrollHeight;
      await loadSessions();
    } catch(e){ showError(byId("global-error"),e); }
  }
  async function newSession() {
    if(busy) return;
    const s=await api("POST","/ui/api/sessions",{}); await selectSession(s.session_id);
    byId("chat-input").focus();
  }
  async function send(event) {
    event.preventDefault();
    const input=byId("chat-input"), text=input.value.trim();
    if(!text || busy) return;
    busy=true; byId("send-message").disabled=true; byId("create-session").disabled=true;
    byId("chat-notice").textContent="비서가 메시지를 확인하고 있어요…";
    byId("messages").setAttribute("aria-busy","true");
    clear(byId("global-error"));
    try {
      if(!currentSession) {
        const s=await api("POST","/ui/api/sessions",{}); currentSession=s.session_id;
      }
      const list=byId("messages"), empty=list.querySelector(".welcome"); if(empty) empty.remove();
      const pending=message("user",text); list.append(pending);
      const progress=message("assistant","요청 접수 중…");
      const timeline=el("ol",null,"stage-timeline live"); progress.append(timeline); list.append(progress);
      list.scrollTop=list.scrollHeight;
      await streamChat(currentSession, {
        text, message_id:newKey("chat"), domain_id:byId("chat-domain").value || null
      }, (stage)=>{
        progress.querySelector(".bubble").textContent=stage.label;
        timeline.append(el("li",stage.label));
        byId("chat-notice").textContent=stage.label;
        list.scrollTop=list.scrollHeight;
      });
      input.value=""; byId("chat-notice").textContent="";
      await selectSession(currentSession); await loadNotes();
    } catch(e) {
      byId("chat-notice").textContent="전송 결과를 확인해 주세요. 자동 재전송하지 않습니다.";
      showError(byId("global-error"),e);
    } finally {
      busy=false; byId("send-message").disabled=false; byId("create-session").disabled=false;
      byId("messages").setAttribute("aria-busy","false"); input.focus();
    }
  }
  async function streamChat(session, body, onStage) {
    const response=await fetch("/ui/api/sessions/"+encodeURIComponent(session)+"/chat/stream",{
      method:"POST",credentials:"same-origin",cache:"no-store",redirect:"error",
      headers:{"Content-Type":"application/json","X-RFA-CSRF":await ensureCsrf()},
      body:JSON.stringify(body)
    });
    if(!response.ok || !response.body) throw {code:"chat_rejected",message:"요청을 처리할 수 없습니다."};
    const reader=response.body.getReader(), decoder=new TextDecoder();
    let buffer="", finished=false;
    function consume(line) {
      if(!line.trim()) return;
      const event=JSON.parse(line);
      if(event.type==="stage") onStage(event);
      if(event.type==="result") finished=true;
      if(event.type==="error") throw event;
    }
    try {
      while(true) {
        const {value,done}=await reader.read();
        buffer+=decoder.decode(value,{stream:!done});
        let end;
        while((end=buffer.indexOf("\n"))>=0) {consume(buffer.slice(0,end));buffer=buffer.slice(end+1);}
        if(done) break;
      }
      if(buffer.trim()) consume(buffer);
      if(!finished) throw {code:"outcome_unknown",message:"연결이 종료되었습니다. 대화를 다시 조회하세요.",query_required:true};
    } finally { await reader.cancel(); reader.releaseLock(); }
  }
  function noteMatches(n) {
    const q=byId("kb-search").value.toLowerCase(), domain=byId("kb-domain").value;
    return (!domain || n.document.domain_id===domain) &&
      (n.document.title+" "+n.document.content).toLowerCase().includes(q);
  }
  function renderNotes() {
    const list=byId("notes"); clear(list);
    const filtered=notes.filter(noteMatches);
    byId("kb-count").textContent=String(notes.length);
    byId("kb-summary").textContent=filtered.length+"개의 자료 · 현재 접근 가능한 자료만 표시";
    if(!filtered.length) list.append(el("p","아직 자료가 없어요. 채팅에서 메모를 남겨보세요.","empty"));
    filtered.forEach((note)=>{
      const doc=note.document, card=el("button",null,"note-card");
      card.type="button";card.classList.toggle("selected",selectedNote===doc.source_id);
      card.append(el("h3",doc.title),el("p",doc.content),el("small",(doc.audience==="private"?"비공개":doc.audience)+" · "+doc.domain_id+" · v"+note.revision_number));
      card.addEventListener("click",()=>selectNote(doc.source_id));list.append(card);
    });
  }
  async function loadNotes() {
    try { notes=await api("GET","/ui/api/notes"); renderNotes(); }
    catch(e){ notes=[]; clear(byId("notes"));clear(byId("note-detail"));showError(byId("notes"),e); }
  }
  async function selectNote(id) {
    selectedNote=id; renderNotes();
    const detail=byId("note-detail"); clear(detail);
    try {
      const note=await api("GET","/ui/api/notes/"+encodeURIComponent(id));
      if(selectedNote!==id)return;
      const doc=note.document; detail.append(el("h3",doc.title),el("span",doc.audience==="private"?"나만 보는 비공개 메모":doc.audience,"badge"),el("pre",doc.content));
      const meta=el("dl");
      [["자료 공간",doc.domain_id],["출처",doc.source_id],["버전",doc.source_revision],["수정 시각",note.created_at||doc.updated_at||"기록 없음"]].forEach(([k,v])=>{meta.append(el("dt",k),el("dd",v));});
      detail.append(meta);
    } catch(e){showError(detail,e);}
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
    card.dataset.draftId = view.draft_id;
    card.append(el("h3", "공개 초안 · 버전 " + view.version));
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
            await api("POST", "/ui/api/runs/" + encodeURIComponent(view.run_id) + "/refresh-review", {});
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
      const publicViews = views.filter((v) => v.target.audience === "public");
      byId("review-count").textContent = String(publicViews.filter(v => v.decision === "pending").length);
      if (!publicViews.length && views.length) target.append(el("p", "공개용 초안이 없습니다. 개인 검색 답변은 채팅에서 확인하세요.", "muted"));
      publicViews.forEach((view) => target.append(renderReview(view)));
    } catch (error) {
      showError(target, error);
    }
  }


  let welcome;
  function wireExamples() {
    document.querySelectorAll("[data-example]").forEach((button) => {
      button.onclick=()=>{byId("chat-input").value=button.dataset.example;byId("chat-input").focus();};
    });
  }
  document.addEventListener("DOMContentLoaded",async()=>{
    welcome=byId("welcome").cloneNode(true); wireExamples();
    ["chat","kb","reviews"].forEach((name)=>byId("tab-"+name).addEventListener("click",()=>switchTab(name)));
    byId("chat-form").addEventListener("submit",send);
    byId("chat-input").addEventListener("keydown",(e)=>{if(e.key==="Enter"&&!e.shiftKey&&!e.isComposing){e.preventDefault();byId("chat-form").requestSubmit();}});
    byId("create-session").addEventListener("click",()=>newSession().catch(e=>showError(byId("global-error"),e)));
    byId("reload-sessions").addEventListener("click",()=>loadSessions().catch(e=>showError(byId("global-error"),e)));
    byId("reload-notes").addEventListener("click",loadNotes);
    byId("reload-reviews").addEventListener("click",loadReviews);
    byId("kb-search").addEventListener("input",renderNotes);
    byId("kb-domain").addEventListener("change",renderNotes);
    await loadStatus(); await Promise.all([loadNotes(),loadReviews()]);
    try { const sessions=await loadSessions(); if(sessions.length)await selectSession(sessions[0].session_id); }
    catch(e){showError(byId("global-error"),e);}
  });
})();
