from pathlib import Path

import pytest

from palserver_manager.config import AppConfig, FleetHostConfig, FleetServerRef, load_config, save_config
from palserver_manager.host_ops import (
    _combined_output,
    _prepare_steamcmd_for_user,
    _run_steamcmd,
    _safe_remove_tree,
    _steamcmd_as_user,
)
from palserver_manager.provisioning import LinuxHostBootstrapper


def test_steamcmd_launcher_permissions_are_repaired(tmp_path: Path):
    launcher = tmp_path / "steamcmd.sh"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o600)
    _prepare_steamcmd_for_user(launcher, "palworld")
    assert launcher.stat().st_mode & 0o111


def test_uninstall_refuses_protected_root_paths():
    with pytest.raises(ValueError):
        _safe_remove_tree("/opt", "Palworld install directory")
    with pytest.raises(ValueError):
        _safe_remove_tree("/", "Palworld install directory")


def test_agent_host_only_mode_survives_reload_and_can_add_server(tmp_path: Path):
    path = tmp_path / "config.json"
    cfg = AppConfig().resolve()
    cfg.instances = []
    cfg.active_instance_id = ""
    cfg.host_only_mode = True
    save_config(cfg, path)

    loaded = load_config(path)
    assert loaded.host_only_mode is True
    assert loaded.instances == []
    assert loaded.active_instance_id == ""

    created = loaded.add_instance({
        "id": "002",
        "name": "Reinstalled Server",
        "server": {
            "install_dir": "/opt/palworld",
            "service_name": "palworld",
            "game_port": 8211,
            "rest_api_port": 8212,
        },
    })
    assert created.id == "002"
    assert loaded.host_only_mode is False
    assert loaded.active_instance_id == "002"


def test_bootstrapper_agent_uninstall_preserves_game_files(monkeypatch):
    bootstrapper = LinuxHostBootstrapper.__new__(LinuxHostBootstrapper)
    bootstrapper.host = "example.test"
    bootstrapper.user = "palworld"
    bootstrapper.port = 22
    bootstrapper.key_file = "key"
    seen = []

    monkeypatch.setattr(bootstrapper, "test_connection", lambda: {"hostname": "node-02", "os": "Linux"})
    monkeypatch.setattr(bootstrapper, "_sudo", lambda: "sudo -n ")
    monkeypatch.setattr(bootstrapper, "run", lambda command, timeout=120, progress=None: seen.append(command) or "")

    result = bootstrapper.uninstall_agent(progress=lambda _line: None)
    command = "\n".join(seen)
    assert result["agent_uninstalled"] is True
    assert "/opt/palserver-manager" in command
    assert "/etc/palserver-manager" in command
    assert "/opt/palworld" not in command


def test_steamcmd_uses_game_users_home_with_sudo(monkeypatch):
    monkeypatch.setattr("palserver_manager.host_ops.shutil_which", lambda name: "/usr/bin/sudo" if name == "sudo" else None)
    command = _steamcmd_as_user(["/opt/steamcmd/steamcmd.sh", "+quit"], "palworld")
    assert command[:6] == ["sudo", "-u", "palworld", "-H", "--", "/opt/steamcmd/steamcmd.sh"]


def test_steamcmd_runuser_fallback_supplies_home(monkeypatch):
    monkeypatch.setattr("palserver_manager.host_ops.shutil_which", lambda name: "/usr/sbin/runuser" if name == "runuser" else None)
    monkeypatch.setattr("palserver_manager.host_ops._steam_user_home", lambda _user: "/home/palworld")
    command = _steamcmd_as_user(["/opt/steamcmd/steamcmd.sh", "+quit"], "palworld")
    assert command[:5] == ["runuser", "-u", "palworld", "--", "env"]
    assert "HOME=/home/palworld" in command
    assert "USER=palworld" in command
    assert "LOGNAME=palworld" in command


def test_command_error_output_keeps_stdout_and_stderr():
    import subprocess
    result = subprocess.CompletedProcess(["steamcmd"], 1, stdout="actual Steam error", stderr="launcher startup")
    output = _combined_output(result)
    assert "actual Steam error" in output
    assert "launcher startup" in output


def test_steamcmd_retries_after_self_update_restart(monkeypatch, tmp_path: Path):
    import subprocess
    launcher = tmp_path / "steamcmd.sh"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    attempts = iter([
        subprocess.CompletedProcess(["steamcmd"], 42, stdout="", stderr="Restarting steamcmd by request"),
        subprocess.CompletedProcess(["steamcmd"], 0, stdout="Success", stderr=""),
    ])
    monkeypatch.setattr("palserver_manager.host_ops._run", lambda *args, **kwargs: next(attempts))
    monkeypatch.setattr("palserver_manager.host_ops._steamcmd_invocation", lambda *args, **kwargs: ["steamcmd"] )
    monkeypatch.setattr("palserver_manager.host_ops._prepare_steamcmd_for_user", lambda *args, **kwargs: None)
    result = _run_steamcmd(str(launcher), ["+quit"], "palworld")
    assert result.returncode == 0
