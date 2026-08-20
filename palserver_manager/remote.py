from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import threading
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import requests

from .config import AppConfig, ConnectionConfig, FleetHostConfig, FleetServerRef, save_config


class RemoteError(RuntimeError):
    pass


@dataclass
class SSHTunnel:
    host: str
    user: str
    port: int
    local_port: int
    remote_port: int
    key_file: str = ""
    process: subprocess.Popen | None = None

    def start(self) -> None:
        ssh = shutil.which("ssh")
        if not ssh:
            raise FileNotFoundError("OpenSSH client 'ssh' was not found. Install OpenSSH or use Direct HTTPS mode.")
        target = f"{self.user}@{self.host}" if self.user else self.host
        cmd = [
            ssh, "-N", "-T",
            "-p", str(self.port),
            "-L", f"127.0.0.1:{self.local_port}:127.0.0.1:{self.remote_port}",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
        ]
        if self.key_file:
            cmd += ["-i", self.key_file]
        cmd += [target]
        kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE, "text": True}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        self.process = subprocess.Popen(cmd, **kwargs)
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.process.poll() is not None:
                error = self.process.stderr.read() if self.process.stderr else ""
                raise RemoteError(error.strip() or "SSH tunnel exited unexpectedly")
            sock = socket.socket()
            sock.settimeout(0.2)
            try:
                if sock.connect_ex(("127.0.0.1", self.local_port)) == 0:
                    return
            finally:
                sock.close()
            time.sleep(0.2)
        self.stop()
        raise RemoteError("Timed out waiting for the SSH tunnel")

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None


