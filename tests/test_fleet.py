from pathlib import Path

from palserver_manager.config import AppConfig, FleetHostConfig, FleetServerRef, load_config, save_config


def test_remote_legacy_config_migrates_to_fleet_001(tmp_path: Path):
    path = tmp_path / "config.json"
    cfg = AppConfig()
    cfg.connection.mode = "ssh"
    cfg.connection.ssh_host = "example.test"
    cfg.connection.ssh_user = "root"
    cfg.connection.ssh_key_file = "/tmp/test-key"
    cfg.connection.remote_token = "secret"
    save_config(cfg, path)

    loaded = load_config(path)
    assert loaded.fleet_hosts[0].id == "host-001"
    assert loaded.fleet_hosts[0].ssh_host == "example.test"
    assert loaded.fleet_servers[0].id == "001"
    assert loaded.fleet_servers[0].remote_instance_id == "default"
    assert loaded.active_fleet_server_id == "001"


def test_fleet_ids_increment_from_001():
    cfg = AppConfig()
    cfg.connection.mode = "ssh"
    cfg.connection.ssh_host = "one.example"
    cfg.resolve()
    assert cfg.next_fleet_server_id() == "002"
    cfg.fleet_servers.append(FleetServerRef(id="002", name="Two", host_id="host-001", remote_instance_id="two"))
    assert cfg.next_fleet_server_id() == "003"
    assert cfg.next_fleet_host_id() == "host-002"


def test_local_agent_does_not_create_desktop_fleet():
    cfg = AppConfig().resolve()
    assert cfg.connection.mode == "local"
    assert cfg.fleet_hosts == []
    assert cfg.fleet_servers == []


def test_fleet_manager_routes_global_001_to_remote_default(monkeypatch, tmp_path: Path):
    import palserver_manager.remote as remote_mod

    class FakeRemote:
        def __init__(self, cfg):
            self.cfg = cfg
            self.instance_id = cfg.active_instance_id
        def close(self):
            pass
        def instances(self, include_status=True):
            return [{"id": "default", "name": "Primary Server", "state": "active", "service_name": "palworld", "game_port": 8211, "rest_api_port": 8212, "install_dir": "/opt/palworld"}]
        def select_instance(self, instance_id):
            self.instance_id = instance_id
            return {"id": instance_id}
        def update_instance(self, instance_id, payload):
            return {"id": instance_id, "name": payload.get("name", "Primary Server")}
        def set_setting(self, key, value):
            return {"key": key, "value": value}

    monkeypatch.setattr(remote_mod, "RemoteManager", FakeRemote)
    cfg = AppConfig()
    cfg.connection.mode = "ssh"
    cfg.connection.ssh_host = "host.example"
    cfg.connection.ssh_user = "root"
    cfg.connection.ssh_key_file = str(tmp_path / "key")
    cfg.connection.remote_token = "token"
    cfg.resolve()

    manager = remote_mod.FleetManager(cfg)
    rows = manager.instances(True)
    assert rows[0]["id"] == "001"
    assert rows[0]["remote_instance_id"] == "default"
    assert rows[0]["state"] == "active"
    selected = manager.select_instance("001")
    assert selected["remote_instance_id"] == "default"
    renamed = manager.rename_instance("001", "Scryptos Server")
    assert renamed["name"] == "Scryptos Server"


