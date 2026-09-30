"""Real high-descriptor output regression for long-lived gateways."""
import os
import shlex
import pytest
from tools.environments.local import LocalEnvironment

resource = pytest.importorskip("resource")

@pytest.mark.skipif(os.name == "nt", reason="POSIX high-fd collector regression")
def test_high_fd_terminal_and_file_output(tmp_path):
    if resource.getrlimit(resource.RLIMIT_NOFILE)[0] < 1300:
        pytest.skip("requires at least 1300 available descriptors")
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("FILE_CONTENT_PROBE\n")
    held = []
    env = None
    try:
        while not held or held[-1] <= 1100:
            held.append(os.open(os.devnull, os.O_RDONLY))
        env = LocalEnvironment(cwd=str(tmp_path), timeout=10)
        result = env.execute("printf 'TERMINAL_OUTPUT_PROBE\n'", timeout=10)
        assert result["returncode"] == 0
        assert "TERMINAL_OUTPUT_PROBE" in result["output"]
        result = env.execute("/bin/cat " + shlex.quote(str(fixture)), timeout=10)
        assert "FILE_CONTENT_PROBE" in result["output"]
        result = env.execute("/usr/bin/grep FILE_CONTENT_PROBE " + shlex.quote(str(fixture)), timeout=10)
        assert "FILE_CONTENT_PROBE" in result["output"]
    finally:
        if env:
            env.cleanup()
        for fd in held:
            os.close(fd)
