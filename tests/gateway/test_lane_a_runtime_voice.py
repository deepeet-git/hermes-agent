"""Audio-only VC delivery suppresses text without losing durable conversation history."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import weakref

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore


class VoiceAdapter:
    platform = Platform.DISCORD

    def __init__(self, text_output=False):
        self.config = PlatformConfig(enabled=True, extra={"voice_channel_text_output": text_output,
                                                         "voice_channel_prompt": "Speak naturally."})
        self._voice_text_channels = {111: 123}
        self._voice_sources = {}
        self._voice_input_callback = None
        self._on_voice_disconnect = None
        self._voice_mode_getter = None
        self._client = MagicMock()
        self._client.get_channel.return_value = SimpleNamespace(send=AsyncMock())
        self._client.get_guild.return_value = None
        self._client.get_user.return_value = None
        self.handle_message = AsyncMock()
        self.send = AsyncMock()
        self.get_user_voice_channel = AsyncMock(return_value=SimpleNamespace(name="VC"))
        self.join_voice_channel = AsyncMock(return_value=True)
        self.leave_voice_channel = AsyncMock()
        self._streaming_tts_turn_completed = lambda *a: False

    def is_in_voice_channel(self, guild_id):
        return guild_id == 111

    def _should_auto_tts_for_chat(self, chat_id):
        return True

    def _thread_id_and_chat_for_channel(self, channel):
        return None, "123"

    def build_source(self, **kwargs):
        return SessionSource(platform=self.platform, **kwargs)

    def _resolve_channel_prompt(self, *args):
        return "Channel prompt"


def setup_voice(tmp_path, text_output=False):
    runner = object.__new__(GatewayRunner)
    adapter = VoiceAdapter(text_output)
    runner.config = GatewayConfig(platforms={Platform.DISCORD: adapter.config})
    runner.adapters = {Platform.DISCORD: adapter}
    runner._voice_mode = {"discord:123": "all"}
    runner._VOICE_MODE_PATH = tmp_path / "voice.json"
    runner._session_db = None
    runner._is_user_authorized = lambda source: True
    source = SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type="group", user_id="42")
    source._transport_adapter_ref = weakref.ref(adapter)
    event = MessageEvent(source=source, text="Hello", message_type=MessageType.VOICE,
                         raw_message=SimpleNamespace(guild_id=111, guild=None))
    return runner, adapter, event


@pytest.mark.asyncio
async def test_audio_only_join_leave_and_echo_are_silent(tmp_path):
    runner, adapter, event = setup_voice(tmp_path)
    assert await runner._handle_voice_channel_join(event) == ""
    await runner._handle_voice_channel_input(111, 42, "Hello from audio")
    adapter._client.get_channel.return_value.send.assert_not_awaited()
    received = adapter.handle_message.call_args.args[0]
    assert received.channel_prompt == "Channel prompt\n\nSpeak naturally."
    assert received.source._transport_adapter_ref() is adapter
    assert await runner._handle_voice_channel_leave(event) == ""
    adapter.leave_voice_channel.assert_awaited_once_with(111)


@pytest.mark.asyncio
@pytest.mark.parametrize("text_output,echo", [(False, True), (True, False), (True, True)])
async def test_transcript_echo_requires_both_gates(tmp_path, text_output, echo):
    runner, adapter, _event = setup_voice(tmp_path, text_output)
    runner.config.stt_echo_transcripts = echo
    await runner._handle_voice_channel_input(111, 42, "Hello from audio")
    assert adapter._client.get_channel.return_value.send.await_count == int(text_output and echo)
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming_done,tool_tts", [(False, False), (True, False), (False, True)])
async def test_final_tts_exactly_once_text_silent_and_history_persisted(tmp_path, monkeypatch, streaming_done, tool_tts):
    runner, adapter, event = setup_voice(tmp_path)
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "test-model")
    runner.session_store = SessionStore(tmp_path / "sessions", runner.config)
    entry = runner.session_store.get_or_create_session(event.source)
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._send_voice_reply = AsyncMock()
    adapter._streaming_tts_turn_completed = lambda *args: streaming_done
    messages = [{"role": "user", "content": "Hello"}]
    if tool_tts:
        messages.append({"role": "assistant", "tool_calls": [{"function": {"name": "text_to_speech"}}]})
    messages.append({"role": "assistant", "content": "Spoken answer"})
    result = {"messages": messages, "final_response": "Spoken answer", "agent_persisted": False}
    prepared = SimpleNamespace(history=[], persist_user_message=None, message_text="Hello",
                               persist_user_timestamp=None, persist_user_display_kind=None, persistence_owner=None)
    await runner._hmwa_persist_turn_transcript(
        event=event, source=event.source, session_entry=entry, session_key=entry.session_key,
        agent_result=result, agent_messages=messages, prepared=prepared, response="Spoken answer",
        agent_failed_early=False, hidden_reasoning_incomplete=False, is_context_overflow_failure=False)
    delivered = await runner._hmwa_deliver_turn_response(
        event, event.source, entry, entry.session_key, None, result, messages, "Spoken answer", "footer", False)
    assert delivered is None
    assert runner._send_voice_reply.await_count == int(not streaming_done and not tool_tts)
    adapter.send.assert_not_awaited()
    stored = runner.session_store.load_transcript(entry.session_id)
    assert any(row.get("role") == "user" and row.get("content") == "Hello" for row in stored)
    assert any(row.get("role") == "assistant" and row.get("content") == "Spoken answer" for row in stored)


@pytest.mark.asyncio
async def test_audio_turn_has_no_text_stream_or_interim_lane(tmp_path):
    runner, adapter, event = setup_voice(tmp_path)
    runner._run_still_current_fn = lambda *a: lambda: True
    disp = runner._RunAgentDisplay(user_config={}, platform_key="discord", enabled_toolsets=[],
                                    resolve_display_setting=lambda *a: True,
                                    interim_assistant_messages_enabled=True, needs_progress_queue=True)
    ctx, turn, _cleanup = runner._run_agent_build_turn_context(
        disp, lambda **kw: None, message="hello", source=event.source, session_key=None,
        run_generation=None, voice_turn=True)
    consumer, delta, interim, want_interim = turn._setup_stream_consumer("discord")
    assert consumer is None and not want_interim
    assert not ctx.interim_assistant_messages_enabled and not ctx.tool_progress_enabled
    assert ctx.resolve_display_setting({}, "discord", "streaming") is False
