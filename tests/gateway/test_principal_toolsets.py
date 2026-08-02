from copy import deepcopy

import pytest

from gateway.principal_toolsets import (
    apply_principal_toolset_policy,
    resolve_principal_toolsets,
)


def _valid_toolset(name: str) -> bool:
    return name in {"web", "clarify", "deepeet-pdp", "terminal", "file", "image_gen", "vision"}


def _policy() -> dict:
    return {
        "owner_user_ids": ["owner"],
        "scope_ids": ["guild"],
        "dm": {"owner": "inherit", "regular": []},
        "channels": {
            "general": {
                "owner": "inherit",
                "regular": ["web", "clarify"],
            },
            "pdp": {
                "owner": ["terminal", "file", "image_gen", "vision", "deepeet-pdp"],
                "regular": ["deepeet-pdp"],
                "allowed_user_ids": ["owner", "regular", "regular-user"],
            },
        },
    }


def test_missing_policy_preserves_legacy_platform_toolsets() -> None:
    decision = resolve_principal_toolsets(
        platform_toolsets=["web", "terminal"],
        policy=None,
        platform="discord",
        user_id="owner",
        scope_id="guild",
        chat_id="channel",
        validated_parent_chat_id=None,
        is_dm=False,
        is_valid_toolset=_valid_toolset,
    )

    assert decision.toolsets is None
    assert decision.reason == "not_configured"


def test_owner_in_general_channel_inherits_platform_toolsets() -> None:
    decision = resolve_principal_toolsets(
        platform_toolsets=["web", "terminal", "deepeet-pdp"],
        policy=_policy(),
        platform="discord",
        user_id="owner",
        scope_id="guild",
        chat_id="general",
        validated_parent_chat_id=None,
        is_dm=False,
        is_valid_toolset=_valid_toolset,
    )

    assert decision.toolsets == ("web", "terminal", "deepeet-pdp")
    assert decision.reason == "owner_inherit"


def test_regular_user_in_general_channel_gets_explicit_safe_toolsets() -> None:
    decision = resolve_principal_toolsets(
        platform_toolsets=["web", "terminal", "vision"],
        policy=_policy(),
        platform="discord",
        user_id="regular-user",
        scope_id="guild",
        chat_id="general",
        validated_parent_chat_id=None,
        is_dm=False,
        is_valid_toolset=_valid_toolset,
    )

    assert decision.toolsets == ("web", "clarify")
    assert decision.reason == "regular_explicit"


def test_owner_dm_inherits_platform_toolsets() -> None:
    decision = resolve_principal_toolsets(
        platform_toolsets=["web", "terminal"],
        policy=_policy(),
        platform="discord",
        user_id="owner",
        scope_id=None,
        chat_id="owner-dm",
        validated_parent_chat_id=None,
        is_dm=True,
        is_valid_toolset=_valid_toolset,
    )

    assert decision.toolsets == ("web", "terminal")
    assert decision.reason == "owner_inherit"


def test_regular_user_in_pdp_thread_gets_only_pdp_capability() -> None:
    decision = resolve_principal_toolsets(
        platform_toolsets=["web", "terminal", "file", "deepeet-pdp"],
        policy=_policy(),
        platform="discord",
        user_id="regular-user",
        scope_id="guild",
        chat_id="thread-id",
        validated_parent_chat_id="pdp",
        is_dm=False,
        is_valid_toolset=_valid_toolset,
    )

    assert decision.toolsets == ("deepeet-pdp",)
    assert decision.reason == "regular_explicit"


def test_pdp_channel_denies_user_outside_channel_capability_allowlist() -> None:
    decision = resolve_principal_toolsets(
        platform_toolsets=["web", "terminal", "deepeet-pdp"],
        policy=_policy(),
        platform="discord",
        user_id="outsider",
        scope_id="guild",
        chat_id="pdp",
        validated_parent_chat_id=None,
        is_dm=False,
        is_valid_toolset=_valid_toolset,
    )

    assert decision.toolsets == ()
    assert decision.reason == "denied_principal"


