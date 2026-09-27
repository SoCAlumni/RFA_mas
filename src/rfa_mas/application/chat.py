"""Small replaceable chat contract; routing is not an authorization decision."""

import re
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from rfa_mas.contracts import DomainId


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=10000)
    message_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,160}$")
    domain_id: DomainId = DomainId.TRIV3


class ChatPort(Protocol):
    async def sessions(self) -> list[dict]: ...

    async def history(self, session_id: str) -> list[dict]: ...

    async def send(self, session_id: str, body: ChatMessage) -> dict: ...


def chat_intent(text: str) -> str:
    """Conservative local PoC rules, not a learned intent model.

    Explicit note envelopes are data, including questions/commands inside them.
    Uncertain requests ask for clarification rather than starting tools or jobs.
    """
    text = text.strip().lower()
    if (
        re.match(r"^(메모|노트|기록|note)\s*[:：\n]", text)
        or re.match(r"^(저장해|기억해|메모해|기록해)", text)
        or re.search(r"(저장해\s*줘|저장해|기억해\s*줘|기억해|메모해\s*줘)[.!。\s]*$", text)
    ):
        return "store_note"
    if any(t in text for t in ("삭제해", "지워줘", "배포해", "송금", "delete all")):
        return "clarify"
    if any(t in text for t in ("공개 초안", "공개 답변", "외부 공유", "고객 답변")):
        return "external_draft"
    if any(
        t in text
        for t in (
            "?",
            "？",
            "찾아",
            "검색",
            "알려",
            "무엇",
            "어떻",
            "언제",
            "어디",
            "얼마",
            "누가",
            "뭐",
            "요약해",
            "정리해줘",
        )
    ):
        return "query"
    if re.search(
        r"(입니다|이다|했다|했어|했어요|하기로|예정|완료|임|함|있다|있어|이에요|예요|해요|이야|합니다)[.!。\s]*$",
        text,
    ):
        return "store_note"
    if "\n" in text or re.search(r"\S+\s*[:：]\s*\S+", text):
        return "store_note"
    return "clarify"
