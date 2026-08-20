from __future__ import annotations

import json
import os
import platform
import re
import secrets
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


APP_DIR_NAME = "PalServerManager"


def user_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / APP_DIR_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "palserver-manager"


def default_install_dir() -> str:
    if os.name == "nt":
        candidates = [
            Path(r"C:\Program Files (x86)\Steam\steamapps\common\PalServer"),
            Path(r"C:\steamcmd\steamapps\common\PalServer"),
            Path(r"C:\PalServer"),
        ]
    else:
        candidates = [
            Path("/opt/palworld"),
            Path.home() / "Steam" / "steamapps" / "common" / "PalServer",
            Path.home() / ".steam" / "steam" / "steamapps" / "common" / "PalServer",
        ]
    for item in candidates:
        if item.exists():
            return str(item)
    return str(candidates[0])


def default_steamcmd() -> str:
    if os.name == "nt":
        candidates = [Path(r"C:\steamcmd\steamcmd.exe"), Path(r"C:\SteamCMD\steamcmd.exe")]
    else:
        candidates = [Path("/usr/games/steamcmd"), Path("/usr/bin/steamcmd"), Path.home() / "steamcmd" / "steamcmd.sh"]
    for item in candidates:
        if item.exists():
            return str(item)
    return str(candidates[0])


def normalize_instance_id(value: str) -> str:
    """Return a stable, URL/header-safe instance id."""
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return text or "server"


@dataclass
class ServerConfig:
    install_dir: str = field(default_factory=default_install_dir)
    steamcmd_path: str = field(default_factory=default_steamcmd)
    steam_user: str = "palworld"
    service_name: str = "palworld" if os.name != "nt" else "PalServer"
    launch_args: list[str] = field(default_factory=lambda: ["-useperfthreads", "-NoAsyncLoadingThread", "-UseMultithreadForDS"])
    config_path: str = ""
    save_dir: str = ""
    backup_dir: str = ""
    log_file: str = ""
    rest_api_host: str = "127.0.0.1"
    rest_api_port: int = 8212
    rest_api_username: str = "admin"
    admin_password: str = ""
    game_port: int = 8211
    app_id: str = "2394010"

    def resolve(self) -> "ServerConfig":
        root = Path(self.install_dir)
        if not self.config_path:
            cfg_folder = "WindowsServer" if os.name == "nt" else "LinuxServer"
            self.config_path = str(root / "Pal" / "Saved" / "Config" / cfg_folder / "PalWorldSettings.ini")
        if not self.save_dir:
            self.save_dir = str(root / "Pal" / "Saved" / "SaveGames")
        if not self.backup_dir:
            self.backup_dir = str(root.parent / "palworld-backups")
        if not self.log_file:
            self.log_file = str(user_data_dir() / "palserver.log")
        return self


@dataclass
class BackupConfig:
    enabled: bool = True
    interval_minutes: int = 120
    retention_count: int = 30
    backup_before_update: bool = True
    backup_before_restore: bool = True


@dataclass
class UpdateConfig:
    auto_check: bool = True
    check_interval_minutes: int = 60
    auto_install: bool = False
    only_when_empty: bool = True
    maintenance_start: str = "04:00"
    maintenance_end: str = "05:00"


@dataclass
class AgentConfig:
    bind_host: str = "127.0.0.1"
    port: int = 8765
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    tls_cert: str = ""
    tls_key: str = ""
    allow_direct_wan: bool = False


@dataclass
class ConnectionConfig:
    mode: str = "local"  # local, direct, ssh
    remote_url: str = "https://server.example.com:8765"
    remote_token: str = ""
    verify_tls: bool = True
    ca_bundle: str = ""
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_key_file: str = ""
    ssh_local_port: int = 18765
    ssh_remote_agent_port: int = 8765


@dataclass
class FleetHostConfig:
    """One remote PalServer Manager agent host known to the desktop manager."""

    id: str = "host-001"
    name: str = "Primary Host"
    mode: str = "ssh"
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_key_file: str = ""
    local_tunnel_port: int = 18765
    remote_agent_port: int = 8765
    agent_token: str = ""
    verify_tls: bool = True
    remote_url: str = ""
    enabled: bool = True
    os_type: str = "linux"