def test_different_hosts_may_reuse_game_and_rest_ports(monkeypatch, tmp_path: Path):
    import palserver_manager.remote as remote_mod

    class FakeRemote:
        def __init__(self, cfg):
            self.cfg = cfg
            self.instance_id = "default"
        def close(self):
            pass
        def instances(self, include_status=True):
            return [{
                "id": "default",
                "name": f"Server on {self.cfg.connection.ssh_host}",
                "state": "active",
                "service_name": "palworld",
                "game_port": 8211,
                "rest_api_port": 8212,
                "install_dir": "/opt/palworld",
            }]

    monkeypatch.setattr(remote_mod, "RemoteManager", FakeRemote)
    cfg = AppConfig()
    cfg.connection.mode = "ssh"
    cfg.connection.ssh_host = "one.example"
    cfg.connection.ssh_user = "root"
    cfg.connection.ssh_key_file = str(tmp_path / "key")
    cfg.connection.remote_token = "token-one"
    cfg.resolve()
    cfg.fleet_hosts.append(FleetHostConfig(
        id="host-002", name="Two", mode="ssh", ssh_host="two.example",
        ssh_user="root", ssh_key_file=str(tmp_path / "key"), agent_token="token-two",
        local_tunnel_port=18766,
    ))
    cfg.fleet_servers.append(FleetServerRef(
        id="002", name="Two", host_id="host-002", remote_instance_id="default",
    ))

    rows = remote_mod.FleetManager(cfg).instances(True)
    assert len(rows) == 2
    assert {row["host_id"] for row in rows} == {"host-001", "host-002"}
    assert {row["game_port"] for row in rows} == {8211}
    assert {row["rest_api_port"] for row in rows} == {8212}
    assert {row["service_name"] for row in rows} == {"palworld"}
    assert {row["install_dir"] for row in rows} == {"/opt/palworld"}


def test_wait_for_host_info_retries_after_transient_disconnect():
    from palserver_manager.remote import FleetManager

    class Remote:
        def __init__(self, owner):
            self.owner = owner

        def host_info(self):
            self.owner.calls += 1
            if self.owner.calls < 3:
                raise ConnectionAbortedError(10053, "connection aborted")
            return {"agent_version": "0.4.8", "hostname": "palworld-node-02"}

    class FakeFleet:
        def __init__(self):
            self.calls = 0
            self.resets = 0
            self.retries = []

        def reset_host_connection(self, host_id):
            self.resets += 1

        def remote_for_host(self, host_id):
            return Remote(self)

    fake = FakeFleet()
    result = FleetManager.wait_for_host_info(
        fake,
        "host-002",
        attempts=4,
        delay=0,
        on_retry=lambda attempt, attempts, exc: fake.retries.append((attempt, attempts, type(exc))),
    )

    assert result["agent_version"] == "0.4.8"
    assert fake.calls == 3
    assert fake.resets == 3
    assert len(fake.retries) == 2


def test_wait_for_host_info_reports_failure_only_after_retries():
    from palserver_manager.remote import FleetManager, RemoteError

    class Remote:
        def host_info(self):
            raise ConnectionAbortedError(10053, "connection aborted")

    class FakeFleet:
        def reset_host_connection(self, host_id):
            pass

        def remote_for_host(self, host_id):
            return Remote()

    try:
        FleetManager.wait_for_host_info(FakeFleet(), "host-002", attempts=2, delay=0)
    except RemoteError as exc:
        assert "2 reconnect attempt" in str(exc)
        assert "10053" in str(exc)
    else:
        raise AssertionError("Expected RemoteError after reconnect attempts are exhausted")


def test_active_fleet_host_tracks_selected_server_and_persists(tmp_path: Path):
    path = tmp_path / "config.json"
    cfg = AppConfig()
    cfg.connection.mode = "ssh"
    cfg.connection.ssh_host = "one.example"
    cfg.connection.ssh_user = "root"
    cfg.connection.ssh_key_file = "/tmp/key"
    cfg.connection.remote_token = "one"
    cfg.resolve()
    cfg.fleet_hosts.append(FleetHostConfig(
        id="host-002", name="Node Two", mode="ssh", ssh_host="two.example",
        ssh_user="palworld", ssh_key_file="/tmp/key", agent_token="two",
        local_tunnel_port=18766,
    ))
    cfg.fleet_servers.append(FleetServerRef(
        id="002", name="Server Two", host_id="host-002", remote_instance_id="two",
    ))
    cfg.active_fleet_host_id = "host-002"
    cfg.active_fleet_server_id = "002"
    save_config(cfg, path)

    loaded = load_config(path)
    assert loaded.active_fleet_host_id == "host-002"
    assert loaded.active_fleet_server_id == "002"


