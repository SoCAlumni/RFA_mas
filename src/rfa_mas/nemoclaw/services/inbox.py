"""결재함 (D-20, D-22, D-23): RFA_module's approvals (state, draft, publishing) merged with what rfa_mas
knows about the same request (intake: grade, task, stages, injection, blocked attempts).

One item per mention URL: it exists from the moment the head request arrives ("drafting") and keeps
the same id once the desk files the approval. Approve = 「바로 응답」 (RFA_module publishes), reject
with the owner's text = 「재생성 요청」 (the desk rewrites; the 3rd one closes the approval)."""

from __future__ import annotations

import datetime as dt
import time

from rfa_mas.nemoclaw import audit
from rfa_mas.nemoclaw.services.approvals import ApprovalsBackend, ApprovalsError
from rfa_mas.nemoclaw.services.intake import IntakeService, item_id_for, title_for
from rfa_mas.nemoclaw.services.presentation import initials_for
from rfa_mas.nemoclaw.store import Store

MAX_ROUNDS = 3                      # RFA_module closes an approval on its 3rd rejection
STALL_SECONDS = 600                 # head answered but no approval filed after this → 「초안 없음」
GRADE_LABEL = {"public": "사외", "company": "사내"}
CHANNEL_LABEL = {"github": "GitHub", "slack": "Slack"}
RULE_LABEL = {"canary": "기밀 표지 문장", "internal-project": "사내 프로젝트명", "money-krw": "금액",
              "percent-metric": "내부 지표 수치", "latency-ms": "지연 수치", "email": "이메일 주소",
              "credential": "자격 증명", "citation-id": "근거 인용 id", "date": "일정·날짜", "llm": "LLM 판정"}
BADGE = {"drafting": ("작성 중", "info"), "pending": ("결재 필요", "warn"), "blocked": ("차단됨", "danger"),
         "regenerating": ("재생성 중", "info"), "posting": ("보내는 중", "info"), "publish_failed": ("게시 실패", "danger"),
         "posted": ("응답 완료", "ok"), "closed": ("결재 완료", "muted"), "stalled": ("초안 없음", "muted")}
INJECTION_NOTE = "표시한 문장은 질문이 아니라 에이전트에게 내리는 지시로 보입니다."
EDIT_UNSUPPORTED = "수정한 초안은 아직 보낼 수 없습니다. 고칠 내용은 재생성 요청으로 보내 주세요."


class InboxError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _epoch(value) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def rule_label(rule: str) -> str:
    return RULE_LABEL.get(rule.split(":", 1)[0], rule)