@dataclass
class FleetServerRef:
    """Global server reference that maps a manager ID to an agent-local instance."""

    id: str = "001"
    name: str = "Primary Server"
    host_id: str = "host-001"
    remote_instance_id: str = "default"
    enabled: bool = True


def next_numeric_id(values, width: int = 3) -> str:
    used = []
    for value in values:
        try:
            used.append(int(str(value)))
        except (TypeError, ValueError):
            continue
    return f"{(max(used) if used else 0) + 1:0{width}d}"


@dataclass
class HealthConfig:
    cpu_warning: float = 85.0
    cpu_critical: float = 95.0
    ram_warning: float = 85.0
    ram_critical: float = 95.0
    disk_warning: float = 85.0
    disk_critical: float = 95.0
    stale_backup_hours: float = 6.0


@dataclass
class ServerInstanceConfig:
    """One independently managed Palworld dedicated-server instance."""

    id: str = "default"
    name: str = "Primary Server"
    enabled: bool = True
    server: ServerConfig = field(default_factory=ServerConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    updates: UpdateConfig = field(default_factory=UpdateConfig)
    health: HealthConfig = field(default_factory=HealthConfig)

    def resolve(self) -> "ServerInstanceConfig":
        self.id = normalize_instance_id(self.id)
        self.name = str(self.name or self.id).strip() or self.id
        self.server.resolve()
        return self


@dataclass
class AppConfig:
    # Legacy/default aliases are retained for compatibility with modules and
    # existing config files. They point at the primary instance after resolve().
    server: ServerConfig = field(default_factory=ServerConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    updates: UpdateConfig = field(default_factory=UpdateConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    github_repo: str = ""
    page_size: int = 8
    # Desktop-only mod catalog settings. CurseForge API credentials are never
    # forwarded to remote agents. An environment variable can override the
    # stored key for administrators who do not want it persisted in JSON.
    curseforge_api_key: str = ""
    curseforge_game_id: int = 0
    mod_catalog_provider: str = "curated"
    instances: list[ServerInstanceConfig] = field(default_factory=list)
    active_instance_id: str = "default"
    host_only_mode: bool = False
    # Desktop-side fleet catalog. Agent hosts keep these empty because their
    # connection mode is local. The fleet catalog lets one GUI route requests
    # to many independent agent hosts without exposing agent ports publicly.
    fleet_hosts: list[FleetHostConfig] = field(default_factory=list)
    fleet_servers: list[FleetServerRef] = field(default_factory=list)
    active_fleet_host_id: str = ""
    active_fleet_server_id: str = ""

    def resolve(self) -> "AppConfig":
        self.server.resolve()
        if not self.instances and not self.host_only_mode:
            # Seamless migration: the old single-server fields become the
            # primary multi-instance entry without changing any paths/ports.
            self.instances = [
                ServerInstanceConfig(
                    id="default",
                    name="Primary Server",
                    server=self.server,
                    backup=self.backup,
                    updates=self.updates,
                    health=self.health,
                ).resolve()
            ]
        elif self.instances:
            seen: set[str] = set()
            for index, instance in enumerate(self.instances):
                instance.resolve()
                original = instance.id
                suffix = 2
                while instance.id in seen:
                    instance.id = f"{original}-{suffix}"
                    suffix += 1
                seen.add(instance.id)
            primary = next((x for x in self.instances if x.id == "default"), self.instances[0])
            # Keep legacy aliases synchronized with the primary instance.
            self.server = primary.server
            self.backup = primary.backup
            self.updates = primary.updates
            self.health = primary.health

        if self.instances:
            if not any(x.id == self.active_instance_id for x in self.instances):
                self.active_instance_id = self.instances[0].id
        elif self.host_only_mode:
            self.active_instance_id = ""

        # Desktop migration for the original single remote connection. Agents
        # run in local mode and intentionally do not create a fleet catalog.
        if self.connection.mode in {"ssh", "direct"} and not self.fleet_hosts:
            host = FleetHostConfig(
                id="host-001",
                name=self.connection.ssh_host or "Primary Host",
                mode=self.connection.mode,
                ssh_host=self.connection.ssh_host,
                ssh_port=self.connection.ssh_port,
                ssh_user=self.connection.ssh_user,
                ssh_key_file=self.connection.ssh_key_file,
                local_tunnel_port=self.connection.ssh_local_port,
                remote_agent_port=self.connection.ssh_remote_agent_port,
                agent_token=self.connection.remote_token,
                verify_tls=self.connection.verify_tls,
                remote_url=self.connection.remote_url,
            )
            self.fleet_hosts = [host]
        if self.connection.mode in {"ssh", "direct"} and self.fleet_hosts and not self.fleet_servers:
            self.fleet_servers = [FleetServerRef(
                id="001",
                name="Primary Server",
                host_id=self.fleet_hosts[0].id,
                remote_instance_id=self.active_instance_id or "default",
            )]
        if self.fleet_hosts:
            host_ids = {row.id for row in self.fleet_hosts}
            if not self.active_fleet_host_id or self.active_fleet_host_id not in host_ids:
                selected = next((row for row in self.fleet_servers if row.id == self.active_fleet_server_id), None)
                self.active_fleet_host_id = selected.host_id if selected and selected.host_id in host_ids else self.fleet_hosts[0].id
        else:
            self.active_fleet_host_id = ""

        if self.fleet_servers:
            ids = {row.id for row in self.fleet_servers}
            if not self.active_fleet_server_id or self.active_fleet_server_id not in ids:
                same_host = [row for row in self.fleet_servers if row.host_id == self.active_fleet_host_id]
                self.active_fleet_server_id = (same_host[0] if same_host else self.fleet_servers[0]).id
            selected = next((row for row in self.fleet_servers if row.id == self.active_fleet_server_id), None)
            if selected and not self.active_fleet_host_id:
                self.active_fleet_host_id = selected.host_id
        return self

    def instance(self, instance_id: str | None = None) -> ServerInstanceConfig:
        self.resolve()
        target = normalize_instance_id(instance_id or self.active_instance_id)
        for instance in self.instances:
            if instance.id == target:
                return instance
        raise KeyError(f"Unknown server instance: {target}")

    def instance_view(self, instance_id: str | None = None) -> "AppConfig":
        """Return an AppConfig-shaped view backed by one instance's objects.

        The nested objects are intentionally shared with the root config so
        mutations performed by existing managers are reflected in root config.
        """
        instance = self.instance(instance_id)
        view = AppConfig(
            server=instance.server,
            backup=instance.backup,
            updates=instance.updates,
            agent=self.agent,
            connection=self.connection,
            health=instance.health,
            github_repo=self.github_repo,
            page_size=self.page_size,
            instances=[],
            active_instance_id=instance.id,
        )
        # Do not call AppConfig.resolve(), which would create a synthetic
        # instance list. The individual server paths still need resolving.
        view.server.resolve()
        return view

    def fleet_host(self, host_id: str) -> FleetHostConfig:
        for host in self.fleet_hosts:
            if host.id == str(host_id):
                return host
        raise KeyError(f"Unknown fleet host: {host_id}")

    def fleet_server(self, server_id: str | None = None) -> FleetServerRef:
        target = str(server_id or self.active_fleet_server_id)
        for server in self.fleet_servers:
            if server.id == target:
                return server
        raise KeyError(f"Unknown fleet server: {target}")

    def next_fleet_server_id(self) -> str:
        return next_numeric_id([row.id for row in self.fleet_servers], 3)

    def next_fleet_host_id(self) -> str:
        nums = []
        for row in self.fleet_hosts:
            match = re.search(r"(\d+)$", row.id)
            if match:
                nums.append(match.group(1))
        return f"host-{next_numeric_id(nums, 3)}"

    def add_instance(self, payload: dict[str, Any] | None = None) -> ServerInstanceConfig:
        payload = dict(payload or {})
        name = str(payload.get("name") or "New Server").strip() or "New Server"
        requested_id = normalize_instance_id(payload.get("id") or name)
        instance_id = requested_id
        used = {x.id for x in self.instances}
        suffix = 2
        while instance_id in used:
            instance_id = f"{requested_id}-{suffix}"
            suffix += 1

        if self.instances:
            base = self.instance(self.active_instance_id)
            server = ServerConfig(**asdict(base.server))
            backup = BackupConfig(**asdict(base.backup))
            updates = UpdateConfig(**asdict(base.updates))
            health = HealthConfig(**asdict(base.health))
        else:
            # A provisioned host may legitimately have no Palworld server
            # after the final instance is uninstalled.  New installations
            # rebuild from the root defaults and the supplied payload.
            server = ServerConfig(**asdict(self.server))
            backup = BackupConfig(**asdict(self.backup))
            updates = UpdateConfig(**asdict(self.updates))
            health = HealthConfig(**asdict(self.health))
            base = ServerInstanceConfig(id="default", name="Primary Server", server=server, backup=backup, updates=updates, health=health)

        server_values = dict(payload.get("server") or {})
        root = Path(base.server.install_dir)
        if "install_dir" not in server_values:
            server.install_dir = str(root.parent / f"{root.name}-{instance_id}")
            server.config_path = ""
            server.save_dir = ""
            server.log_file = ""
        else:
            server.install_dir = str(server_values["install_dir"])
            if "config_path" not in server_values: server.config_path = ""
            if "save_dir" not in server_values: server.save_dir = ""
            if "log_file" not in server_values: server.log_file = ""
        for key, value in server_values.items():
            if hasattr(server, key) and value is not None:
                setattr(server, key, value)

        existing_game_ports = {int(x.server.game_port) for x in self.instances}
        existing_rest_ports = {int(x.server.rest_api_port) for x in self.instances}
        if "game_port" not in server_values:
            candidate = int(base.server.game_port) + 10
            while candidate in existing_game_ports:
                candidate += 10
            server.game_port = candidate
        if "rest_api_port" not in server_values:
            candidate = int(base.server.rest_api_port) + 10
            while candidate in existing_rest_ports or candidate == server.game_port:
                candidate += 10
            server.rest_api_port = candidate
        if "service_name" not in server_values:
            prefix = str(base.server.service_name or "palworld").split("-")[0]
            server.service_name = f"{prefix}-{instance_id}"
        if "backup_dir" not in server_values:
            server.backup_dir = str(Path(base.server.backup_dir).parent / f"palworld-backups-{instance_id}")
        if "admin_password" not in server_values:
            # Each REST endpoint should have its own explicit credential.
            server.admin_password = ""
        if "log_file" not in server_values:
            server.log_file = str(user_data_dir() / f"palserver-{instance_id}.log")
        server.resolve()

        instance = ServerInstanceConfig(
            id=instance_id,
            name=name,
            enabled=bool(payload.get("enabled", True)),
            server=server,
            backup=backup,
            updates=updates,
            health=health,
        ).resolve()
        self.instances.append(instance)
        self.host_only_mode = False
        if not self.active_instance_id:
            self.active_instance_id = instance.id
        return instance

    def remove_instance(self, instance_id: str) -> ServerInstanceConfig:
        if len(self.instances) <= 1:
            raise ValueError("At least one server instance must remain configured")
        target = self.instance(instance_id)
        self.instances = [x for x in self.instances if x.id != target.id]
        if self.active_instance_id == target.id:
            self.active_instance_id = self.instances[0].id
        # If the primary/default instance was removed, point legacy aliases at
        # the first remaining instance so older code keeps working.
        primary = next((x for x in self.instances if x.id == "default"), self.instances[0])
        self.server = primary.server
        self.backup = primary.backup
        self.updates = primary.updates
        self.health = primary.health
        return target


def config_path() -> Path:
    override = os.environ.get("PALSERVER_MANAGER_CONFIG")
    return Path(override) if override else user_data_dir() / "config.json"


def _merge_dataclass(instance: Any, values: dict[str, Any]) -> Any:
    for key, value in values.items():
        if hasattr(instance, key):
            setattr(instance, key, value)
    return instance


def _load_instance(raw: dict[str, Any]) -> ServerInstanceConfig:
    instance = ServerInstanceConfig(
        id=str(raw.get("id") or "server"),
        name=str(raw.get("name") or raw.get("id") or "Server"),
        enabled=bool(raw.get("enabled", True)),
    )
    if isinstance(raw.get("server"), dict):
        _merge_dataclass(instance.server, raw["server"])
    if isinstance(raw.get("backup"), dict):
        _merge_dataclass(instance.backup, raw["backup"])
    if isinstance(raw.get("updates"), dict):
        _merge_dataclass(instance.updates, raw["updates"])
    if isinstance(raw.get("health"), dict):
        _merge_dataclass(instance.health, raw["health"])
    return instance.resolve()


def _load_fleet_host(raw: dict[str, Any]) -> FleetHostConfig:
    host = FleetHostConfig()
    _merge_dataclass(host, raw)
    return host


def _load_fleet_server(raw: dict[str, Any]) -> FleetServerRef:
    server = FleetServerRef()
    _merge_dataclass(server, raw)
    return server


def load_config(path: Path | None = None) -> AppConfig:
    path = path or config_path()
    cfg = AppConfig()
    if not path.exists():
        cfg.resolve()
        save_config(cfg, path)
        return cfg
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data.get("server"), dict):
        _merge_dataclass(cfg.server, data["server"])
    if isinstance(data.get("backup"), dict):
        _merge_dataclass(cfg.backup, data["backup"])
    if isinstance(data.get("updates"), dict):
        _merge_dataclass(cfg.updates, data["updates"])
    if isinstance(data.get("agent"), dict):
        _merge_dataclass(cfg.agent, data["agent"])
    if isinstance(data.get("connection"), dict):
        _merge_dataclass(cfg.connection, data["connection"])
    if isinstance(data.get("health"), dict):
        _merge_dataclass(cfg.health, data["health"])
    cfg.github_repo = data.get("github_repo", cfg.github_repo)
    cfg.page_size = int(data.get("page_size", cfg.page_size))
    cfg.curseforge_api_key = str(os.environ.get("CURSEFORGE_API_KEY") or data.get("curseforge_api_key", cfg.curseforge_api_key) or "")
    cfg.curseforge_game_id = int(data.get("curseforge_game_id", cfg.curseforge_game_id) or 0)
    cfg.mod_catalog_provider = str(data.get("mod_catalog_provider", cfg.mod_catalog_provider) or "curated")
    cfg.active_instance_id = str(data.get("active_instance_id", cfg.active_instance_id) or "default")
    cfg.host_only_mode = bool(data.get("host_only_mode", cfg.host_only_mode))
    if isinstance(data.get("instances"), list):
        cfg.instances = [_load_instance(row) for row in data["instances"] if isinstance(row, dict)]
    if isinstance(data.get("fleet_hosts"), list):
        cfg.fleet_hosts = [_load_fleet_host(row) for row in data["fleet_hosts"] if isinstance(row, dict)]
    if isinstance(data.get("fleet_servers"), list):
        cfg.fleet_servers = [_load_fleet_server(row) for row in data["fleet_servers"] if isinstance(row, dict)]
    cfg.active_fleet_host_id = str(data.get("active_fleet_host_id", cfg.active_fleet_host_id) or "")
    cfg.active_fleet_server_id = str(data.get("active_fleet_server_id", cfg.active_fleet_server_id) or "")
    return cfg.resolve()


def save_config(cfg: AppConfig, path: Path | None = None) -> Path:
    path = path or config_path()
    cfg.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return path


def platform_summary() -> dict[str, str]:
    return {
        "os": platform.system(),
        "release": platform.release(),
        "python": platform.python_version(),
        "machine": platform.machine(),
    }
