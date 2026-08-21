"""Conservative runtime routing for short conversational turns.

The router deliberately recognizes only clear conversation/feedback. Everything
else keeps the normal operator path, so actions and live-state checks retain
full tools and verification.
"""

from __future__ import annotations

import re
from typing import Any

_MAX_LEAN_CHARS = 240

_EXPLICIT_OPERATOR_RE = re.compile(
    r"(?:"
    r"해줘|해주세요|해라|해봐|해놔|해두|하자|진행해|반영해|적용해|수정해|"
    r"구현해|실행해|확인해|조회해|검색해|찾아|조사해|검증해|테스트해|"
    r"배포해|커밋해|푸시해|병합해|만들어|생성해|추가해|삭제해|제거해|"
    r"변경해|바꿔|보내|예약해|저장해|기록해|열어|읽어|분석해|계산해|"
    r"비교해|최신화|동기화|sync\b|run\b|check\b|inspect\b|deploy\b|"
    r"update\b|fix\b|create\b|edit\b|send\b|schedule\b"
    r")",
    re.IGNORECASE,
)

# Terse Korean operator follow-ups commonly omit the final verb: "반영도",
# "수정도", "배포까지". Keep these out of the no-tool lane.
_TERSE_OPERATOR_RE = re.compile(
    r"(?:반영|적용|수정|구현|실행|진행|확인|조회|검색|조사|검증|테스트|"
    r"배포|커밋|푸시|병합|생성|추가|삭제|변경|저장|기록|계산|동기화)"
)

_LIVE_STATE_RE = re.compile(
    r"(?:오늘|현재|지금\s*(?:상태|뭐|어떻게|돌아)|최신\s*(?:상태|버전)|"
    r"몇\s*(?:개|건|명|원)|얼마|어디에|켜져|꺼져|열려|실행\s*중|"
    r"running|status|latest|today|current)",
    re.IGNORECASE,
)

_MUTABLE_SOURCE_RE = re.compile(
    r"(?=.*(?:규칙|계약|저장소|리포지토리|repo(?:sitory)?|코드|파일|폴더|경로|설정))"
    r"(?=.*(?:변경됐|바뀌었|달라졌|최신|기존|현재))",
    re.IGNORECASE,
)

_TOOL_CAPABILITY_RE = re.compile(
    r"(?=.*(?:도구|툴|저장소|리포지토리|repo(?:sitory)?|DB|데이터베이스|로그|서버|API|파일|코드))"
    r"(?=.*(?:가능|접근|연결|조회|확인|검색|읽|수정|변경|쓸\s*수|사용))",
    re.IGNORECASE,
)

_CONVERSATIONAL_RE = re.compile(
    r"(?:"
    r"안\s*되네|안되네|느리네|여전하|그대로네|맞네|맞아|아니네|아니야|"
    r"그렇네|그런가|왜\s*(?:그래|이래|느려)|어때|어떻게\s*생각|"
    r"무슨\s*뜻|뭐야|인가요?\??$|일까\??$|고마워|알겠어|좋아|싫어|"
    r"not improved|still slow|too slow|what do you think|thanks"
    r")",
    re.IGNORECASE,
)


def _plain_text(message: Any) -> str | None:
    if not isinstance(message, str):
        return None
    text = message.strip()
    if not text or len(text) > _MAX_LEAN_CHARS or text.count("\n") > 3:
        return None
    if "```" in text or re.search(r"https?://|(?:^|\s)[~/].+[/\\]", text):
        return None
    return text


def should_use_lean_chat_fast_path(message: Any) -> bool:
    """Return True only for clear short conversation/feedback.

    The ordering is intentional: an explicit action always wins over complaint
    wording, while a complaint wins over incidental nouns such as "개선" or
    "작업". Ambiguous input fails closed to the normal operator lane.
    """

    text = _plain_text(message)
    if text is None:
        return False
    if _EXPLICIT_OPERATOR_RE.search(text):
        return False
    if len(text) <= 18 and _TERSE_OPERATOR_RE.search(text):
        return False
    if _CONVERSATIONAL_RE.search(text):
        return True
    if _LIVE_STATE_RE.search(text):
        return False
    if _MUTABLE_SOURCE_RE.search(text) or _TOOL_CAPABILITY_RE.search(text):
        return False
    return bool(text.endswith("?") and len(text) <= 120)


def apply_lean_chat_request(api_kwargs: dict[str, Any], effort: str = "low") -> None:
    """Remove tool schemas and lower supported reasoning knobs in-place."""

    api_kwargs.pop("tools", None)
    api_kwargs.pop("functions", None)
    api_kwargs["tool_choice"] = "none"

    reasoning = api_kwargs.get("reasoning")
    if isinstance(reasoning, dict):
        reasoning = dict(reasoning)
        reasoning["effort"] = effort
        api_kwargs["reasoning"] = reasoning

    if "reasoning_effort" in api_kwargs:
        api_kwargs["reasoning_effort"] = effort

    extra_body = api_kwargs.get("extra_body")
    if isinstance(extra_body, dict):
        extra_body = dict(extra_body)
        extra_reasoning = extra_body.get("reasoning")
        if isinstance(extra_reasoning, dict):
            extra_reasoning = dict(extra_reasoning)
            extra_reasoning["effort"] = effort
            extra_body["reasoning"] = extra_reasoning
        api_kwargs["extra_body"] = extra_body