def test_fleet_manager_select_host_filters_context_to_that_nodes_server(monkeypatch, tmp_path: Path):
    import palserver_manager.remote as remote_mod

    class FakeRemote:
        def __init__(self, cfg):
            self.cfg = cfg
            self.instance_id = "default"
        def close(self):
            pass
        def select_instance(self, instance_id):
            self.instance_id = instance_id
            return {"id": instance_id}

    monkeypatch.setattr(remote_mod, "RemoteManager", FakeRemote)
    cfg = AppConfig()
    cfg.connection.mode = "ssh"
    cfg.connection.ssh_host = "one.example"
    cfg.connection.ssh_user = "root"
    cfg.connection.ssh_key_file = str(tmp_path / "key")
    cfg.connection.remote_token = "one"
    cfg.resolve()
    cfg.fleet_hosts.append(FleetHostConfig(
        id="host-002", name="Node Two", mode="ssh", ssh_host="two.example",
        ssh_user="palworld", ssh_key_file=str(tmp_path / "key"), agent_token="two",
        local_tunnel_port=18766,
    ))
    cfg.fleet_servers.append(FleetServerRef(
        id="002", name="Server Two", host_id="host-002", remote_instance_id="server-two",
    ))

    manager = remote_mod.FleetManager(cfg)
    result = manager.select_host("host-002")
    assert result["host_id"] == "host-002"
    assert result["server_id"] == "002"
    assert result["remote_instance_id"] == "server-two"
    assert cfg.active_fleet_host_id == "host-002"
    assert cfg.active_fleet_server_id == "002"


def test_selecting_server_updates_active_host(monkeypatch, tmp_path: Path):
    import palserver_manager.remote as remote_mod

    class FakeRemote:
        def __init__(self, cfg):
            self.cfg = cfg
            self.instance_id = "default"
        def close(self):
            pass
        def select_instance(self, instance_id):
            self.instance_id = instance_id
            return {"id": instance_id}

    monkeypatch.setattr(remote_mod, "RemoteManager", FakeRemote)
    cfg = AppConfig()
    cfg.connection.mode = "ssh"
    cfg.connection.ssh_host = "one.example"
    cfg.connection.ssh_user = "root"
    cfg.connection.ssh_key_file = str(tmp_path / "key")
    cfg.connection.remote_token = "one"
    cfg.resolve()
    cfg.fleet_hosts.append(FleetHostConfig(
        id="host-002", name="Node Two", mode="ssh", ssh_host="two.example",
        ssh_user="palworld", ssh_key_file=str(tmp_path / "key"), agent_token="two",
        local_tunnel_port=18766,
    ))
    cfg.fleet_servers.append(FleetServerRef(
        id="002", name="Server Two", host_id="host-002", remote_instance_id="server-two",
    ))

    manager = remote_mod.FleetManager(cfg)
    result = manager.select_instance("002")
    assert result["host_id"] == "host-002"
    assert cfg.active_fleet_host_id == "host-002"
    assert cfg.active_fleet_server_id == "002"


def test_fleet_manager_can_select_empty_node_without_leaking_other_server_context(monkeypatch, tmp_path: Path):
    import palserver_manager.remote as remote_mod

    class FakeRemote:
        def __init__(self, cfg):
            self.cfg = cfg
            self.instance_id = "default"
        def close(self):
            pass
        def select_instance(self, instance_id):
            self.instance_id = instance_id
            return {"id": instance_id}

    monkeypatch.setattr(remote_mod, "RemoteManager", FakeRemote)
    cfg = AppConfig()
    cfg.connection.mode = "ssh"
    cfg.connection.ssh_host = "one.example"
    cfg.connection.ssh_user = "root"
    cfg.connection.ssh_key_file = str(tmp_path / "key")
    cfg.connection.remote_token = "one"
    cfg.resolve()
    cfg.fleet_hosts.append(FleetHostConfig(
        id="host-002", name="Empty Node", mode="ssh", ssh_host="two.example",
        ssh_user="palworld", ssh_key_file=str(tmp_path / "key"), agent_token="two",
        local_tunnel_port=18766,
    ))

    manager = remote_mod.FleetManager(cfg)
    result = manager.select_host("host-002")
    assert result == {"host_id": "host-002", "server_id": "", "remote_instance_id": ""}
    assert cfg.active_fleet_host_id == "host-002"
    # The old server ID remains stored for later return to that node, but callers
    # can detect that it does not belong to the active node.
    assert cfg.active_fleet_server_id == "001"
