import sys

from palserver_manager.provisioning import LinuxHostBootstrapper


def test_streaming_process_emits_progress_lines():
    bootstrapper = LinuxHostBootstrapper.__new__(LinuxHostBootstrapper)
    seen = []
    output = bootstrapper._run_process(
        [sys.executable, "-u", "-c", "print('phase-one'); print('phase-two')"],
        timeout=10,
        progress=seen.append,
        error_label="test process",
    )
    assert output.splitlines() == ["phase-one", "phase-two"]
    assert seen == ["phase-one", "phase-two"]


def test_host_command_streaming_emits_live_lines():
    from palserver_manager.host_ops import _run_streaming

    seen = []
    result = _run_streaming(
        [sys.executable, "-u", "-c", "print('steam-phase'); print('download-phase')"],
        timeout=10,
        progress=seen.append,
    )
    assert result.returncode == 0
    assert seen == ["steam-phase", "download-phase"]
