"""Trusted-principal toolset policy for messaging gateway turns.

This module is intentionally pure: callers provide trusted source fields and a
toolset validator, and receive a deterministic decision.  It does not inspect
message text, display names, or model-provided arguments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence, cast


@dataclass(frozen=True)
class PrincipalToolsetDecision:
    """Effective toolsets and a stable audit reason.

    ``toolsets is None`` means the feature is not configured and the caller
    should preserve legacy platform behavior.  An empty tuple is an explicit
    fail-closed denial and must never be converted to ``None``.
    """

    toolsets: Optional[tuple[str, ...]]
    reason: str


def apply_principal_toolset_policy(
    *,
    feature_enabled: bool,
    platform_toolsets: Sequence[str],
    policy: object,
    platform: str,
    user_id: Optional[str],
    scope_id: Optional[str],
    chat_id: Optional[str],
    validated_parent_chat_id: Optional[str],
    is_dm: bool,
    is_valid_toolset: Callable[[str], bool],
    is_bot: bool = False,
) -> PrincipalToolsetDecision:
    """Apply the opt-in policy while preserving an explicit rollback switch."""

    if not feature_enabled or platform != "discord":
        return PrincipalToolsetDecision(None, "feature_disabled")
    # Once the feature is enabled, a missing policy is a configuration error,
    # not permission to fall back to every platform tool.
    effective_policy: object = {} if policy is None else policy
    return resolve_principal_toolsets(
        platform_toolsets=platform_toolsets,
        policy=effective_policy,
        platform=platform,
        user_id=user_id,
        scope_id=scope_id,
        chat_id=chat_id,
        validated_parent_chat_id=validated_parent_chat_id,
        is_dm=is_dm,
        is_valid_toolset=is_valid_toolset,
        is_bot=is_bot,
    )


def resolve_principal_toolsets(
    *,
    platform_toolsets: Sequence[str],
    policy: object,
    platform: str,
    user_id: Optional[str],
    scope_id: Optional[str],
    chat_id: Optional[str],
    validated_parent_chat_id: Optional[str],
    is_dm: bool,
    is_valid_toolset: Callable[[str], bool],
    is_bot: bool = False,
) -> PrincipalToolsetDecision:
    """Resolve a principal-scoped toolset decision from trusted source fields."""

    if policy is None:
        return PrincipalToolsetDecision(None, "not_configured")
    if platform != "discord" or not isinstance(policy, dict):
        return PrincipalToolsetDecision((), "invalid_policy")
    typed_policy = cast(dict[str, Any], policy)

    owners = typed_policy.get("owner_user_ids")
    scopes = typed_policy.get("scope_ids")
    channels = typed_policy.get("channels")
    if not isinstance(owners, list) or not isinstance(scopes, list) or not isinstance(channels, dict):
        return PrincipalToolsetDecision((), "invalid_policy")
    if not all(isinstance(value, str) and value for value in owners + scopes):
        return PrincipalToolsetDecision((), "invalid_policy")
    if is_bot or not user_id:
        return PrincipalToolsetDecision((), "denied_context")

    role = "owner" if user_id in owners else "regular"
    if is_dm:
        dm_rule = typed_policy.get("dm")
        if not isinstance(dm_rule, dict):
            return PrincipalToolsetDecision((), "invalid_policy")
        configured = dm_rule.get(role)
        if role == "owner" and configured == "inherit":
            return PrincipalToolsetDecision(tuple(platform_toolsets), "owner_inherit")
        if isinstance(configured, list) and all(
            isinstance(name, str) and name and is_valid_toolset(name) for name in configured
        ):
            return PrincipalToolsetDecision(tuple(dict.fromkeys(configured)), f"{role}_explicit")
        return PrincipalToolsetDecision((), "denied_principal")

    if not scope_id or scope_id not in scopes:
        return PrincipalToolsetDecision((), "denied_context")

    effective_channel = validated_parent_chat_id or chat_id
    channel_rule = channels.get(effective_channel)
    if not isinstance(channel_rule, dict):
        return PrincipalToolsetDecision((), "denied_channel")
    allowed_user_ids = channel_rule.get("allowed_user_ids")
    if allowed_user_ids is not None:
        if (
            not isinstance(allowed_user_ids, list)
            or not allowed_user_ids
            or not all(isinstance(value, str) and value for value in allowed_user_ids)
        ):
            return PrincipalToolsetDecision((), "invalid_policy")
        if user_id not in allowed_user_ids:
            return PrincipalToolsetDecision((), "denied_principal")
    if user_id in owners and channel_rule.get("owner") == "inherit":
        return PrincipalToolsetDecision(tuple(platform_toolsets), "owner_inherit")

    role = "owner" if user_id in owners else "regular"
    explicit = channel_rule.get(role)
    if isinstance(explicit, list) and all(
        isinstance(name, str) and name and is_valid_toolset(name) for name in explicit
    ):
        return PrincipalToolsetDecision(tuple(dict.fromkeys(explicit)), f"{role}_explicit")
    return PrincipalToolsetDecision((), "denied_principal")
