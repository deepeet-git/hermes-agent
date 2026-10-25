"""Real codec coverage for explicit DAVE state, isolated from gateway's SDK stubs."""
from pathlib import Path
import subprocess
import sys

import pytest


_CODEC_SCRIPT = r'''
import sys
from unittest.mock import MagicMock
from tests.integration.test_voice_channel_flow import (
    OPUS_AVAILABLE, _build_encrypted_rtp_packet, _build_padded_rtp_packet,
    _make_secret_key, _make_voice_receiver,
)
assert OPUS_AVAILABLE, "real libopus is required"
padded, protocol = bool(int(sys.argv[1])), int(sys.argv[2])
key = _make_secret_key()
dave = MagicMock()
receiver = _make_voice_receiver(key, dave_session=dave)
connection = receiver._vc._connection
connection.dave_protocol_version = protocol
connection.dave_downgraded = False
connection.dave_pending_transitions = {}
receiver.refresh_credentials("explicit test protocol")
builder = _build_padded_rtp_packet if padded else _build_encrypted_rtp_packet
kwargs = {"pad_len": 4} if padded else {}
try:
    receiver._on_packet(builder(key, b"\xf8\xff\xfe", ssrc=100, **kwargs))
    dave.decrypt.assert_not_called()
    if protocol == 0:
        assert receiver._buffers[100]
    else:
        assert 100 not in receiver._buffers
        assert receiver._dave_unmapped_dropped == 1
finally:
    receiver.stop()
'''


@pytest.mark.parametrize("padded", [False, True])
@pytest.mark.parametrize("protocol", [0, 1])
def test_unknown_ssrc_only_passes_through_when_e2ee_is_inactive(padded, protocol):
    # gateway/conftest installs a Discord stub before collection. A fresh interpreter exercises
    # the locked real SDK/NaCl/Opus stack without weakening or changing that shared fixture.
    result = subprocess.run(
        [sys.executable, "-c", _CODEC_SCRIPT, str(int(padded)), str(protocol)],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