class InboxService:
    def __init__(self, *, backend: ApprovalsBackend | None, store: Store, intake: IntakeService, tasks):
        self.backend, self.store, self.intake, self.tasks = backend, store, intake, tasks
        self._cache: tuple[float, list[dict]] = (0.0, [])
        self.counts: dict[str, int] = {}
        self._counts_at = 0.0

    # ---- data ---------------------------------------------------------------------------------

    async def _approvals(self, max_age: float = 2.0) -> list[dict]:
        at, cached = self._cache
        if time.time() - at < max_age:
            return cached
        approvals = await self.backend.list() if self.backend else []
        self._cache = (time.time(), approvals)
        self.counts = self._pending_counts(approvals)
        self._counts_at = time.time()
        return approvals

    @staticmethod
    def _pending_counts(approvals: list[dict]) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in approvals:
            if a.get("status") == "pending" and a.get("task"):
                out[a["task"]["id"]] = out.get(a["task"]["id"], 0) + 1
        return out

    async def refresh_counts(self, max_age: float = 5.0) -> dict[str, int]:
        """Pending approvals per task (「맡은 안건 N건」, left-nav badges); stale values on failure."""
        if time.time() - self._counts_at >= max_age:
            try:
                await self._approvals(max_age=0)
            except ApprovalsError:
                self._counts_at = time.time()
        return self.counts

    def _publish_error(self, approval_id: int) -> str | None:
        row = self.store.one("SELECT publish_error FROM inbox_state WHERE approval_id=?", (approval_id,))
        return row["publish_error"] if row else None

    def _set_publish_error(self, approval_id: int, error: str | None) -> None:
        self.store.execute("INSERT OR REPLACE INTO inbox_state (approval_id, publish_error, updated) VALUES (?,?,?)",
                           (approval_id, error, self.store.now()))

    def _agent(self, task_id: str | None) -> dict | None:
        task = self.tasks.get(task_id) if task_id else None
        if not task:
            return None
        return {"id": task["agentId"], "name": task["agentName"], "desk": task["desk"], "icon": task["icon"],
                "color": task["color"], "initials": task["initials"]}

    # ---- items --------------------------------------------------------------------------------

    def _phase(self, approval: dict | None, rows: list[dict] | None = None) -> str:
        if approval is None:
            finished = (rows or [{}])[-1].get("finished") if rows else None
            return "stalled" if finished and time.time() - finished > STALL_SECONDS else "drafting"
        status = approval.get("status")
        if status == "approved":
            return "publish_failed" if self._publish_error(approval["id"]) else "posting"
        return {"pending": "pending", "rejected": "regenerating", "posted": "posted", "closed": "closed"}.get(status, "pending")

    def _item(self, url: str, approval: dict | None, rows: list[dict]) -> dict:
        first, last = (rows[0] if rows else None), (rows[-1] if rows else None)
        channel = (approval or {}).get("channel") or (last or {}).get("channel") or "github"
        grade = (last or {}).get("audience") or (approval or {}).get("audience") or "public"
        task = (approval or {}).get("task") or ({"id": last["task_id"], "name": last["task_name"]}
                                                  if last and last.get("task_id") else None)
        requester = (approval or {}).get("requester") or (first or {}).get("requester") or ""
        phase = self._phase(approval, rows)
        injected = bool(first and self.store.loads(first.get("injection"), []))
        badge_key = "blocked" if phase == "pending" and injected else phase
        aid = (approval or {}).get("id")
        line = {"drafting": "답변 초안 작성 중", "stalled": "대응 에이전트가 초안을 올리지 않았습니다",
                "pending": f"결재 {aid} · " + ("주입 문장 제외 · " if injected else "") + "답변 초안 승인 대기",
                "regenerating": f"결재 {aid} · 재생성 요청 반영 중", "posting": f"결재 {aid} · 보내는 중",
                "publish_failed": f"결재 {aid} · 게시 실패 — 다시 시도할 수 있어요", "posted": f"결재 {aid} · 응답함",
                "closed": f"결재 {aid} · 응답하지 않기로 결정"}[phase]
        title = (first or {}).get("title") or title_for(channel, url, (approval or {}).get("context") or [],
                                                         (approval or {}).get("question") or "")
        arrived = (first or {}).get("started") or _epoch((approval or {}).get("created_at"))
        updated = max((x for x in ((last or {}).get("updated"), _epoch((approval or {}).get("updated_at")), arrived) if x),
                      default=None)
        return {"id": item_id_for(url), "approvalId": aid, "sourceUrl": url, "channel": channel,
                "channelLabel": CHANNEL_LABEL.get(channel, channel), "target": (approval or {}).get("target") or (last or {}).get("target"),
                "requester": requester, "requesterInitials": initials_for(requester) if requester else "",
                "title": title, "grade": grade, "gradeLabel": GRADE_LABEL.get(grade, grade),
                "gradeSource": (last or {}).get("grade_source") or "channel", "status": phase,
                "badge": {"label": BADGE[badge_key][0], "tone": BADGE[badge_key][1]}, "statusLine": line,
                "round": (approval or {}).get("round") or len(rows) or 1, "task": task,
                "agent": self._agent((task or {}).get("id")), "injection": injected, "arrivedAt": arrived,
                "updatedAt": updated}

    async def _merged(self) -> list[tuple[str, dict | None, list[dict]]]:
        approvals = await self._approvals()
        by_url = {a["source_url"]: a for a in approvals}
        intakes = self.intake.all()
        urls = list(dict.fromkeys([*by_url, *intakes]))
        return [(u, by_url.get(u), intakes.get(u, [])) for u in urls]

    async def list(self, *, authenticated: bool, task: str | None = None, status: str | None = None) -> list[dict]:
        items = [self._item(u, a, rows) for u, a, rows in await self._merged()]
        if not authenticated:  # D-23: a guest sees 사외 items only
            items = [i for i in items if i["grade"] == "public"]
        if task:
            items = [i for i in items if (i["task"] or {}).get("id") == task]
        if status == "needs_approval":
            items = [i for i in items if i["status"] == "pending"]
        elif status and status != "all":
            items = [i for i in items if i["status"] == status]
        return sorted(items, key=lambda i: -(i["arrivedAt"] or 0))

    async def summary(self, *, authenticated: bool) -> dict:
        items = await self.list(authenticated=authenticated)
        by_status: dict[str, int] = {}
        by_task: dict[str, int] = {}
        for i in items:
            by_status[i["status"]] = by_status.get(i["status"], 0) + 1
            if i["status"] == "pending" and i["task"]:
                by_task[i["task"]["id"]] = by_task.get(i["task"]["id"], 0) + 1
        return {"needsApproval": by_status.get("pending", 0), "byTask": by_task, "byStatus": by_status,
                "autoReplied": 0, "total": len(items)}

    async def _find(self, item_id: str, authenticated: bool) -> tuple[str, dict | None, list[dict]]:
        for url, approval, rows in await self._merged():
            if item_id_for(url) == item_id:
                grade = (rows[-1] if rows else {}).get("audience") or (approval or {}).get("audience")
                if not authenticated and grade != "public":
                    break
                return url, approval, rows
        raise InboxError(404, "unknown_item", "없는 결재입니다.")

    # ---- detail -------------------------------------------------------------------------------

    def _source_view(self, channel: str, url: str, target: str, question: str, context: list[dict],
                     requester: str, arrived: float | None) -> dict:
        mention = {"author": requester, "text": question, "at": arrived, "isRequest": True}
        if channel == "github":
            repo, _, number = (target or "").partition("#")
            issue_first = "#issuecomment" in url and context and len(context) < 10
            title, body = "", ""
            if issue_first:
                title, _, body = (context[0].get("text") or "").partition("\n")
                comments = context[1:]
            else:
                comments = context
                if "#issuecomment" not in url:  # the mention is the issue body itself
                    body = question
            return {"kind": "github", "url": url, "repo": repo, "number": int(number) if number.isdigit() else None,
                    "issueUrl": url.split("#", 1)[0], "title": title.strip(), "body": body.strip(),
                    "author": (context[0].get("author") if issue_first else requester),
                    "comments": [{"author": c.get("author"), "text": c.get("text"), "at": _epoch(c.get("at"))}
                                 for c in comments] + ([mention] if "#issuecomment" in url else [])}
        channel_id, _, ts = (target or "").partition("/")
        return {"kind": "slack", "url": url, "channelId": channel_id, "threadTs": ts, "dm": channel_id.startswith("D"),
                "messages": [{"author": c.get("author"), "text": c.get("text"), "at": _epoch(c.get("at"))} for c in context]
                            + [mention]}

    def _blocked_attempts(self, request_ids: list[str]) -> list[dict]:
        sids = {f"ask-{r}" for r in request_ids}
        out = []
        for row in audit._rows():
            if row.get("session_id") in sids and row.get("verdict") in ("block", "refused", "denied"):
                d = row.get("detail") or {}
                out.append({"at": row.get("ts"), "kind": row.get("kind"),
                            "action": d.get("action") or row.get("action") or row.get("kind"),
                            "reason": d.get("blocked_by") or d.get("reason") or row.get("verdict")})
        return out[-10:]

    def _steps(self, row: dict | None, approval: dict | None, round_: int, phase: str, grade: str,
               injection: list[dict], blocked: list[dict]) -> list[dict]:
        s = self.store.loads((row or {}).get("steps"), {}) or {}
        grade_label = GRADE_LABEL.get(grade, grade)
        rag, verify = s.get("rag", {}), s.get("verify", {})
        if row is None:  # no rfa_mas record for this round (earlier head, or not asked yet)
            state = "pending" if phase in ("regenerating", "drafting") else "unknown"
            rag_step = {"key": "rag", "title": "RAG 검색", "state": state, "ms": None,
                        "summary": "대기" if state == "pending" else "rfa_mas 기록 없음", "details": []}
            verify_step = {"key": "verify", "title": "검증", "state": state, "ms": None,
                           "summary": "대기" if state == "pending" else "rfa_mas 기록 없음", "details": []}
        else:
            cites = rag.get("citations") or []
            if rag.get("outcome") == "no_task":
                rag_summary = "맡을 태스크를 찾지 못했습니다"
            elif rag.get("state") == "error":
                rag_summary = "담당 에이전트가 답하지 못했습니다"
            elif rag.get("state") == "running":
                rag_summary = "문서를 찾는 중…"
            else:
                rag_summary = f"{row.get('task_name') or rag.get('task') or '태스크'} · 근거 {len(cites)}건 인용"
            details = []
            if rag.get("task"):
                details.append({"label": "담당", "meta": f"{row.get('task_name') or rag['task']} · {rag.get('agent') or ''}".strip(" ·"),
                                "tone": "ok"})
            if rag.get("routing") == "source":
                details.append({"label": "라우팅", "meta": "등록된 소스로 지정", "tone": "ok"})
            details += [{"label": c, "meta": "인용", "tone": "ok"} for c in cites]
            ms = sum(x for x in (rag.get("headMs"), rag.get("taskMs")) if x) or None
            rag_step = {"key": "rag", "title": "RAG 검색", "state": rag.get("state", "pending"), "ms": ms,
                        "summary": rag_summary, "details": details}
            v_state = verify.get("state", "pending")
            rules = verify.get("redactions") or []
            labels = [rule_label(r) for r in rules]
            if v_state == "running":
                v_summary = "공개 범위를 검사하는 중…"
            elif v_state == "skipped":
                v_summary = "검사할 지식 없음"
            elif verify.get("verdict") == "block":
                v_summary = f"{grade_label} 등급 · 답변 차단"
            elif v_state == "done":
                v_summary = f"{grade_label} 등급 · " + (f"{', '.join(labels)} 제외" if labels else "제외 항목 없음")
            else:
                v_summary = "대기"
            if injection and v_state == "done":
                v_summary += f" · 지시문 주입 의심 {len(injection)}건"
            v_details = [{"label": f"{rule_label(r)} 제외", "meta": f"{grade_label} 등급", "tone": "warn"} for r in rules]
            if verify.get("verdict") == "block":
                v_details.append({"label": "답변 차단", "meta": verify.get("blockedBy") or "검열", "tone": "block"})
            v_details += [{"label": f"\"{x['text'][:60]}\" 문장은 지시로 보고 따르지 않음", "meta": "주입 의심", "tone": "block"}
                          for x in injection]
            v_details += [{"label": str(b["action"]), "meta": f"{b['reason']} · 차단", "tone": "block"} for b in blocked]
            if not injection and v_state == "done":
                v_details.append({"label": "지시문 주입 의심 문장", "meta": "없음", "tone": "ok"})
            verify_step = {"key": "verify", "title": "검증", "state": v_state, "ms": verify.get("ms"),
                           "summary": v_summary, "details": v_details}
        # the draft is written by RFA_module's desk after the head answered
        filed = None
        if approval and approval.get("round", 1) >= round_:
            filed = _epoch(approval.get("created_at")) if round_ == 1 else next(
                (_epoch(e.get("at")) for e in approval.get("events") or []
                 if e.get("who") == "desk" and e.get("detail") == f"round {round_}"), None)
        finished = (row or {}).get("finished")
        if approval and approval.get("round", 1) >= round_ and phase != "regenerating":
            text = approval.get("draft") or ""
            d_state, d_summary = "done", f"{len(text)}자 초안 · 대응 에이전트 작성"
        elif finished:
            d_state, d_summary = "running", "초안을 쓰는 중…"
        else:
            d_state, d_summary = "pending", "대기"
        d_details = [{"label": "작성", "meta": "RFA_module 대응 에이전트", "tone": "ok"}]
        if round_ > 1:
            d_details.append({"label": "거절 이력 반영", "meta": f"{round_ - 1}건", "tone": "ok"})
        draft_step = {"key": "draft", "title": "LLM 초안", "state": d_state,
                      "ms": int((filed - finished) * 1000) if filed and finished and filed >= finished else None,
                      "summary": d_summary, "details": d_details}
        return [rag_step, verify_step, draft_step]

    async def detail(self, item_id: str, *, authenticated: bool) -> dict:
        url, approval, rows = await self._find(item_id, authenticated)
        item = self._item(url, approval, rows)
        first = rows[0] if rows else {}
        question = (approval or {}).get("question") or first.get("question") or ""
        context = (approval or {}).get("context") or self.store.loads(first.get("context"), []) or []
        phase = item["status"]
        round_ = (approval["round"] + 1) if approval and phase == "regenerating" else item["round"]
        row = next((r for r in rows if r["round"] == round_), None)
        injection = self.store.loads(first.get("injection"), []) if first else []
        blocked = self._blocked_attempts([r["request_id"] for r in rows])
        rejections = (approval or {}).get("rejections") or []
        draft_phase = {"pending": "ready", "drafting": "drafting"}.get(phase, phase)
        return {
            **item,
            "question": question,
            "source": self._source_view(item["channel"], url, item["target"] or "", question, context,
                                        item["requester"], item["arrivedAt"]),
            "injection": {"sentences": injection, "note": INJECTION_NOTE} if injection else None,
            "refusal": (row or first or {}).get("refusal") or (approval or {}).get("refusal"),
            "draft": {"text": (approval or {}).get("draft") or "", "chars": len((approval or {}).get("draft") or ""),
                      "round": item["round"], "phase": draft_phase,
                      "regenerations": [{"request": r.get("reason"), "at": _epoch(r.get("at")), "draft": r.get("draft")}
                                        for r in rejections],
                      "lastRequest": rejections[-1].get("reason") if rejections else None,
                      "postedUrl": (approval or {}).get("posted_url"),
                      "publishError": self._publish_error(approval["id"]) if approval else None,
                      "canRespond": phase in ("pending", "publish_failed") and authenticated,
                      "canRegenerate": phase == "pending" and authenticated,
                      "regenerationsLeft": max(0, MAX_ROUNDS - item["round"]) if approval else MAX_ROUNDS - 1,
                      "closesOnRegenerate": bool(approval) and item["round"] >= MAX_ROUNDS},
            "steps": self._steps(row, approval, round_, phase, item["grade"], injection if round_ == 1 else [],
                                 blocked if round_ == 1 else []),
            "blockedAttempts": blocked,
            "events": [{"at": _epoch(e.get("at")), "who": e.get("who"), "what": e.get("what"), "detail": e.get("detail")}
                       for e in (approval or {}).get("events") or []],
        }

    # ---- actions ------------------------------------------------------------------------------

    async def _approval_for(self, item_id: str, authenticated: bool) -> dict:
        if not authenticated:
            raise InboxError(403, "owner_only", "게스트는 결재할 수 없습니다. 소유자에게 요청하세요.")
        if self.backend is None:
            raise InboxError(503, "approvals_unavailable", "결재 서버가 설정되지 않았습니다.")
        _, approval, _ = await self._find(item_id, authenticated)
        if approval is None:
            raise InboxError(409, "not_filed", "아직 초안이 올라오지 않았습니다. 잠시 뒤 다시 시도해 주세요.")
        return approval

    async def respond(self, item_id: str, draft: str | None, *, authenticated: bool) -> dict:
        approval = await self._approval_for(item_id, authenticated)
        if draft is not None and draft.strip() != (approval.get("draft") or "").strip():
            raise InboxError(409, "draft_edit_unsupported", EDIT_UNSUPPORTED)  # D-22
        if approval.get("status") not in ("pending", "approved"):
            raise InboxError(409, "invalid_state", "지금 상태에서는 응답할 수 없습니다.")
        try:
            result = await self.backend.approve(approval["id"])
        except ApprovalsError as exc:
            if exc.status == 502:
                self._set_publish_error(approval["id"], exc.detail or exc.message)
            audit.record(kind="approval", verdict="error", action="respond",
                         detail={"approval": approval["id"], "status": exc.status, "code": exc.code})
            self._cache = (0.0, [])
            raise InboxError(exc.status, exc.code, exc.message) from exc
        self._set_publish_error(approval["id"], None)
        audit.record(kind="approval", verdict=result.get("status"), action="respond",
                     channel=approval.get("channel"), detail={"approval": approval["id"], "posted_url": result.get("posted_url")})
        self._cache = (0.0, [])
        return await self.detail(item_id, authenticated=authenticated)

    async def regenerate(self, item_id: str, request: str, *, authenticated: bool) -> dict:
        approval = await self._approval_for(item_id, authenticated)
        text = (request or "").strip()
        if not text:
            raise InboxError(422, "request_required", "초안을 어떻게 바꿀지 적어 주세요.")
        if approval.get("status") != "pending":
            raise InboxError(409, "invalid_state", "승인 대기 중인 초안만 재생성할 수 있습니다.")
        try:
            result = await self.backend.reject(approval["id"], text[:400])
        except ApprovalsError as exc:
            self._cache = (0.0, [])
            raise InboxError(exc.status, exc.code, exc.message) from exc
        audit.record(kind="approval", verdict=result.get("status"), action="regenerate",
                     channel=approval.get("channel"), detail={"approval": approval["id"], "round": approval.get("round")})
        self._cache = (0.0, [])
        return await self.detail(item_id, authenticated=authenticated)
