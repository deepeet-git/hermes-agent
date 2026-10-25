"""Principal policy remains a ceiling across upstream intake and profile refactors."""
from types import SimpleNamespace

import pytest

from gateway.authz_mixin import GatewayAuthorizationMixin
from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource


def policy():
    return {
        "scope_ids": ["guild"],
        "owner_user_ids": ["owner-a", "owner-b"],
        "channels": {"internal": {"owner": "inherit", "regular": ["terminal"]}},
        "dm": {"owner": "inherit", "regular": []},
    }


class BoundaryRunner(GatewayAuthorizationMixin):
    def __init__(self, configured_policy):
        self.config = SimpleNamespace(
            multiplex_profiles=False,
            platforms={Platform.DISCORD: PlatformConfig(
                enabled=True, extra={"principal_toolsets": configured_policy},
            )},
        )
        self.adapters = {}
        self._primary_profile_name = "default"
        self._profile_configs = {}

    def _chat_scoped_grant(self, source, adapter_profile, is_group: bool, allow_adapter_delegation: bool) -> bool:
        # A legacy bot/relay/chat grant must not override an explicit channel policy.
        return True


def source(user="owner-a", chat="internal", **kwargs):
    return SessionSource(
        platform=Platform.DISCORD, chat_id=chat, chat_type="channel",
        user_id=user, scope_id="guild", **kwargs,
    )


@pytest.mark.parametrize("owner", ["owner-a", "owner-b"])
def test_both_owners_inherit_internal_read_tools_and_private_context(owner):
    from gateway.run import _principal_context_is_restricted

    configured = policy()
    runner = BoundaryRunner(configured)
    current = source(owner)
    tools = ["terminal", "file", "memory"]
    assert runner._principal_effective_toolsets(current, tools) == tools
    assert runner._principal_channel_authorizes(current)
    assert not _principal_context_is_restricted(
        source=current, platform_toolsets=tools, enabled_toolsets=tools,
        user_config={"discord": {"principal_toolsets": configured}},
    )


@pytest.mark.parametrize("configured", [None, {}, policy()])
def test_legacy_chat_grant_cannot_bypass_configured_channel_policy(configured):
    runner = BoundaryRunner(configured)
    assert not runner._principal_authorized(source(chat="unlisted"), allow_adapter_delegation=True)


def test_scoped_principal_does_not_gain_private_context_by_listing_all_tools():
    from gateway.run import _principal_context_is_restricted

    configured = policy()
    tools = ["terminal"]
    assert BoundaryRunner(configured)._principal_effective_toolsets(source("regular"), tools) == tools
    assert _principal_context_is_restricted(
        source=source("regular"), platform_toolsets=tools, enabled_toolsets=tools,
        user_config={"discord": {"principal_toolsets": configured}},
    )


def test_secondary_without_owning_adapter_never_borrows_primary_policy():
    runner = BoundaryRunner(policy())
    runner.config.multiplex_profiles = True
    current = source(profile="secondary")
    adapter, configured, selected = runner._principal_policy_context(current)
    assert adapter is None
    assert configured is True
    assert selected is None
    assert runner._principal_effective_toolsets(current, ["terminal"]) == []


def test_serialized_thread_parent_does_not_authorize_inheritance():
    runner = BoundaryRunner(policy())
    current = source()
    current.chat_type = "thread"
    current.chat_id = "thread"
    current.thread_id = "thread"
    current.parent_chat_id = "internal"
    assert not runner._principal_channel_authorizes(current)
    assert runner._principal_effective_toolsets(current, ["terminal"]) == []
