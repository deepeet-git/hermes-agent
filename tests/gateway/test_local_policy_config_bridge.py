"""Fork settings survive the upstream generic PlatformConfig extra bridge."""
import pytest
import json

from gateway.config import Platform, load_gateway_config


@pytest.mark.parametrize("container", ["root", "platforms", "gateway"])
def test_discord_local_policy_fields_reach_adapter_extra(tmp_path, monkeypatch, container):
    policy = {
        "principal_toolsets": {
            "owner_user_ids": ["owner", "co-owner"], "scope_ids": ["guild"],
            "channels": {"channel": {"owner": "inherit", "regular": ["web"]}},
        },
        "heimdall_incident_intake": {
            "guild_id": "100000000000000001", "channel_id": "100000000000000002",
            "webhook_id": "100000000000000004", "author_id": "100000000000000003",
            "toolsets": ["safe"],
        },
        "voice_channel_text_output": False,
        "voice_channel_prompt": "Speak without a text echo.",
    }
    data = {"discord": policy}
    if container == "platforms":
        data = {"platforms": data}
    elif container == "gateway":
        data = {"gateway": {"platforms": data}}
    (tmp_path / "config.yaml").write_text(json.dumps(data))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    extra = load_gateway_config().platforms[Platform.DISCORD].extra
    for name, value in policy.items():
        assert extra[name] == value
