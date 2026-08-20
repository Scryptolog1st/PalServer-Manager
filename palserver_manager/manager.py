from __future__ import annotations

import os
import platform
import socket
import time
import threading
from pathlib import Path
from typing import Any

import psutil

from .backup import BackupManager
from .bans import BanRegistry
from . import __version__
from .config import AppConfig, load_config, save_config
from .health import build_health
from .logs import LogManager
from .mods import ModManager
from .network import diagnose, primary_ip, udp_listening
from .pal_api import PalworldAPI
from .player_identity import platform_from_user_id
from .profiles import ProfileManager
from .service import service_controller
from .settings import IniManager, SECRET_KEYS, unquote
from .steam import SteamManager
from .worlds import WorldManager


def _resolve_palserver_process(service_pid: int, install_dir: str = ""):
    """Return the real PalServer game process instead of a shell wrapper.

    systemd commonly exposes ``PalServer.sh`` as MainPID, while the heavy
    ``PalServer-Linux-Shipping`` process may be a descendant or may have been
    re-parented.  Look at the service tree first, then fall back to a tightly
    filtered process-table search.  The highest scoring / largest-RSS game
    process wins.
    """
    candidates = []
    seen = set()

    def add_process(proc):
        try:
            if proc.pid not in seen:
                seen.add(proc.pid)
                candidates.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if service_pid and psutil.pid_exists(service_pid):
        try:
            root = psutil.Process(service_pid)
            add_process(root)
            for child in root.children(recursive=True):
                add_process(child)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # PalServer.sh can spawn/re-parent the shipping binary.  A global fallback
    # keeps watchdog metrics correct without accidentally selecting unrelated
    # processes: only PalServer-named executables/cmdlines are admitted.
    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline", "exe"]):
            try:
                name = (proc.info.get("name") or "").lower()
                cmdline = " ".join(proc.info.get("cmdline") or []).lower()
                exe = (proc.info.get("exe") or "").lower()
                text = f"{name} {cmdline} {exe}"
                if (
                    "palserver-linux-shipping" in text
                    or "palserver-linux" in text
                    or "palserver.exe" in text
                ):
                    add_process(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
    except (psutil.AccessDenied, OSError):
        pass

    if not candidates:
        return None

    install_hint = str(install_dir or "").lower().rstrip("/\\")
    ranked = []
    for proc in candidates:
        try:
            name = (proc.name() or "").lower()
            cmdline = " ".join(proc.cmdline() or []).lower()
            try:
                exe = (proc.exe() or "").lower()
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                exe = ""
            text = f"{name} {cmdline} {exe}"
            score = 0
            if "palserver-linux-shipping" in text:
                score += 1000
            elif "palserver.exe" in text:
                score += 1000
            elif "palserver-linux" in text:
                score += 800
            elif "palserver" in text:
                score += 250
            if install_hint and install_hint in text:
                score += 150
            if proc.pid == service_pid:
                score += 20
            if "palserver.sh" in text or name in {"sh", "bash", "dash"}:
                score -= 500
            rss = proc.memory_info().rss
            ranked.append((score, rss, proc))
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue

    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return ranked[0][2]



class LocalManager:
    def __init__(self, cfg: AppConfig | None = None, instance_id: str | None = None):
        self.root_cfg = (cfg or load_config()).resolve()
        self.operation_lock = threading.RLock()
        self.profiles = ProfileManager()
        self.instance_id = ""
        self.instance_name = ""
        self._bind_instance(instance_id or self.root_cfg.active_instance_id)

    def _bind_instance(self, instance_id: str) -> None:
        instance = self.root_cfg.instance(instance_id)
        self.instance_id = instance.id
        self.instance_name = instance.name
        self.cfg = self.root_cfg.instance_view(instance.id)
        self.service = service_controller(self.cfg, self.instance_id)
        self.ini = IniManager(self.cfg.server.config_path, Path(self.cfg.server.backup_dir) / "config")
        self.steam = SteamManager(self.cfg)
        self.backups = BackupManager(self.cfg)
        self.logs = LogManager(self.cfg)
        self.api = PalworldAPI(self.cfg)
        self.worlds = WorldManager(self.cfg)
        self.bans = BanRegistry(Path(self.cfg.server.backup_dir) / ".palserver-manager" / "bans.json")
        self.mods = ModManager(self.cfg, self.instance_id)

    def _save_root_config(self) -> None:
        save_config(self.root_cfg)

    def select_instance(self, instance_id: str) -> dict[str, Any]:
        self._bind_instance(instance_id)
        self.root_cfg.active_instance_id = self.instance_id
        self._save_root_config()
        return self.current_instance()

    def current_instance(self) -> dict[str, Any]:
        instance = self.root_cfg.instance(self.instance_id)
        return {
            "id": instance.id,
            "name": instance.name,
            "enabled": instance.enabled,
            "service_name": instance.server.service_name,
            "install_dir": instance.server.install_dir,
            "game_port": instance.server.game_port,
            "rest_api_port": instance.server.rest_api_port,
        }

    def instances(self, include_status: bool = True) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for instance in self.root_cfg.instances:
            row = {
                "id": instance.id,
                "name": instance.name,
                "enabled": instance.enabled,
                "service_name": instance.server.service_name,
                "install_dir": instance.server.install_dir,
                "game_port": instance.server.game_port,
                "rest_api_port": instance.server.rest_api_port,
                "active": instance.id == self.instance_id,
            }
            if include_status:
                try:
                    view = self.root_cfg.instance_view(instance.id)
                    state = service_controller(view, instance.id).status()
                    row["state"] = state.get("state", "unknown")
                    row["pid"] = state.get("pid", 0)
                except Exception as exc:
                    row["state"] = "unknown"
                    row["error"] = str(exc)
            rows.append(row)
        return rows

    def create_instance(self, payload: dict[str, Any]) -> dict[str, Any]:
        instance = self.root_cfg.add_instance(payload)
        save_config(self.root_cfg)
        return {
            "id": instance.id, "name": instance.name, "enabled": instance.enabled,
            "service_name": instance.server.service_name, "install_dir": instance.server.install_dir,
            "game_port": instance.server.game_port, "rest_api_port": instance.server.rest_api_port,
        }

    def update_instance(self, instance_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        instance = self.root_cfg.instance(instance_id)
        if "name" in payload:
            instance.name = str(payload.get("name") or instance.name).strip() or instance.name
        if "enabled" in payload:
            instance.enabled = bool(payload.get("enabled"))
        server_values = payload.get("server")
        if isinstance(server_values, dict):
            target = self if instance.id == self.instance_id else LocalManager(self.root_cfg, instance.id)
            target.update_server_config(server_values)
        save_config(self.root_cfg)
        if instance.id == self.instance_id:
            self.instance_name = instance.name
        return {
            "id": instance.id, "name": instance.name, "enabled": instance.enabled,
            "service_name": instance.server.service_name, "install_dir": instance.server.install_dir,
            "game_port": instance.server.game_port, "rest_api_port": instance.server.rest_api_port,
        }

    def rename_instance(self, instance_id: str, name: str) -> dict[str, Any]:
        clean = str(name or "").strip()
        if not clean:
            raise ValueError("Server name cannot be blank")
        result = self.update_instance(instance_id, {"name": clean})
        target = self if str(instance_id) == self.instance_id else LocalManager(self.root_cfg, str(instance_id))
        try:
            target.set_setting("ServerName", clean)
        except Exception:
            # The manager display name is still updated even when the game
            # config has not been generated yet. Server Setup can finish it.
            pass
        return result

    def delete_instance(self, instance_id: str) -> dict[str, Any]:
        instance = self.root_cfg.instance(instance_id)
        view = self.root_cfg.instance_view(instance.id)
        state = str(service_controller(view, instance.id).status().get("state", "")).lower()
        if state in {"active", "running"}:
            raise RuntimeError("Stop this server instance before removing it from PalServer Manager")
        removed = self.root_cfg.remove_instance(instance.id)
        save_config(self.root_cfg)
        if self.instance_id == removed.id:
            self._bind_instance(self.root_cfg.active_instance_id)
        return {"deleted": removed.id, "active_instance_id": self.instance_id}

    def server_config(self) -> dict[str, Any]:
        server = self.cfg.server
        return {
            "install_dir": server.install_dir,
            "steamcmd_path": server.steamcmd_path,
            "steam_user": server.steam_user,
            "service_name": server.service_name,
            "launch_args": list(server.launch_args),
            "config_path": server.config_path,
            "save_dir": server.save_dir,
            "backup_dir": server.backup_dir,
            "log_file": server.log_file,
            "rest_api_host": server.rest_api_host,
            "rest_api_port": server.rest_api_port,
            "rest_api_username": server.rest_api_username,
            "admin_password_set": bool(server.admin_password),
            "game_port": server.game_port,
        }

    def update_server_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "install_dir", "steamcmd_path", "steam_user", "service_name", "launch_args",
            "config_path", "save_dir", "backup_dir", "log_file", "rest_api_host",
            "rest_api_port", "rest_api_username", "admin_password", "game_port",
        }
        server = self.cfg.server
        old_values = {key: getattr(server, key) for key in allowed if hasattr(server, key)}
        old_install = server.install_dir
        try:
            for key, value in payload.items():
                if key in allowed and value is not None:
                    setattr(server, key, value)
            if server.install_dir != old_install:
                for attr in ("config_path", "save_dir", "backup_dir", "log_file"):
                    if attr not in payload:
                        setattr(server, attr, "")
            server.resolve()
            for other in self.root_cfg.instances:
                if other.id == self.instance_id:
                    continue
                if int(other.server.game_port) == int(server.game_port):
                    raise ValueError(f"Game port {server.game_port} is already used by server instance '{other.name}'")
                if int(other.server.rest_api_port) == int(server.rest_api_port):
                    raise ValueError(f"REST API port {server.rest_api_port} is already used by server instance '{other.name}'")
                if server.service_name and other.server.service_name == server.service_name:
                    raise ValueError(f"Service name '{server.service_name}' is already used by server instance '{other.name}'")
                if server.config_path and other.server.config_path and Path(server.config_path) == Path(other.server.config_path):
                    raise ValueError(f"Config path is already used by server instance '{other.name}'")
                if server.save_dir and other.server.save_dir and Path(server.save_dir) == Path(other.server.save_dir):
                    raise ValueError(f"Save directory is already used by server instance '{other.name}'")
        except Exception:
            for key, value in old_values.items():
                setattr(server, key, value)
            raise
        self._save_root_config()

        # Optionally configure Palworld's built-in local REST API at the same time.
        sync_rest = bool(payload.get("sync_palworld_rest", False))
        if sync_rest and Path(server.config_path).exists():
            ini = IniManager(server.config_path, Path(server.backup_dir) / "config")
            changes: dict[str, Any] = {
                "RESTAPIEnabled": "True",
                "RESTAPIPort": int(server.rest_api_port),
            }
            if server.admin_password:
                changes["AdminPassword"] = server.admin_password
            ini.set_many(changes)

        # Rebuild helpers around any changed paths/service/API values.
        self.service = service_controller(self.cfg, self.instance_id)
        self.ini = IniManager(self.cfg.server.config_path, Path(self.cfg.server.backup_dir) / "config")
        self.steam = SteamManager(self.cfg)
        self.backups = BackupManager(self.cfg)
        self.logs = LogManager(self.cfg)
        self.api = PalworldAPI(self.cfg)
        self.worlds = WorldManager(self.cfg)
        self.bans = BanRegistry(Path(self.cfg.server.backup_dir) / ".palserver-manager" / "bans.json")
        self.mods = ModManager(self.cfg, self.instance_id)
        return self.server_config()

    def status(self, include_update: bool = False) -> dict[str, Any]:
        service = self.service.status()
        settings = {}
        if Path(self.cfg.server.config_path).exists():
            try:
                settings = self.ini.values(reveal_secrets=True)
            except Exception:
                settings = {}
        api_info: dict[str, Any] = {}
        metrics: dict[str, Any] = {}
        if settings.get("RESTAPIEnabled") == "True" and self.cfg.server.admin_password:
            try:
                api_info = self.api.info()
                metrics = self.api.metrics()
            except Exception:
                pass
        server_name = unquote(settings.get("ServerName", '"Unknown"')) if settings else "Unknown"
        max_players = settings.get("ServerPlayerMaxNum", "Unknown")
        game_port = int(self.cfg.server.game_port)
        advertised_port = int(settings.get("PublicPort", game_port) or game_port)
        build = self.steam.installed_build()
        update = self.steam.cached_update_status()
        if include_update:
            try:
                update = self.steam.update_status(force=True)
            except Exception as exc:
                update = {"installed": build, "latest": None, "state": "unknown", "error": str(exc)}
        service_pid = int(service.get("pid") or 0)
        process = {}
        p = _resolve_palserver_process(service_pid, self.cfg.server.install_dir)
        if p is not None:
            try:
                process = {
                    "pid": p.pid,
                    "service_pid": service_pid,
                    "name": p.name(),
                    "rss": p.memory_info().rss,
                    # A short blocking sample is intentional here. status() is
                    # called by the live watchdog once per second and this gives
                    # a real instantaneous CPU sample rather than psutil's first-
                    # call 0.0% behavior.
                    "cpu_percent": p.cpu_percent(interval=0.12),
                    "threads": p.num_threads(),
                }
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                process = {"pid": p.pid, "service_pid": service_pid}
        elif service_pid:
            process = {"pid": service_pid, "service_pid": service_pid}
        return {
            "instance_id": self.instance_id,
            "instance_name": self.instance_name,
            "server_name": api_info.get("servername", server_name),
            "game_version": api_info.get("version") or self.logs.game_version() or "Unavailable",
            "world_guid": api_info.get("worldguid", ""),
            "service": service,
            "game_port": game_port,
            "rest_api_port": int(self.cfg.server.rest_api_port),
            "advertised_port": advertised_port,
            "udp_listening": udp_listening(game_port),
            "lan_ip": primary_ip(),
            "max_players": metrics.get("maxplayernum", max_players),
            "current_players": metrics.get("currentplayernum", None),
            "server_fps": metrics.get("serverfps", None),
            "server_fps_average": metrics.get("serverfpsaverage", None),
            "server_uptime": metrics.get("uptime", None),
            "base_camps": metrics.get("basecampnum", None),
            "world_days": metrics.get("days", None),
            "pvp": settings.get("bIsPvP", "Unknown"),
            "hardcore": settings.get("bHardcore", "Unknown"),
            "pal_lost": settings.get("bPalLost", "Unknown"),
            "crossplay": settings.get("CrossplayPlatforms", "Unknown"),
            "password_set": bool(unquote(settings.get("ServerPassword", '""'))),
            "build": update,
            "process": process,
            "host": {
                "hostname": socket.gethostname(),
                "os": platform.system(),
                "os_release": platform.release(),
                "time": time.time(),
            },
        }

    def health(self) -> dict:
        metrics = None
        try:
            metrics = self.api.metrics() if self.api.available() else None
        except Exception:
            metrics = None
        return build_health(self.cfg, self.service.status(), metrics)

    def settings(self, query: str = "") -> list[dict]:
        return [row.__dict__ for row in self.ini.records(query)]

    def set_setting(self, key: str, value: Any) -> dict:
        return self.ini.set_value(key, value)

    def compare_defaults(self) -> list[dict]:
        default_path = Path(self.cfg.server.install_dir) / "DefaultPalWorldSettings.ini"
        if not default_path.exists():
            raise FileNotFoundError(default_path)
        return self.ini.compare_default(default_path)

    def reset_defaults(self, keys: list[str] | None = None) -> dict:
        default_path = Path(self.cfg.server.install_dir) / "DefaultPalWorldSettings.ini"
        if not default_path.exists():
            raise FileNotFoundError(default_path)
        return self.ini.reset_to_defaults(default_path, keys)

    def profiles_list(self) -> dict:
        return self.profiles.all()

    def profile_apply(self, name: str) -> dict:
        profiles = self.profiles.all()
        if name not in profiles:
            raise KeyError(name)
        changes = profiles[name]
        if not changes:
            return {"profile": name, "changes": [], "note": "Vanilla profile has no forced overrides; use Reset to Defaults for a full reset."}
        result = self.ini.set_many(changes)
        result["profile"] = name
        return result

    def service_action(self, action: str) -> dict:
        action = action.lower()
        with self.operation_lock:
            if action == "start":
                return self.service.start()
            if action == "stop":
                return self.service.stop()
            if action == "restart":
                return self.service.restart()
            raise ValueError(f"Unsupported service action: {action}")

    def update_check(self) -> dict:
        return self.steam.update_status(force=True)

    def update_server(self, backup: bool = True, restart: bool = True) -> dict:
        with self.operation_lock:
            before = self.service.status()
            was_running = str(before.get("state", "")).lower() in {"active", "running"}
            backup_result = None
            if backup and self.cfg.backup.backup_before_update:
                try:
                    if self.api.available():
                        self.api.save()
                except Exception:
                    pass
                backup_result = self.backups.create("pre-update")
            if was_running:
                try:
                    if self.api.available():
                        self.api.shutdown(10, "Server update starting in 10 seconds")
                        time.sleep(12)
                    else:
                        self.service.stop()
                except Exception:
                    self.service.stop()
            result = self.steam.update()
            mod_runtime = None
            try:
                mod_runtime = self.mods.repair_after_update()
            except Exception as exc:
                mod_runtime = {"health": "failed", "detail": str(exc)}
            if restart and was_running:
                self.service.start()
                if mod_runtime and (self.mods.status().get("runtime") or {}).get("enabled"):
                    time.sleep(1.0)
                    try:
                        mod_runtime = self.mods.validate_runtime()
                    except Exception as exc:
                        mod_runtime = {"health": "failed", "detail": str(exc)}
            return {"backup": backup_result, "update": result, "mod_runtime": mod_runtime, "service": self.service.status()}

    def _mod_change(self, label: str, fn):
        """Apply a mod/runtime mutation with backup + controlled restart."""
        with self.operation_lock:
            before = self.service.status()
            running = str(before.get("state", "")).lower() in {"active", "running"}
            try:
                if running and self.api.available():
                    self.api.save()
            except Exception:
                pass
            backup = self.backups.create(f"pre-mod-{label}")
            if running:
                self.service.stop()
            try:
                result = fn()
            except Exception:
                if running:
                    try:
                        self.service.start()
                    except Exception:
                        pass
                raise
            if running:
                self.service.start()
                time.sleep(1.0)
            validation = None
            if (self.mods.status().get("runtime") or {}).get("enabled"):
                try:
                    validation = self.mods.validate_runtime()
                except Exception as exc:
                    validation = {"health": "failed", "detail": str(exc)}
            return {"backup": backup, "result": result, "validation": validation, "service": self.service.status(), "was_running": running}

    def mods_status(self) -> dict:
        status = self.mods.status()
        status["manager_version"] = __version__
        try:
            status["validation"] = self.mods.validate_runtime()
        except Exception as exc:
            status["validation"] = {"health": "unknown", "detail": str(exc), "checks": {}}
        return status

    def _sync_client_mod_setting(self) -> None:
        """Permit modded clients when any enabled managed mod requires client files."""
        try:
            status = self.mods.status()
            required = any(bool(row.get("enabled")) and bool(row.get("client_required")) for row in status.get("mods", []))
            if required and Path(self.cfg.server.config_path).exists():
                self.ini.set_value("bAllowClientMod", "True")
        except Exception:
            # Package/runtime operations should not be rolled back just because
            # an older Palworld build does not expose this setting yet.
            pass

    def mod_runtime_enable(self, source_url: str = "") -> dict:
        result = self._mod_change("runtime-enable", lambda: self.mods.install_runtime(source_url=source_url))
        validation = result.get("validation") or {}
        service = result.get("service") or {}
        service_state = str(service.get("state") or "").lower()
        failed = str(validation.get("health") or "").lower() == "failed"
        if result.get("was_running") and service_state not in {"active", "running"}:
            failed = True
        if failed:
            # A community runtime can temporarily lag a Palworld update. Do not
            # leave an otherwise healthy production server in a crash loop.
            # Revert only the runtime launch integration; runtime files and the
            # safety backup remain available for troubleshooting/retry.
            try:
                self.service.stop()
            except Exception:
                pass
            rollback = self.mods.disable_runtime()
            if result.get("was_running"):
                try:
                    self.service.start()
                except Exception:
                    pass
            detail = str(validation.get("detail") or "UE4SS runtime validation failed")
            raise RuntimeError(
                f"Mod runtime did not validate successfully: {detail}. "
                "PalServer Manager automatically restored vanilla startup so the server is not left in a mod-runtime crash loop."
            )
        return result

    def mod_runtime_disable(self) -> dict:
        return self._mod_change("runtime-disable", self.mods.disable_runtime)

    def mod_validate(self) -> dict:
        return self.mods.validate_runtime()

    def mod_install_package(self, archive_bytes: bytes, filename: str = "mod.zip") -> dict:
        result = self._mod_change("install", lambda: self.mods.install_package(archive_bytes, filename))
        self._sync_client_mod_setting()
        return result

    def mod_set_enabled(self, mod_id: str, enabled: bool) -> dict:
        action = "enable" if enabled else "disable"
        result = self._mod_change(f"{action}-{mod_id}", lambda: self.mods.set_enabled(mod_id, enabled))
        if enabled:
            self._sync_client_mod_setting()
        return result

    def mod_remove(self, mod_id: str) -> dict:
        return self._mod_change(f"remove-{mod_id}", lambda: self.mods.remove(mod_id))

    def mod_log(self, lines: int = 160) -> list[str]:
        return self.mods.log_tail(lines)

    def mod_client_pack(self) -> dict:
        return self.mods.build_client_pack()

    def backup_create(self, label: str = "manual") -> dict:
        with self.operation_lock:
            try:
                if self.api.available():
                    self.api.save()
            except Exception:
                pass
            return self.backups.create(label)

    def backup_list(self) -> list[dict]:
        return self.backups.list()

    def backup_restore(self, name: str) -> dict:
        with self.operation_lock:
            before = self.service.status()
            running = str(before.get("state", "")).lower() in {"active", "running"}
            if running:
                self.service.stop()
            try:
                return self.backups.restore(name)
            finally:
                if running:
                    self.service.start()

    def backup_delete(self, name: str) -> dict:
        return self.backups.delete(name)

    def logs_tail(self, lines: int = 100, errors_only: bool = False) -> list[str]:
        return self.logs.tail(lines, errors_only)

    def crash_summary(self) -> dict:
        return self.logs.crash_summary()

    def players(self) -> list[dict]:
        rows = self.api.players()
        for row in rows:
            row.setdefault("platform", platform_from_user_id(str(row.get("userId", ""))))
        return rows

    def banned_players(self) -> list[dict]:
        return self.bans.list()

    def announce(self, message: str) -> dict:
        return self.api.announce(message)

    def player_action(self, action: str, user_id: str, message: str = "") -> dict:
        action = action.lower()
        user_id = str(user_id or "").strip()
        if not user_id:
            raise ValueError("A player user ID is required")
        if action == "kick":
            return self.api.kick(user_id, message or "Kicked by administrator")
        if action == "ban":
            player = {}
            try:
                player = next((row for row in self.players() if str(row.get("userId", "")) == user_id), {})
            except Exception:
                player = {}
            result = self.api.ban(user_id, message or "Banned by administrator")
            self.bans.add(
                user_id,
                player_name=str(player.get("name", "")),
                account_name=str(player.get("accountName", "")),
                platform=str(player.get("platform") or platform_from_user_id(user_id)),
                reason=message or "Banned by administrator",
            )
            return result
        if action == "unban":
            result = self.api.unban(user_id)
            self.bans.remove(user_id)
            return result
        raise ValueError(action)

    def overview(self) -> dict:
        """Return the frequently-used dashboard/watchdog data in one agent call."""
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
            "status": status,
            "health": health,
            "players": players,
            "backups": backups,
            "scheduler": scheduler,
            "logs": logs,
            "bans": self.banned_players(),
        }

    def watchdog_snapshot(self) -> dict:
        """Return only the live data needed by the Watchdog page."""
        return {
            "status": self.status(False),
            "health": self.health(),
            "logs": self.logs_tail(120),
        }

    def save_world(self) -> dict:
        return self.api.save()

    def graceful_shutdown(self, waittime: int = 30, message: str = "Server shutting down") -> dict:
        return self.api.shutdown(waittime, message)


    def world_list(self) -> list[dict]:
        return self.worlds.list_worlds()

    def world_archive(self, guid: str) -> dict:
        return self.worlds.archive(guid)

    def world_delete(self, guid: str) -> dict:
        with self.operation_lock:
            was_running = str(self.service.status().get("state", "")).lower() in {"active", "running"}
            if was_running:
                self.service.stop()
            try:
                return self.worlds.delete(guid, archive_first=True)
            finally:
                if was_running:
                    self.service.start()

    def world_new(self) -> dict:
        with self.operation_lock:
            was_running = str(self.service.status().get("state", "")).lower() in {"active", "running"}
            if was_running:
                self.service.stop()
            try:
                full_backup = self.backups.create("pre-new-world")
                result = self.worlds.archive_all_and_clear()
                return {"backup": full_backup, **result, "note": "Existing world directories were archived and cleared. Palworld will create a fresh world on startup."}
            finally:
                if was_running:
                    self.service.start()

    def network_diagnostics(self, include_public_ip: bool = True) -> dict:
        return diagnose(self.cfg, include_public_ip)

    def scheduler_config(self) -> dict:
        return {
            "backup": self.cfg.backup.__dict__,
            "updates": self.cfg.updates.__dict__,
        }

    def scheduler_update(self, payload: dict) -> dict:
        backup = payload.get("backup", {})
        updates = payload.get("updates", {})
        for key, value in backup.items():
            if hasattr(self.cfg.backup, key):
                setattr(self.cfg.backup, key, value)
        for key, value in updates.items():
            if hasattr(self.cfg.updates, key):
                setattr(self.cfg.updates, key, value)
        self._save_root_config()
        return self.scheduler_config()

    def diagnostics(self) -> dict:
        paths = {
            "install_dir": self.cfg.server.install_dir,
            "config_path": self.cfg.server.config_path,
            "save_dir": self.cfg.server.save_dir,
            "backup_dir": self.cfg.server.backup_dir,
            "steamcmd": self.cfg.server.steamcmd_path,
        }
        return {
            "paths": {key: {"path": value, "exists": Path(value).exists()} for key, value in paths.items()},
            "service": self.service.status(),
            "network": self.network_diagnostics(False),
            "api_available": self.api.available(),
            "config_readable": Path(self.cfg.server.config_path).is_file(),
            "config_writable": os.access(self.cfg.server.config_path, os.W_OK) if Path(self.cfg.server.config_path).exists() else False,
        }
