import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from palserver_manager.config import AppConfig, load_config, save_config


def test_legacy_config_migrates_to_default_instance(tmp_path: Path):
    path = tmp_path / "config.json"
    cfg = AppConfig()
    cfg.server.install_dir = str(tmp_path / "palworld")
    cfg.server.game_port = 9001
    save_config(cfg, path)

    loaded = load_config(path)
    assert loaded.instances
    assert loaded.instances[0].id == "default"
    assert loaded.instances[0].server.game_port == 9001
    assert loaded.active_instance_id == "default"


def test_add_instance_gets_independent_paths_ports_and_service(tmp_path: Path):
    cfg = AppConfig().resolve()
    cfg.server.install_dir = str(tmp_path / "palworld")
    cfg.server.backup_dir = str(tmp_path / "backups")
    cfg.server.service_name = "palworld"
    cfg.server.game_port = 8211
    cfg.server.rest_api_port = 8212

    instance = cfg.add_instance({"name": "Community Server"})
    assert instance.id == "community-server"
    assert instance.server.game_port != cfg.server.game_port
    assert instance.server.rest_api_port != cfg.server.rest_api_port
    assert instance.server.service_name != cfg.server.service_name
    assert instance.server.install_dir != cfg.server.install_dir
    assert instance.server.backup_dir != cfg.server.backup_dir


def test_instances_persist(tmp_path: Path):
    path = tmp_path / "config.json"
    cfg = AppConfig().resolve()
    created = cfg.add_instance({
        "name": "Second Server",
        "server": {"install_dir": str(tmp_path / "second"), "game_port": 8311, "rest_api_port": 8312},
    })
    cfg.active_instance_id = created.id
    save_config(cfg, path)

    loaded = load_config(path)
    assert len(loaded.instances) == 2
    assert loaded.active_instance_id == created.id
    assert loaded.instance(created.id).server.game_port == 8311
    assert loaded.instance(created.id).server.config_path.startswith(str(tmp_path / "second"))


def test_agent_routes_requests_to_selected_instance(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("PALSERVER_MANAGER_CONFIG", str(path))
    cfg = load_config(path)
    cfg.server.install_dir = str(tmp_path / "primary")
    cfg.server.game_port = 8211
    second = cfg.add_instance({
        "name": "Second",
        "server": {
            "install_dir": str(tmp_path / "second"),
            "service_name": "palworld-second",
            "game_port": 8311,
            "rest_api_port": 8312,
        },
    })
    save_config(cfg, path)

    from palserver_manager.agent import create_app
    app = create_app()
    token = load_config(path).agent.token
    headers = {"X-PalManager-Token": token, "X-PalManager-Instance": second.id}
    with TestClient(app) as client:
        instances = client.get("/v1/instances", headers={"X-PalManager-Token": token})
        assert instances.status_code == 200
        assert len(instances.json()) == 2
        response = client.get("/v1/server-config", headers=headers)
        assert response.status_code == 200
        assert response.json()["game_port"] == 8311


def test_host_only_config_does_not_create_synthetic_primary(tmp_path: Path):
    path = tmp_path / "agent-host.json"
    cfg = AppConfig().resolve()
    cfg.instances = []
    cfg.active_instance_id = ""
    cfg.host_only_mode = True
    save_config(cfg, path)

    loaded = load_config(path)
    assert loaded.host_only_mode is True
    assert loaded.instances == []
    assert loaded.active_instance_id == ""


@pytest.mark.skipif(os.name == "nt", reason="Synthetic bootstrap placeholder migration is Linux-agent behavior")
def test_registry_identifies_only_untouched_bootstrap_placeholder():
    from palserver_manager.agent import InstanceRegistry

    cfg = AppConfig().resolve()
    registry = InstanceRegistry(cfg)
    assert registry.bootstrap_placeholder_id() == "default"

    cfg.instance("default").server.admin_password = "configured-secret"
    assert registry.bootstrap_placeholder_id() == ""


def test_registry_discovery_is_idempotent_and_prunes_stale_duplicate(tmp_path: Path, monkeypatch):
    from palserver_manager.agent import InstanceRegistry

    monkeypatch.setenv("PALSERVER_MANAGER_CONFIG", str(tmp_path / "agent.json"))
    real_install = tmp_path / "palworld"
    real_install.mkdir()
    (real_install / "PalServer.sh").write_text("#!/bin/sh\n")
    stale_install = tmp_path / "stale-palworld"

    cfg = AppConfig().resolve()
    primary = cfg.instance("default")
    primary.name = "Scryptos Test Server"
    primary.server.install_dir = str(stale_install)
    primary.server.service_name = "palworld"
    primary.server.game_port = 8213
    primary.server.rest_api_port = 8212
    primary.server.resolve()
    duplicate = cfg.add_instance({
        "id": "002",
        "name": "Scryptos Test Server",
        "server": {
            "install_dir": str(real_install),
            "service_name": "palworld",
            "game_port": 8213,
            "rest_api_port": 8212,
        },
    })
    registry = InstanceRegistry(cfg)
    discovered = {
        "install_dir": str(real_install),
        "config_path": duplicate.server.config_path,
        "service_name": "palworld",
        "game_port": 8213,
        "rest_api_port": 8212,
    }
    assert registry.find_discovered_match(discovered) == "002"
    removed = registry.prune_stale_discovery_duplicates(discovered, "002")
    assert "default" in removed
    assert [row.id for row in cfg.instances] == ["002"]
