"""Heimdall trust survives native FIFO replay, not a serialized marker or another process."""
from copy import deepcopy
import weakref

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session_authority import initialize_session_authority
from gateway.session_envelope import prepare_native, restore_native, _validate_native
from gateway.session_ingress_context import native_callback
from hermes_state_runtime import RuntimeStoreError


@pytest.mark.asyncio
async def test_heimdall_native_roundtrip_is_bound_to_exact_live_input(tmp_path, monkeypatch):
    from plugins.platforms.discord.adapter import DiscordAdapter

    monkeypatch.setenv('DISCORD_ALLOW_BOTS', 'none')
    monkeypatch.delenv('DISCORD_ALLOWED_USERS', raising=False)
    monkeypatch.delenv('DISCORD_ALLOW_ALL_USERS', raising=False)
    monkeypatch.delenv('GATEWAY_ALLOW_ALL_USERS', raising=False)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='test'))
    adapter.gateway_runner, runner.adapters = runner, {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='heimdall')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='1566365107779412081', user_id='1532801692536410405',
                                  chat_type='thread', thread_id='1566365107779412081',
                                  parent_chat_id='100000000000000002', guild_id='100000000000000001',
                                  is_bot=True, message_id='101')
    source._trusted_heimdall_incident = True  # adapter's exact-intake seam, tested separately
    source._transport_adapter_ref = weakref.ref(adapter)
    event = MessageEvent(text='[Untrusted Discord webhook alert evidence]\nAlert', source=source, message_id='101')
    try:
        with native_callback(runner, event, runner._native_transport_homes[None]):
            payload = await prepare_native(runner, event)
        restored = restore_native(payload, runner)
        _validate_native(runner, restored, payload['native_text_v1']['provenance'])
        assert restored.source._trusted_heimdall_incident is True
        assert restored.source.chat_type == 'thread'

        for mutate in ('text', 'author', 'marker', 'event', 'route', 'seal'):
            forged = deepcopy(payload)
            if mutate == 'text':
                forged['text'] = 'different historical webhook body'
            elif mutate == 'author':
                forged['native_text_v1']['source']['user_id'] = '999'
            elif mutate == 'marker':
                forged['native_text_v1']['source']['_trusted_heimdall_incident'] = True
            elif mutate == 'event':
                forged['native_text_v1']['event']['reply_to_text'] = 'injected context'
            elif mutate == 'route':
                forged['native_text_v1']['route'] = 'different-route'
            else:
                forged['native_text_v1']['heimdall_seal'] = 'forged'
            with pytest.raises(RuntimeStoreError):
                candidate = restore_native(forged, runner)
                _validate_native(runner, candidate, forged['native_text_v1']['provenance'])

        unsigned = deepcopy(payload)
        unsigned['native_text_v1'].pop('heimdall_seal')
        with pytest.raises(RuntimeStoreError):
            candidate = restore_native(unsigned, runner)
            _validate_native(runner, candidate, unsigned['native_text_v1']['provenance'])
        unsigned['native_text_v1']['source']['_trusted_heimdall_incident'] = True
        with pytest.raises(RuntimeStoreError):
            restore_native(unsigned, runner)

        # A restarted runner has no in-process key. Even an intact old body/seal is not a grant.
        runner.__dict__.pop('_heimdall_native_key', None)
        with pytest.raises(RuntimeStoreError):
            candidate = restore_native(payload, runner)
            _validate_native(runner, candidate, payload['native_text_v1']['provenance'])
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_heimdall_text_batch_runs_once_through_native_fifo(tmp_path, monkeypatch):
    from plugins.platforms.discord.adapter import DiscordAdapter
    from gateway.platforms.base import SendResult
    from gateway.session_results import execution_result
    from tests.waiting import async_wait_until

    monkeypatch.setenv('DISCORD_ALLOW_BOTS', 'none')
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='test', extra={
        'text_batch_delay_seconds': 0}))
    adapter.gateway_runner, runner.adapters = runner, {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='batch')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='1566365107779412081', user_id='1532801692536410405',
                                  chat_type='thread', thread_id='1566365107779412081',
                                  parent_chat_id='100000000000000002', guild_id='100000000000000001',
                                  is_bot=True, message_id='102')
    source._trusted_heimdall_incident = True
    source._transport_adapter_ref = weakref.ref(adapter)
    event = MessageEvent(text='[Untrusted Discord webhook alert evidence]\nAlert', source=source, message_id='102')
    event._ingress_sha256 = 'a' * 64
    turns, sent = [], []

    async def send(chat_id, content, **_):
        sent.append(content)
        return SendResult(success=True, message_id='reply')

    async def turn(restored, *_args, **_kwargs):
        assert restored.source._trusted_heimdall_incident is True
        turns.append(restored.text)
        execution_result.get()['result'] = {'final_response': 'ok', 'messages': []}
        return 'ok'

    monkeypatch.setattr(adapter, 'send', send)
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    try:
        adapter._enqueue_text_event(event)
        await async_wait_until(lambda: bool(sent) and not adapter._background_tasks,
                               timeout=20, what='Heimdall FIFO reply')
        assert turns == [event.text]
        assert sent == ['ok']
    finally:
        authority.db.close()
