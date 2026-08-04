from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.lean_chat import (
    build_lean_chat_messages,
    is_lean_chat_request,
    prepare_lean_chat_request,
)


@pytest.mark.parametrize(
    "message",
    [
        "왜 이렇게 느린 거야? 더 개선할 방법은?",
        "Explain why prompt caching matters.",
        "이 구조의 장단점을 설명해줘",
        "What do you think about deterministic routing?",
        "HTTP와 HTTPS의 차이가 뭐야?",
        "그 방식의 가장 큰 리스크는 뭐야?",
        "What is eventual consistency?",
    ],
)
def test_stable_explanatory_requests_use_lean_chat(message: str) -> None:
    assert is_lean_chat_request(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "최신화",
        "지금 응답 속도를 확인해줘",
        "이 파일을 수정하고 테스트해줘",
        "오늘 날씨가 어때?",
        "2 + 2 계산해줘",
        "https://example.com 내용을 요약해줘",
        "/new",
        "개선해 완벽하게",
        "최근 로그를 조사해줘",
        "내일 오전 9시에 알려줘",
        "우리가 전에 결정한 내용이 뭐야?",
        "내 이름이 뭐야?",
        "What is my preferred deployment style?",
        "Why did you delete that message?",
        "왜 삭제했어?",
        "What is Bitcoin's price?",
        "Who is the president of South Korea?",
        "한국 대통령이 누구야?",
        "What is 10 divided by 2?",
        "What is 15 percent of 80?",
        "10 나누기 2는 뭐야?",
        "What is my API key?",
        "What do you know about me?",
        "이 문제 해결해줘",
        "이 문제 해결 방법을 설명해줘",
        "문제 해결 방법은 뭐야?",
        "What does this code do?",
        "Explain this error",
        "What does that output mean?",
        "이 코드가 뭐야?",
        "이 에러를 설명해줘",
    ],
)
def test_live_or_action_requests_stay_on_operator_lane(message: str) -> None:
    assert is_lean_chat_request(message) is False


def test_multimodal_requests_stay_on_operator_lane() -> None:
    message = [
        {"type": "text", "text": "이 이미지 설명해줘"},
        {"type": "image_url", "image_url": {"url": "x"}},
    ]
    assert is_lean_chat_request(message) is False


