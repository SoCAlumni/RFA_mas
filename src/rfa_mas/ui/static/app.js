"use strict";
// All user/source strings use textContent. No browser persistence or HTML rendering.
(() => {
  let csrfToken = null, currentSession = null, busy = false, historyVersion = 0;
  let notes = [], selectedNote = null;
  const byId = (id) => document.getElementById(id);
  const labels = {store_note:"KB에 저장", query:"내 자료 검색", external_draft:"공개 초안", clarify:"확인 필요", task_run:"Task 팀 실행"};
  const routeKinds = {task:"Task 팀", new_task:"새 Task 팀", domain:"자료 공간", assistant:"비서"};
  const routeReasons = {
    explicit_task:"사용자가 Task 팀을 직접 지정", existing_task_subject_match:"기존 Task 목표와 주제 일치",
    ambiguous_tasks:"후보 Task 팀이 동점이라 억지 배정 안 함", explicit_domain:"자료 공간 직접 지정",
    domain_subject_match:"자료 공간 주제 언급(영속 Task 아님)", ambiguous_domains:"자료 공간 후보 모호",
    no_suitable_assignee:"적합한 담당 없음 → 비서 직접 처리", explicit_task_request_no_match:"명시적 요청이지만 기존 팀 없음 → 새 팀"
  };
  let liveTurn = null;
  function stageDetail(s) {
    // Every value is rendered as text. Details never include note bodies, keys or answers.
    const d=s.detail||{}, lines=[];
    if(s.stage==="understanding") lines.push(["의도",(d.intent_label||d.intent||"")+" · "+(d.rule||"")]);
    if(s.stage==="routing" && d.route) {
      const r=d.route;
      lines.push(["담당",(routeKinds[r.kind]||r.kind)+(r.label?" · "+r.label:"")]);
      lines.push(["근거",(routeReasons[r.reason]||r.reason||"")+(typeof r.considered==="number"?" · 검토한 내 Task 팀 "+r.considered+"개":"")]);
      (r.candidates||[]).forEach((c)=>lines.push(["후보",c.goal+" (공통 주제: "+(c.shared_subjects||[]).join(", ")+")"]));
      if(r.task_id) lines.push(["Task",r.task_id+(r.team_id?" · 팀 "+r.team_id:"")]);
    }
    if(s.stage==="team_spawn") { lines.push(["패턴",d.pattern]); lines.push(["예정 역할",(d.planned_roles||[]).join(", ")]); if(d.selector) lines.push(["선택",d.selector]); }
    if(s.stage==="team") {
      lines.push(["구성",(d.spawned?"새로 생성":"기존 팀 재사용")+" · "+d.pattern+(d.team_state?" · 상태 "+d.team_state:"")+(d.runtime_kind?" · runtime "+d.runtime_kind:"")]);
      lines.push(["Task",d.task_id+" · 팀 "+d.team_id]);
      (d.members||[]).forEach((m)=>lines.push([m.role,m.agent_id+(m.capabilities&&m.capabilities.length?" · "+m.capabilities.join(", "):"")+(m.tools&&m.tools.length?" · tools: "+m.tools.join(", "):"")]));
    }
    if(s.stage==="team_result") {
      lines.push(["실행",d.status+(d.stop_reason?" · "+d.stop_reason:"")+(d.simulated?" · 실험값 simulated(mock)":"")]);
      (d.roles||[]).forEach((r)=>lines.push([r.role,r.status+" · steps "+(r.steps||0)+" · tool calls "+(r.tool_calls||0)]));
    }
    if(s.stage==="completed") lines.push(["결과",d.status+(d.run_id?" · run "+d.run_id:"")+(d.source_id?" · source "+d.source_id:"")]);
    if(s.stage==="error") lines.push(["오류",s.message||""]);
    return lines;
  }
  function renderProcess(turn, live) {
    const list=byId("process-list"); clear(list);
    byId("process-title").textContent=turn?turn.text:"아직 처리한 요청이 없어요";
    const status=byId("process-status"); status.classList.toggle("live",Boolean(live));
    status.textContent=!turn?"":live?"진행 중":({stored:"완료",answered:"완료",clarify:"확인 필요",outcome_unknown:"결과 미확정",pending:"결과 미확정"}[turn.status]||turn.status||"");
    if(!turn) return;
    (turn.stages||[]).forEach((s,i,all)=>{
      const item=el("li",null,live&&i===all.length-1?"active":(s.stage==="error"?"error":"done"));
      item.append(el("span",live&&i===all.length-1?"●":(s.stage==="error"?"!":"✓"),"mark"));
      const body=el("div"); body.append(el("span",s.label,"stage-label"));
      stageDetail(s).forEach(([k,v])=>{ const line=el("span",null,"stage-detail"); line.append(el("b",k+": "),document.createTextNode(String(v))); body.append(line); });
      item.append(body); list.append(item);
    });
    if(live) list.scrollTop=list.scrollHeight;
  }
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
    ["chat","kb","teams","reviews"].forEach((tab) => {
      byId("panel-" + tab).hidden = tab !== name;
      byId("tab-" + tab).classList.toggle("active", tab === name);
      if (tab === name) byId("tab-" + tab).setAttribute("aria-current","page");
      else byId("tab-" + tab).removeAttribute("aria-current");
    });
    byId("page-title").textContent = {chat:"비서 채팅",kb:"내 KB",teams:"팀 에이전트",reviews:"승인함"}[name];
    if (name === "kb") loadNotes();
    if (name === "teams") loadTeams();
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
    return loadSessionsInner();
  }
  async function loadAssignees() {
    // Owner's Task teams (one team per Task). Domains stay a storage-space fallback only.
    const group=byId("assignee-tasks"), select=byId("chat-assignee"), keep=select.value;
    let teams=[];
    try { teams=await api("GET","/ui/api/chat/assignees"); } catch(_e) { teams=[]; }
    clear(group);
    teams.forEach((t)=>{
      const option=el("option",(t.selectable?"":"(사용 불가) ")+t.goal+" · "+({benchmark:"Benchmark",research:"Research"}[t.pattern]||t.pattern));
      option.value="task:"+t.task_id; option.disabled=!t.selectable; group.append(option);
    });
    if(!teams.length){ const none=el("option","아직 Task 팀 없음 · “…조사해/검증해”로 만들 수 있어요"); none.disabled=true; group.append(none); }
    if([...select.options].some(o=>o.value===keep && !o.disabled)) select.value=keep; else select.value="";
    return teams;
  }
  function assigneeBody() {
    const value=byId("chat-assignee").value;
    if(value.startsWith("task:")) return {task_id:value.slice(5), domain_id:null};
    if(value.startsWith("domain:")) return {task_id:null, domain_id:value.slice(7)};
    return {task_id:null, domain_id:null};
  }
  async function loadSessionsInner() {
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
      const kind=routeKinds[turn.route.kind]||turn.route.kind;
      answer.append(el("p",kind+" · "+turn.route.label,"assignee"));
    }
    if(turn.team) {
      const t=turn.team;
      answer.append(el("p","Task "+t.task_id+" · 팀 "+t.team_id+" · "+t.pattern+" · "+t.status+(t.simulated?" · 실험값 simulated(mock)":""),"assignee"));
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
      if(!liveTurn) renderProcess(turns.length?turns[turns.length-1]:null,false);
      await loadSessions();
    } catch(e){ showError(byId("global-error"),e); }
  }
  async function newSession() {
    if(busy) return;
    busy=true; byId("send-message").disabled=true; byId("create-session").disabled=true;
    try {
      const s=await api("POST","/ui/api/sessions",{}); await selectSession(s.session_id);
      byId("chat-input").focus();
    } finally {
      busy=false; byId("send-message").disabled=false; byId("create-session").disabled=false;
    }
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
      liveTurn={text, stages:[], status:"pending"}; renderProcess(liveTurn,true);
      await streamChat(currentSession, {
        text, message_id:newKey("chat"), ...assigneeBody()
      }, (stage)=>{
        progress.querySelector(".bubble").textContent=stage.label;
        timeline.append(el("li",stage.label));
        byId("chat-notice").textContent=stage.label;
        liveTurn.stages.push(stage); renderProcess(liveTurn,true);
        list.scrollTop=list.scrollHeight;
      }, (result)=>{ liveTurn=null; renderProcess(result,false); });
      input.value=""; byId("chat-notice").textContent="";
      await selectSession(currentSession); await Promise.all([loadNotes(),loadAssignees()]);
    } catch(e) {
      byId("chat-notice").textContent="전송 결과를 확인해 주세요. 자동 재전송하지 않습니다.";
      if(liveTurn){ liveTurn.stages.push({stage:"error",label:"오류 · 결과를 확인해 주세요",message:e.message||e.code||""}); liveTurn.status="outcome_unknown"; renderProcess(liveTurn,false); liveTurn=null; }
      showError(byId("global-error"),e);
    } finally {
      busy=false; byId("send-message").disabled=false; byId("create-session").disabled=false;
      byId("messages").setAttribute("aria-busy","false"); input.focus();
    }
  }
  async function streamChat(session, body, onStage, onResult) {
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
      if(event.type==="result") { finished=true; if(onResult) onResult(event.result); }
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
  function openNote(sourceId) {
    switchTab("kb");
    selectNote(sourceId).catch((e)=>showError(byId("global-error"),e));
  }
  const ROLE_LABELS={supervisor:"Supervisor · 취합/요약",paper_scout:"논문 조사",experiment_runner:"실험 로그 파싱",result_analyst:"결과 비교 (검증)",source_scout:"자료 조사",evidence_reviewer:"근거 검토 (검증)"};
  const TEAM_STATE_LABELS={ready:"준비됨",provisioning:"구성 중",running:"실행 중",failed:"실패",cancelled:"취소됨",cleaned:"정리됨",unknown:"불명",cleanup_pending:"정리 대기"};
  function sourceRow(source, actions) {
    const row=el("li",null,"source-row");
    const open=el("button",source.title||source.source_id,"text-button");
    open.type="button"; open.disabled=!source.available;
    open.addEventListener("click",()=>openNote(source.source_id));
    row.append(open);
    const meta=[];
    if(!source.available) meta.push("현재 접근 불가/삭제됨");
    if(source.audience) meta.push(source.audience==="private"?"비공개":source.audience);
    if(source.revision_number) meta.push("v"+source.revision_number);
    if(source.roles&&source.roles.length) meta.push("인용: "+source.roles.join(", "));
    if(source.shared&&source.shared.length) meta.push("공통 주제: "+source.shared.join(", "));
    row.append(el("small",meta.join(" · ")));
    (actions||[]).forEach((a)=>row.append(a));
    return row;
  }
  function renderTeam(view) {
    const t=view.task, team=view.team;
    const card=el("article",null,"team-card"); card.dataset.taskId=t.task_id;
    const head=el("div",null,"team-head");
    head.append(el("h3",t.goal));
    const badges=el("div",null,"team-badges");
    badges.append(badge(({benchmark:"Benchmark 팀",research:"Research 팀"})[team.pattern]||team.pattern));
    badges.append(badge(({triv3:"TRIV3",quantization_research:"양자화 연구"})[t.domain_id]||t.domain_id));
    badges.append(badge("Task "+t.status));
    badges.append(badge("팀 "+(TEAM_STATE_LABELS[team.state]||team.state)+" · "+team.reason, view.selectable?"done":"error"));
    badges.append(badge(team.runtime_kind+" runtime · "+team.mode,"mock"));
    head.append(badges);
    card.append(head);
    const ids=el("dl",null,"team-ids");
    [["Task",t.task_id],["팀",team.team_id],["템플릿",team.template_id+" v"+team.template_version],["통신",team.communication],["예산",
      "steps "+team.budget.max_steps+" · tools "+team.budget.max_tool_calls+" · tokens "+team.budget.max_tokens+" · "+team.budget.timeout_seconds+"s · 동시 "+team.budget.concurrency]
    ].forEach(([k,v])=>ids.append(el("dt",k),el("dd",v)));
    card.append(ids);
    const grid=el("div",null,"team-grid");
    const members=el("section"); members.append(el("h4","역할 구성 ("+team.members.length+")"));
    const memberList=el("ol",null,"member-list");
    team.members.forEach((m)=>{
      const item=el("li");
      item.append(el("strong",m.role),el("span"," · "+(ROLE_LABELS[m.role]||"")));
      const detail=[ "agent "+m.agent_id, m.capabilities.length?"capability: "+m.capabilities.join(", "):"capability 없음 (취합 전용)", m.tools.length?"tool: "+m.tools.join(", "):null, "prepare "+m.prepare+(m.failed?" · 실패":"") ].filter(Boolean);
      item.append(el("small",detail.join(" · ")));
      memberList.append(item);
    });
    members.append(memberList); grid.append(members);
    const runs=el("section"); runs.append(el("h4","최근 실행 ("+view.runs.count+")"));
    if(!view.runs.recent.length) runs.append(el("p","아직 이 팀으로 실행한 요청이 없어요. 채팅에서 담당을 선택하거나 관련 주제로 질문해 보세요.","empty"));
    const runList=el("ul",null,"run-list");
    view.runs.recent.forEach((r)=>{
      const item=el("li");
      item.append(el("strong",r.text));
      const roles=r.roles.map((x)=>x.role+":"+x.status).join(", ");
      item.append(el("small",[r.created_at.slice(0,19).replace("T"," "),"run "+r.run_id,"팀 결과 "+(r.team_status||r.chat_status)+(r.simulated?" · simulated(mock)":""),roles].filter(Boolean).join(" · ")));
      runList.append(item);
    });
    runs.append(runList); grid.append(runs);
    card.append(grid);
    const kb=el("section",null,"team-kb");
    kb.append(el("h4","연결된 KB ("+view.linked_sources.length+")"));
    const output=el("div");
    const linked=el("ul",null,"source-list");
    if(!view.linked_sources.length) linked.append(el("li","연결된 자료가 없어요. 아래 주제 일치 자료에서 연결할 수 있어요.","empty"));
    view.linked_sources.forEach((s)=>{
      const unlink=el("button","해제","text-button danger"); unlink.type="button";
      unlink.addEventListener("click",async()=>{unlink.disabled=true;try{const updated=await api("DELETE","/ui/api/teams/"+encodeURIComponent(t.task_id)+"/kb/"+encodeURIComponent(s.source_id));card.replaceWith(renderTeam(updated));}catch(e){unlink.disabled=false;showError(output,e);}});
      linked.append(sourceRow(s,[unlink]));
    });
    kb.append(linked);
    kb.append(el("h4","실행에서 인용된 근거 ("+view.cited_sources.length+")"));
    const cited=el("ul",null,"source-list");
    if(!view.cited_sources.length) cited.append(el("li","최근 실행에서 인용된 근거가 없어요.","empty"));
    view.cited_sources.forEach((s)=>cited.append(sourceRow(s)));
    kb.append(cited);
    kb.append(el("h4","주제 일치 자료 · 미연결 ("+view.subject_matches.length+")"));
    const matches=el("ul",null,"source-list");
    if(!view.subject_matches.length) matches.append(el("li","같은 자료 공간에서 Task 목표와 주제가 겹치는 미연결 자료가 없어요.","empty"));
    view.subject_matches.forEach((s)=>{
      const link=el("button","연결","text-button"); link.type="button";
      link.addEventListener("click",async()=>{link.disabled=true;try{const updated=await api("POST","/ui/api/teams/"+encodeURIComponent(t.task_id)+"/kb",{source_ids:[s.source_id],note:"ui"});card.replaceWith(renderTeam(updated));}catch(e){link.disabled=false;showError(output,e);}});
      matches.append(sourceRow(s,[link]));
    });
    kb.append(matches);
    kb.append(el("p",view.notice,"muted"));
    kb.append(output);
    card.append(kb);
    return card;
  }
  async function loadTeams() {
    const target=byId("teams");
    try {
      const teams=await api("GET","/ui/api/teams");
      clear(target); byId("team-count").textContent=String(teams.length);
      if(!teams.length) target.append(el("p","아직 Task 팀이 없어요. 채팅에서 “…조사해 줘” 또는 “…검증해 줘”라고 요청하면 새 Task 팀이 만들어져요.","empty"));
      teams.forEach((view)=>target.append(renderTeam(view)));
    } catch(e){clear(target);showError(target,e);}
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
    busy=true; byId("send-message").disabled=true; byId("create-session").disabled=true;
    welcome=byId("welcome").cloneNode(true); wireExamples();
    ["chat","kb","teams","reviews"].forEach((name)=>byId("tab-"+name).addEventListener("click",()=>switchTab(name)));
    byId("chat-form").addEventListener("submit",send);
    byId("chat-input").addEventListener("keydown",(e)=>{if(e.key==="Enter"&&!e.shiftKey&&!e.isComposing){e.preventDefault();byId("chat-form").requestSubmit();}});
    byId("create-session").addEventListener("click",()=>newSession().catch(e=>showError(byId("global-error"),e)));
    byId("reload-sessions").addEventListener("click",()=>loadSessions().catch(e=>showError(byId("global-error"),e)));
    byId("reload-notes").addEventListener("click",loadNotes);
    byId("reload-teams").addEventListener("click",loadTeams);
    byId("reload-reviews").addEventListener("click",loadReviews);
    byId("kb-search").addEventListener("input",renderNotes);
    byId("kb-domain").addEventListener("change",renderNotes);
    try {
      await loadStatus(); await Promise.all([loadNotes(),loadReviews(),loadAssignees(),loadTeams()]);
      const sessions=await loadSessions(); if(sessions.length)await selectSession(sessions[0].session_id);
    } catch(e){showError(byId("global-error"),e);}
    finally {busy=false;byId("send-message").disabled=false;byId("create-session").disabled=false;}
  });
})();
