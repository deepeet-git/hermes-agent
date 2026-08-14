from agent.lean_chat_router import (
    apply_lean_chat_request,
    should_use_lean_chat_fast_path,
)


def test_routes_latency_complaint_to_lean_chat():
    assert should_use_lean_chat_fast_path(
        "두줄짜리작업을 삼십분하는 원인이 개선이안되네"
    )


def test_routes_short_conversation_to_lean_chat():
    assert should_use_lean_chat_fast_path("그건 왜 그래?")
    assert should_use_lean_chat_fast_path("이 방식은 어때?")


def test_keeps_explicit_actions_on_operator_lane():
    for message in (
        "반영도",
        "진행해놔 반영도",
        "원인 확인해",
        "수정해서 배포해",
        "오늘 주문 조회해",
    ):
        assert not should_use_lean_chat_fast_path(message)


def test_keeps_live_state_and_ambiguous_input_on_operator_lane():
    for message in (
        "현재 상태 알려줘",
        "오늘 몇 건이야?",
        "README",
        "https://example.com 이거 봐",
        ["not", "plain", "text"],
    ):
        assert not should_use_lean_chat_fast_path(message)


def test_apply_lean_chat_request_removes_tools_and_lowers_reasoning():
    kwargs = {
        "model": "gpt-5.6-sol",
        "tools": [{"type": "function", "name": "terminal"}],
        "functions": [{"name": "legacy"}],
        "reasoning": {"effort": "high", "summary": "auto"},
        "reasoning_effort": "high",
        "extra_body": {"reasoning": {"effort": "medium"}, "other": True},
    }

    apply_lean_chat_request(kwargs, "low")

    assert "tools" not in kwargs
    assert "functions" not in kwargs
    assert kwargs["tool_choice"] == "none"
    assert kwargs["reasoning"] == {"effort": "low", "summary": "auto"}
    assert kwargs["reasoning_effort"] == "low"
    assert kwargs["extra_body"] == {
        "reasoning": {"effort": "low"},
        "other": True,
    }