def test_owner_in_pdp_channel_gets_only_configured_pdp_workflow_tools() -> None:
    decision = resolve_principal_toolsets(
        platform_toolsets=["web", "terminal", "memory", "deepeet-pdp"],
        policy=_policy(),
        platform="discord",
        user_id="owner",
        scope_id="guild",
        chat_id="pdp",
        validated_parent_chat_id=None,
        is_dm=False,
        is_valid_toolset=_valid_toolset,
    )

    assert decision.toolsets == ("terminal", "file", "image_gen", "vision", "deepeet-pdp")
    assert decision.reason == "owner_explicit"


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({"user_id": None}, "denied_context"),
        ({"scope_id": "other-guild"}, "denied_context"),
        ({"chat_id": "unknown"}, "denied_channel"),
        ({"is_dm": True, "scope_id": None, "chat_id": "regular-dm"}, "regular_explicit"),
    ],
)
def test_untrusted_or_unknown_context_fails_closed(overrides: dict, expected_reason: str) -> None:
    args = {
        "platform_toolsets": ["web", "terminal"],
        "policy": _policy(),
        "platform": "discord",
        "user_id": "regular-user",
        "scope_id": "guild",
        "chat_id": "general",
        "validated_parent_chat_id": None,
        "is_dm": False,
        "is_valid_toolset": _valid_toolset,
    }
    args.update(overrides)

    decision = resolve_principal_toolsets(**args)

    assert decision.toolsets == ()
    assert decision.reason == expected_reason


def test_unknown_explicit_toolset_fails_closed_instead_of_partially_allowing() -> None:
    policy = deepcopy(_policy())
    policy["channels"]["general"]["regular"] = ["web", "not-a-toolset"]

    decision = resolve_principal_toolsets(
        platform_toolsets=["web", "terminal"],
        policy=policy,
        platform="discord",
        user_id="regular-user",
        scope_id="guild",
        chat_id="general",
        validated_parent_chat_id=None,
        is_dm=False,
        is_valid_toolset=_valid_toolset,
    )

    assert decision.toolsets == ()
    assert decision.reason == "denied_principal"


def test_bot_or_webhook_principal_fails_closed() -> None:
    decision = resolve_principal_toolsets(
        platform_toolsets=["web", "terminal"],
        policy=_policy(),
        platform="discord",
        user_id="owner",
        scope_id="guild",
        chat_id="general",
        validated_parent_chat_id=None,
        is_dm=False,
        is_valid_toolset=_valid_toolset,
        is_bot=True,
    )

    assert decision.toolsets == ()
    assert decision.reason == "denied_context"


@pytest.mark.parametrize(
    "policy",
    [
        {},
        {"owner_user_ids": "owner", "scope_ids": ["guild"], "channels": {}},
        {"owner_user_ids": ["owner"], "scope_ids": ["guild"], "channels": []},
    ],
)
def test_malformed_policy_fails_closed(policy: object) -> None:
    decision = resolve_principal_toolsets(
        platform_toolsets=["terminal"],
        policy=policy,
        platform="discord",
        user_id="owner",
        scope_id="guild",
        chat_id="general",
        validated_parent_chat_id=None,
        is_dm=False,
        is_valid_toolset=_valid_toolset,
    )

    assert decision.toolsets == ()
    assert decision.reason == "invalid_policy"


def test_feature_flag_off_preserves_legacy_behavior_even_with_policy() -> None:
    decision = apply_principal_toolset_policy(
        feature_enabled=False,
        platform_toolsets=["web", "terminal"],
        policy=_policy(),
        platform="discord",
        user_id="regular-user",
        scope_id="guild",
        chat_id="general",
        validated_parent_chat_id=None,
        is_dm=False,
        is_valid_toolset=_valid_toolset,
    )

    assert decision.toolsets is None
    assert decision.reason == "feature_disabled"


def test_feature_flag_on_with_missing_policy_fails_closed() -> None:
    decision = apply_principal_toolset_policy(
        feature_enabled=True,
        platform_toolsets=["web", "terminal"],
        policy=None,
        platform="discord",
        user_id="owner",
        scope_id="guild",
        chat_id="general",
        validated_parent_chat_id=None,
        is_dm=False,
        is_valid_toolset=_valid_toolset,
    )

    assert decision.toolsets == ()
    assert decision.reason == "invalid_policy"