def test_lean_messages_drop_tools_system_and_old_history() -> None:
    messages = [
        {"role": "system", "content": "FULL OPERATOR PROMPT"},
        {"role": "user", "content": "파일을 확인해줘"},
        {
            "role": "assistant",
            "content": "확인하겠습니다.",
            "tool_calls": [
                {"id": "1", "function": {"name": "terminal", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "1", "content": "secret-ish raw output"},
        {"role": "assistant", "content": "확인 결과는 정상입니다."},
        {"role": "user", "content": "My API key is PRIVATE-123"},
        {"role": "assistant", "content": "I will remember PRIVATE-123"},
        {"role": "user", "content": "왜 프롬프트 캐싱이 중요한가?"},
        {"role": "assistant", "content": "반복 입력 계산을 줄이기 때문입니다."},
        {"role": "user", "content": "그 방식의 단점은 뭐야?"},
    ]

    result = build_lean_chat_messages(
        messages, system_prompt="LEAN", history_messages=10
    )

    assert result[0] == {"role": "system", "content": "LEAN"}
    assert result[-1] == {"role": "user", "content": "그 방식의 단점은 뭐야?"}
    assert all(message["role"] in {"system", "user", "assistant"} for message in result)
    serialized = repr(result)
    assert "FULL OPERATOR PROMPT" not in serialized
    assert "secret-ish raw output" not in serialized
    assert "확인하겠습니다" not in serialized
    assert "확인 결과" not in serialized
    assert "PRIVATE-123" not in serialized
    assert "반복 입력 계산" in serialized
    assert len(result) == 4


def test_prepare_lean_chat_request_is_config_gated_and_toolless() -> None:
    agent = SimpleNamespace(
        _intent_aware_routing=True,
        _lean_chat_fast_path=True,
        platform="telegram",
        _memory_store=SimpleNamespace(
            format_for_system_prompt=lambda target: "SENSITIVE OWNER PROFILE"
        ),
        _user_profile_enabled=True,
        _lean_chat_reasoning_effort="low",
    )
    messages = [{"role": "user", "content": "왜 프롬프트 캐싱이 응답 속도에 중요한가?"}]

    prepared = prepare_lean_chat_request(
        agent, "왜 프롬프트 캐싱이 응답 속도에 중요한가?", messages
    )

    assert prepared is not None
    assert prepared.tools == []
    assert prepared.reasoning_config == {"enabled": True, "effort": "low"}
    assert prepared.messages[0]["role"] == "system"
    assert "tool" not in prepared.messages[0]["content"].lower()
    assert "SENSITIVE OWNER PROFILE" not in prepared.messages[0]["content"]
    assert prepared.messages[-1] == messages[-1]

    agent._lean_chat_reasoning_effort = "inherit"
    inherited = prepare_lean_chat_request(
        agent, "왜 프롬프트 캐싱이 응답 속도에 중요한가?", messages
    )
    assert inherited is not None
    assert inherited.reasoning_config is None

    agent._lean_chat_fast_path = False
    assert prepare_lean_chat_request(agent, "왜 느린 거야?", messages) is None


def test_current_performance_question_requires_prior_conversation_context() -> None:
    agent = SimpleNamespace(
        _intent_aware_routing=True,
        _lean_chat_fast_path=True,
        platform="telegram",
        _lean_chat_reasoning_effort="low",
    )
    current = {"role": "user", "content": "왜 이렇게 느린 거야? 더 개선할 방법은?"}
    assert prepare_lean_chat_request(agent, current["content"], [current]) is None

    unrelated = [
        {"role": "user", "content": "현재 시간을 확인해줘"},
        {"role": "assistant", "content": "현재 시각은 오후 6시입니다."},
        current,
    ]
    assert prepare_lean_chat_request(agent, current["content"], unrelated) is None

    operator_context = [
        {"role": "user", "content": "응답 속도를 측정해줘"},
        {"role": "assistant", "content": "단순 답변은 약 40초입니다."},
        current,
    ]
    assert (
        prepare_lean_chat_request(agent, current["content"], operator_context) is None
    )

    safe_context = [
        {"role": "user", "content": "왜 캐싱이 응답 속도에 중요한가?"},
        {"role": "assistant", "content": "캐시가 없으면 단순 답변도 약 40초입니다."},
        current,
    ]
    assert (
        prepare_lean_chat_request(agent, current["content"], safe_context) is not None
    )


def test_operator_context_keeps_elliptical_followup_off_lean_lane() -> None:
    agent = SimpleNamespace(
        _intent_aware_routing=True,
        _lean_chat_fast_path=True,
        platform="telegram",
        _lean_chat_reasoning_effort="low",
    )
    messages = [
        {"role": "user", "content": "현재 로그를 확인해줘"},
        {"role": "assistant", "content": "실패 원인을 찾았습니다."},
        {"role": "user", "content": "그건 왜?"},
    ]
    assert prepare_lean_chat_request(agent, "그건 왜?", messages) is None

    messages[-1]["content"] = "그 내용을 설명해줘"
    assert prepare_lean_chat_request(agent, "그 내용을 설명해줘", messages) is None


def test_conceptual_context_allows_elliptical_lean_followup() -> None:
    agent = SimpleNamespace(
        _intent_aware_routing=True,
        _lean_chat_fast_path=True,
        platform="telegram",
        _lean_chat_reasoning_effort="low",
    )
    messages = [
        {"role": "user", "content": "왜 캐싱이 중요해?"},
        {"role": "assistant", "content": "반복 처리를 줄이기 때문입니다."},
        {"role": "user", "content": "그건 왜?"},
    ]
    assert prepare_lean_chat_request(agent, "그건 왜?", messages) is not None


def test_prepare_lean_chat_request_fails_closed_for_action() -> None:
    agent = SimpleNamespace(
        _intent_aware_routing=True,
        _lean_chat_fast_path=True,
        platform="telegram",
        _memory_store=None,
        _user_profile_enabled=False,
        _lean_chat_reasoning_effort="low",
    )
    assert (
        prepare_lean_chat_request(
            agent,
            "최신 로그를 확인해줘",
            [{"role": "user", "content": "최신 로그를 확인해줘"}],
        )
        is None
    )