class RemoteManager:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.tunnel: SSHTunnel | None = None
        connection = cfg.connection
        if connection.mode == "ssh":
            self.tunnel = SSHTunnel(
                host=connection.ssh_host,
                user=connection.ssh_user,
                port=connection.ssh_port,
                local_port=connection.ssh_local_port,
                remote_port=connection.ssh_remote_agent_port,
                key_file=connection.ssh_key_file,
            )
            self.tunnel.start()
            self.base = f"http://127.0.0.1:{connection.ssh_local_port}/v1"
            self.verify = True
        else:
            self.base = connection.remote_url.rstrip("/") + "/v1"
            self.verify = connection.ca_bundle or connection.verify_tls
        token = connection.remote_token
        self.headers = {"X-PalManager-Token": token, "Accept": "application/json"}
        self.instance_id = str(cfg.active_instance_id or "default")

    def close(self) -> None:
        if self.tunnel:
            self.tunnel.stop()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        # Use an independent HTTP request per worker.  The SSH tunnel is safe
        # to share, while avoiding one shared requests.Session lets telemetry
        # and a user-initiated page load proceed concurrently without UI
        # stalls or session-state races.
        timeout = kwargs.pop("timeout", 60)
        headers = dict(self.headers)
        if self.instance_id:
            headers["X-PalManager-Instance"] = self.instance_id
        extra_headers = kwargs.pop("headers", None) or {}
        headers.update(extra_headers)
        response = requests.request(method, self.base + path, timeout=timeout, verify=self.verify, headers=headers, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise RemoteError(f"Remote manager error {response.status_code}: {detail}")
        if not response.content:
            return {"ok": True}
        return response.json()

    def _request_bytes(self, method: str, path: str, **kwargs: Any) -> tuple[bytes, dict[str, str]]:
        timeout = kwargs.pop("timeout", 60)
        headers = dict(self.headers)
        if self.instance_id:
            headers["X-PalManager-Instance"] = self.instance_id
        extra_headers = kwargs.pop("headers", None) or {}
        headers.update(extra_headers)
        response = requests.request(method, self.base + path, timeout=timeout, verify=self.verify, headers=headers, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise RemoteError(f"Remote manager error {response.status_code}: {detail}")
        return response.content, dict(response.headers)

    def instances(self, include_status: bool = True) -> list[dict]:
        try:
            return self._request("GET", "/instances", timeout=20)
        except RemoteError as exc:
            if " 404:" not in str(exc):
                raise
            return [{"id": "default", "name": "Primary Server", "enabled": True, "active": True}]

    def select_instance(self, instance_id: str) -> dict:
        self.instance_id = str(instance_id or "default")
        self.cfg.active_instance_id = self.instance_id
        return {"id": self.instance_id}

    def create_instance(self, payload: dict) -> dict:
        return self._request("POST", "/instances", json=payload, timeout=30)

    def update_instance(self, instance_id: str, payload: dict) -> dict:
        return self._request("PUT", f"/instances/{instance_id}", json=payload, timeout=30)

    def delete_instance(self, instance_id: str) -> dict:
        result = self._request("DELETE", f"/instances/{instance_id}", timeout=30)
        if self.instance_id == str(instance_id):
            self.instance_id = str(result.get("active_instance_id") or "default")
            self.cfg.active_instance_id = self.instance_id
        return result

    def server_config(self) -> dict:
        return self._request("GET", "/server-config")

    def update_server_config(self, payload: dict) -> dict:
        return self._request("PUT", "/server-config", json=payload)

    def status(self, include_update: bool = False) -> dict:
        return self._request("GET", "/status", params={"include_update": str(include_update).lower()}, timeout=15)

    def overview(self) -> dict:
        try:
            return self._request("GET", "/overview", timeout=20)
        except RemoteError as exc:
            if " 404:" not in str(exc):
                raise
            # Compatibility with older agents. These calls still run in the
            # GUI worker thread, so the UI remains responsive during upgrade.
            status = self.status(False)
            health = self.health()
            try:
                players = self.players()
            except Exception:
                players = []
            try:
                backups = self.backup_list()
            except Exception:
                backups = []
            try:
                scheduler = self.scheduler_config()
            except Exception:
                scheduler = {"backup": {}, "updates": {}}
            try:
                logs = self.logs_tail(120)
            except Exception:
                logs = []
            return {
                "status": status, "health": health, "players": players,
                "backups": backups, "scheduler": scheduler, "logs": logs,
                "bans": self.banned_players(),
            }

    def watchdog_snapshot(self) -> dict:
        try:
            return self._request("GET", "/watchdog", timeout=15)
        except RemoteError as exc:
            if " 404:" not in str(exc):
                raise
            return {
                "status": self.status(False),
                "health": self.health(),
                "logs": self.logs_tail(120),
            }

    def health(self) -> dict:
        return self._request("GET", "/health", timeout=15)

    def settings(self, query: str = "") -> list[dict]:
        return self._request("GET", "/settings", params={"q": query})

    def set_setting(self, key: str, value: Any) -> dict:
        return self._request("PUT", f"/settings/{key}", json={"value": value})

    def compare_defaults(self) -> list[dict]:
        return self._request("GET", "/settings/compare-defaults")

    def reset_defaults(self, keys: list[str] | None = None) -> dict:
        return self._request("POST", "/settings/reset-defaults", json={"keys": keys})

    def profiles_list(self) -> dict:
        return self._request("GET", "/profiles")

    def profile_apply(self, name: str) -> dict:
        return self._request("POST", "/profiles/apply", json={"name": name})

    def service_action(self, action: str) -> dict:
        return self._request("POST", f"/service/{action}")

    def update_check(self) -> dict:
        return self._request("GET", "/update")

    def update_server(self, backup: bool = True, restart: bool = True) -> dict:
        return self._request("POST", "/update", json={"backup": backup, "restart": restart})

    def backup_create(self, label: str = "manual") -> dict:
        return self._request("POST", "/backups", json={"label": label})

    def backup_list(self) -> list[dict]:
        return self._request("GET", "/backups")

    def backup_restore(self, name: str) -> dict:
        return self._request("POST", f"/backups/{name}/restore")

    def backup_delete(self, name: str) -> dict:
        return self._request("DELETE", f"/backups/{name}")

    def logs_tail(self, lines: int = 100, errors_only: bool = False) -> list[str]:
        return self._request("GET", "/logs", params={"lines": lines, "errors_only": str(errors_only).lower()}, timeout=15)

    def crash_summary(self) -> dict:
        return self._request("GET", "/logs/crashes")

    def players(self) -> list[dict]:
        return self._request("GET", "/players", timeout=15)

    def banned_players(self) -> list[dict]:
        try:
            return self._request("GET", "/bans", timeout=15)
        except RemoteError as exc:
            if " 404:" in str(exc):
                return []
            raise

    def announce(self, message: str) -> dict:
        return self._request("POST", "/announce", json={"message": message})

    def player_action(self, action: str, user_id: str, message: str = "") -> dict:
        return self._request("POST", f"/players/{action}", json={"userid": user_id, "message": message})

    def save_world(self) -> dict:
        return self._request("POST", "/world/save")

    def graceful_shutdown(self, waittime: int = 30, message: str = "Server shutting down") -> dict:
        return self._request("POST", "/world/shutdown", json={"waittime": waittime, "message": message})


    def world_list(self) -> list[dict]:
        return self._request("GET", "/worlds")

    def world_archive(self, guid: str) -> dict:
        return self._request("POST", f"/worlds/{guid}/archive")

    def world_delete(self, guid: str) -> dict:
        return self._request("DELETE", f"/worlds/{guid}")

    def world_new(self) -> dict:
        return self._request("POST", "/worlds/new")

    def network_diagnostics(self, include_public_ip: bool = True) -> dict:
        return self._request("GET", "/network", params={"include_public_ip": str(include_public_ip).lower()}, timeout=20)

    def scheduler_config(self) -> dict:
        return self._request("GET", "/scheduler", timeout=15)

    def scheduler_update(self, payload: dict) -> dict:
        return self._request("PUT", "/scheduler", json=payload)

    def diagnostics(self) -> dict:
        return self._request("GET", "/diagnostics", timeout=20)

    def _mod_request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Call a mod API endpoint with a useful compatibility error.

        The desktop UI can be newer than an already-provisioned node.  Agents
        prior to 0.6.0 legitimately return HTTP 404 for every /mods route.
        Translate that into an actionable message instead of exposing a raw
        FastAPI 404 to the administrator.
        """
        try:
            return self._request(method, path, **kwargs)
        except RemoteError as exc:
            if " 404:" not in str(exc):
                raise
            version = "unknown"
            hostname = self.cfg.connection.ssh_host or "selected node"
            try:
                info = self.host_info() or {}
                version = str(info.get("version") or info.get("agent_version") or "unknown")
                hostname = str(info.get("hostname") or hostname)
            except Exception:
                pass
            raise RemoteError(
                f"Mod management API is not available on {hostname} (agent {version}). "
                "Update this node's PalServer Manager agent to version 0.6.0 or newer from Remote Hosts, then refresh Mods."
            ) from exc

    def mods_status(self) -> dict:
        return self._mod_request("GET", "/mods", timeout=20)

    def mod_runtime_enable(self, source_url: str = "") -> dict:
        return self._mod_request("POST", "/mods/runtime/enable", json={"source_url": source_url}, timeout=600)

    def mod_runtime_disable(self) -> dict:
        return self._mod_request("POST", "/mods/runtime/disable", timeout=180)

    def mod_validate(self) -> dict:
        return self._mod_request("POST", "/mods/validate", timeout=30)

    def mod_log(self, lines: int = 160) -> list[str]:
        return self._mod_request("GET", "/mods/log", params={"lines": int(lines)}, timeout=20)

    def mod_install_package(self, archive_bytes: bytes, filename: str = "mod.zip") -> dict:
        return self._mod_request(
            "POST", "/mods/packages", params={"filename": filename}, data=archive_bytes,
            headers={"Content-Type": "application/zip"}, timeout=600,
        )

    def mod_set_enabled(self, mod_id: str, enabled: bool) -> dict:
        action = "enable" if enabled else "disable"
        return self._mod_request("POST", f"/mods/{mod_id}/{action}", timeout=180)

    def mod_remove(self, mod_id: str) -> dict:
        return self._mod_request("DELETE", f"/mods/{mod_id}", timeout=180)

    def mod_client_pack(self) -> dict:
        data, headers = self._request_bytes("GET", "/mods/client-pack", timeout=120)
        disposition = headers.get("Content-Disposition", "")
        name = "palserver-client-modpack.zip"
        if "filename=" in disposition:
            name = disposition.split("filename=", 1)[1].strip().strip('"') or name
        return {"name": name, "bytes": data, "size": len(data)}

    def host_info(self) -> dict:
        return self._request("GET", "/host/info", timeout=15)

    def host_discover(self) -> list[dict]:
        return self._request("GET", "/host/discover", timeout=45)

    def host_update_os(self) -> dict:
        return self._request("POST", "/host/update-os", timeout=3700)

    def host_adopt(self, payload: dict) -> dict:
        return self._request("POST", "/host/adopt", json=payload, timeout=60)

    def host_install_palworld(self, payload: dict) -> dict:
        return self._request("POST", "/host/install-palworld", json=payload, timeout=3900)

    def host_install_palworld_start(self, payload: dict) -> dict:
        return self._request("POST", "/host/install-palworld/start", json=payload, timeout=30)

    def host_install_palworld_job(self, job_id: str, offset: int = 0) -> dict:
        return self._request("GET", f"/host/install-palworld/jobs/{job_id}", params={"offset": int(offset)}, timeout=20)

    def host_uninstall_palworld(self, instance_id: str) -> dict:
        return self._request("POST", "/host/uninstall-palworld", json={"id": str(instance_id)}, timeout=1800)



class FleetManager:
    """Route the desktop GUI to server instances hosted by many agents.

    The global server ID (001, 002, ...) belongs to the manager. Each agent can
    keep its own local instance ID; the fleet catalog maps the two. Agent ports
    stay on loopback and are reached through independent SSH tunnels.
    """

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg.resolve()
        self._lock = threading.RLock()
        self._managers: dict[str, RemoteManager] = {}
        if not self.cfg.fleet_servers:
            raise RemoteError("No fleet servers are configured")

    @property
    def active_host_id(self) -> str:
        if self.cfg.active_fleet_host_id:
            return str(self.cfg.active_fleet_host_id)
        ref = self._active_ref()
        return str(ref.host_id)

    @active_host_id.setter
    def active_host_id(self, value: str) -> None:
        self.cfg.fleet_host(str(value))
        self.cfg.active_fleet_host_id = str(value)

    @property
    def instance_id(self) -> str:
        return str(self.cfg.active_fleet_server_id or self.cfg.fleet_servers[0].id)

    @instance_id.setter
    def instance_id(self, value: str) -> None:
        ref = self.cfg.fleet_server(str(value))
        self.cfg.active_fleet_server_id = ref.id
        self.cfg.active_fleet_host_id = ref.host_id

    def close(self) -> None:
        with self._lock:
            for manager in self._managers.values():
                try:
                    manager.close()
                except Exception:
                    pass
            self._managers.clear()

    def _host_cfg(self, host: FleetHostConfig) -> AppConfig:
        view = deepcopy(self.cfg)
        view.fleet_hosts = []
        view.fleet_servers = []
        view.active_fleet_host_id = ""
        view.active_fleet_server_id = ""
        view.connection = ConnectionConfig(
            mode=host.mode,
            remote_url=host.remote_url or "https://server.example.com:8765",
            remote_token=host.agent_token,
            verify_tls=host.verify_tls,
            ssh_host=host.ssh_host,
            ssh_port=int(host.ssh_port),
            ssh_user=host.ssh_user,
            ssh_key_file=host.ssh_key_file,
            ssh_local_port=int(host.local_tunnel_port),
            ssh_remote_agent_port=int(host.remote_agent_port),
        )
        return view

    def _manager_for_host(self, host_id: str) -> RemoteManager:
        with self._lock:
            if host_id in self._managers:
                return self._managers[host_id]
            host = self.cfg.fleet_host(host_id)
            manager = RemoteManager(self._host_cfg(host))
            self._managers[host_id] = manager
            return manager

    def _active_ref(self) -> FleetServerRef:
        return self.cfg.fleet_server(self.cfg.active_fleet_server_id)

    def _active_manager(self) -> RemoteManager:
        ref = self._active_ref()
        manager = self._manager_for_host(ref.host_id)
        manager.instance_id = ref.remote_instance_id
        return manager

    def __getattr__(self, name: str):
        return getattr(self._active_manager(), name)

    def hosts(self) -> list[dict[str, Any]]:
        rows = []
        for host in self.cfg.fleet_hosts:
            rows.append({
                "id": host.id, "name": host.name, "mode": host.mode,
                "ssh_host": host.ssh_host, "ssh_port": host.ssh_port,
                "ssh_user": host.ssh_user, "agent_port": host.remote_agent_port,
                "enabled": host.enabled, "os_type": host.os_type,
            })
        return rows

    def instances(self, include_status: bool = True) -> list[dict]:
        host_rows: dict[str, dict[str, dict]] = {}
        if include_status:
            for host in self.cfg.fleet_hosts:
                try:
                    remote_rows = self._manager_for_host(host.id).instances(False)
                    host_rows[host.id] = {str(row.get("id")): row for row in remote_rows}
                except Exception as exc:
                    host_rows[host.id] = {"__error__": {"error": str(exc)}}
        result = []
        for ref in self.cfg.fleet_servers:
            host = self.cfg.fleet_host(ref.host_id)
            remote = host_rows.get(ref.host_id, {}).get(ref.remote_instance_id, {})
            error = host_rows.get(ref.host_id, {}).get("__error__", {}).get("error")
            remote_name = str(remote.get("name") or "").strip()
            if remote_name and ref.name in {"Primary Server", f"Server {ref.id}"}:
                ref.name = remote_name
            result.append({
                "id": ref.id,
                "name": ref.name,
                "enabled": ref.enabled,
                "host_id": ref.host_id,
                "host_name": host.name,
                "host_address": host.ssh_host or host.remote_url,
                "remote_instance_id": ref.remote_instance_id,
                "state": "unreachable" if error else remote.get("state", "unknown"),
                "pid": remote.get("pid", 0),
                "service_name": remote.get("service_name", "-"),
                "install_dir": remote.get("install_dir", "-"),
                "game_port": remote.get("game_port", "-"),
                "rest_api_port": remote.get("rest_api_port", "-"),
                "error": error or "",
            })
        try:
            save_config(self.cfg)
        except Exception:
            pass
        return result

    def select_instance(self, instance_id: str) -> dict:
        ref = self.cfg.fleet_server(str(instance_id))
        self.cfg.active_fleet_host_id = ref.host_id
        self.cfg.active_fleet_server_id = ref.id
        manager = self._manager_for_host(ref.host_id)
        manager.select_instance(ref.remote_instance_id)
        save_config(self.cfg)
        return {"id": ref.id, "remote_instance_id": ref.remote_instance_id, "host_id": ref.host_id}

    def select_host(self, host_id: str) -> dict:
        """Select a fleet node and, when available, a server hosted by it.

        Host selection is a desktop-side context operation. It does not change
        other agents and it permits selecting an empty provisioned node.
        """
        host = self.cfg.fleet_host(str(host_id))
        self.cfg.active_fleet_host_id = host.id
        servers = [row for row in self.cfg.fleet_servers if row.host_id == host.id and row.enabled]
        selected = next((row for row in servers if row.id == self.cfg.active_fleet_server_id), None)
        if selected is None and servers:
            selected = servers[0]
        if selected is not None:
            self.cfg.active_fleet_server_id = selected.id
            manager = self._manager_for_host(host.id)
            manager.select_instance(selected.remote_instance_id)
        save_config(self.cfg)
        return {
            "host_id": host.id,
            "server_id": selected.id if selected is not None else "",
            "remote_instance_id": selected.remote_instance_id if selected is not None else "",
        }

    def rename_instance(self, instance_id: str, name: str) -> dict:
        ref = self.cfg.fleet_server(instance_id)
        clean = str(name or "").strip()
        if not clean:
            raise ValueError("Server name cannot be blank")
        manager = self._manager_for_host(ref.host_id)
        updated = manager.update_instance(ref.remote_instance_id, {"name": clean})
        try:
            manager.set_setting("ServerName", clean)
        except Exception:
            pass
        ref.name = clean
        save_config(self.cfg)
        return {**updated, "id": ref.id, "name": clean, "host_id": ref.host_id}

    def create_instance(self, payload: dict) -> dict:
        # Create another game instance on the same host as the active server.
        active = self._active_ref()
        global_id = self.cfg.next_fleet_server_id()
        remote_payload = dict(payload or {})
        remote_payload["id"] = global_id
        remote_payload.setdefault("name", f"Server {global_id}")
        manager = self._manager_for_host(active.host_id)
        created = manager.create_instance(remote_payload)
        ref = FleetServerRef(
            id=global_id,
            name=str(created.get("name") or remote_payload["name"]),
            host_id=active.host_id,
            remote_instance_id=str(created.get("id") or global_id),
        )
        self.cfg.fleet_servers.append(ref)
        save_config(self.cfg)
        return {**created, "id": ref.id, "host_id": ref.host_id, "remote_instance_id": ref.remote_instance_id}

    def update_instance(self, instance_id: str, payload: dict) -> dict:
        ref = self.cfg.fleet_server(instance_id)
        manager = self._manager_for_host(ref.host_id)
        updated = manager.update_instance(ref.remote_instance_id, payload)
        if "name" in payload and str(payload.get("name") or "").strip():
            ref.name = str(payload["name"]).strip()
        if "enabled" in payload:
            ref.enabled = bool(payload["enabled"])
        save_config(self.cfg)
        return {**updated, "id": ref.id, "host_id": ref.host_id}

    def delete_instance(self, instance_id: str) -> dict:
        ref = self.cfg.fleet_server(instance_id)
        if len(self.cfg.fleet_servers) <= 1:
            raise ValueError("At least one managed server must remain")
        manager = self._manager_for_host(ref.host_id)
        manager.delete_instance(ref.remote_instance_id)
        self.cfg.fleet_servers = [row for row in self.cfg.fleet_servers if row.id != ref.id]
        if self.cfg.active_fleet_server_id == ref.id:
            self.cfg.active_fleet_server_id = self.cfg.fleet_servers[0].id
            self.cfg.active_fleet_host_id = self.cfg.fleet_servers[0].host_id
        save_config(self.cfg)
        return {"deleted": ref.id, "active_instance_id": self.cfg.active_fleet_server_id}

    def uninstall_instance(self, instance_id: str) -> dict:
        """Uninstall a Palworld server from its agent host and unlink it."""
        ref = self.cfg.fleet_server(instance_id)
        if len(self.cfg.fleet_servers) <= 1:
            raise ValueError("Add or link another managed server before uninstalling the last server from this manager")
        manager = self._manager_for_host(ref.host_id)
        result = manager.host_uninstall_palworld(ref.remote_instance_id)
        self.cfg.fleet_servers = [row for row in self.cfg.fleet_servers if row.id != ref.id]
        if self.cfg.active_fleet_server_id == ref.id:
            self.cfg.active_fleet_server_id = self.cfg.fleet_servers[0].id
            self.cfg.active_fleet_host_id = self.cfg.fleet_servers[0].host_id
        save_config(self.cfg)
        return {**result, "deleted": ref.id, "active_instance_id": self.cfg.active_fleet_server_id, "host_id": ref.host_id}

    def remove_host(self, host_id: str) -> dict:
        """Remove an unlinked agent host from the desktop fleet catalog."""
        host_id = str(host_id)
        linked = [row for row in self.cfg.fleet_servers if row.host_id == host_id]
        if linked:
            names = ", ".join(f"{row.id} {row.name}" for row in linked)
            raise ValueError(f"This host still has managed servers linked to it: {names}. Remove or uninstall those servers first.")
        self.reset_host_connection(host_id)
        host = self.cfg.fleet_host(host_id)
        self.cfg.fleet_hosts = [row for row in self.cfg.fleet_hosts if row.id != host_id]
        if self.cfg.active_fleet_host_id == host_id:
            self.cfg.active_fleet_host_id = self.cfg.fleet_hosts[0].id if self.cfg.fleet_hosts else ""
        save_config(self.cfg)
        return {"removed_host": host.id, "name": host.name}

    def register_host(self, host: FleetHostConfig) -> FleetHostConfig:
        if any(row.id == host.id for row in self.cfg.fleet_hosts):
            raise ValueError(f"Host ID already exists: {host.id}")
        self.cfg.fleet_hosts.append(host)
        if not self.cfg.active_fleet_host_id:
            self.cfg.active_fleet_host_id = host.id
        save_config(self.cfg)
        return host

    def reset_host_connection(self, host_id: str) -> None:
        """Drop a cached remote manager/tunnel so the next call reconnects cleanly."""
        with self._lock:
            manager = self._managers.pop(str(host_id), None)
            if manager is not None:
                try:
                    manager.close()
                except Exception:
                    pass

    def wait_for_host_info(
        self,
        host_id: str,
        attempts: int = 8,
        delay: float = 1.0,
        on_retry=None,
    ) -> dict:
        """Reconnect to a host agent after an agent/service restart.

        Updating the agent intentionally restarts its systemd service.  On Windows,
        an HTTP request or the SSH forward that was alive during that restart can
        surface WSAECONNABORTED (10053) even though the upgrade completed.  Always
        discard the old tunnel, establish a fresh one, and retry the harmless
        host-info GET before deciding the agent is unreachable.
        """
        attempts = max(1, int(attempts))
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            self.reset_host_connection(host_id)
            try:
                return self.remote_for_host(host_id).host_info()
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                if on_retry is not None:
                    try:
                        on_retry(attempt, attempts, exc)
                    except Exception:
                        pass
                time.sleep(max(0.0, float(delay)) * min(attempt, 3))
        raise RemoteError(
            f"Agent was restarted but could not be reached after {attempts} reconnect attempt(s): {last_error}"
        )

    def link_remote_instance(self, host_id: str, remote_row: dict, preferred_name: str = "") -> dict:
        remote_id = str(remote_row.get("id") or "default")
        for ref in self.cfg.fleet_servers:
            if ref.host_id == host_id and ref.remote_instance_id == remote_id:
                return {"id": ref.id, "name": ref.name, "host_id": ref.host_id, "remote_instance_id": remote_id}
        global_id = self.cfg.next_fleet_server_id()
        name = str(preferred_name or remote_row.get("name") or f"Server {global_id}").strip()
        ref = FleetServerRef(id=global_id, name=name, host_id=host_id, remote_instance_id=remote_id)
        self.cfg.fleet_servers.append(ref)
        save_config(self.cfg)
        return {"id": ref.id, "name": ref.name, "host_id": ref.host_id, "remote_instance_id": remote_id}

    def remote_for_host(self, host_id: str) -> RemoteManager:
        return self._manager_for_host(host_id)


def manager_from_config(cfg: AppConfig):
    if cfg.connection.mode == "local":
        from .manager import LocalManager
        return LocalManager(cfg)
    if cfg.fleet_hosts and cfg.fleet_servers:
        return FleetManager(cfg)
    return RemoteManager(cfg)
