from __future__ import annotations

import argparse
import contextvars
import hmac
import os
import threading
import uuid
from contextlib import asynccontextmanager
from typing import Any

from . import __version__
from .config import AppConfig, load_config, save_config
from .manager import LocalManager
from .scheduler import AgentScheduler
from .host_ops import discover_palworld_servers, install_palworld_files, uninstall_palworld_files, update_linux_host


class InstanceRegistry:
    """Own all local server managers and schedulers for one agent host."""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg.resolve()
        self.lock = threading.RLock()
        self.managers: dict[str, LocalManager] = {}
        self.schedulers: dict[str, AgentScheduler] = {}
        self.started = False
        self._sync_from_config()

    def _sync_from_config(self) -> None:
        with self.lock:
            configured = {instance.id for instance in self.cfg.instances}
            for instance in self.cfg.instances:
                if instance.id not in self.managers:
                    manager = LocalManager(self.cfg, instance.id)
                    self.managers[instance.id] = manager
                    self.schedulers[instance.id] = AgentScheduler(manager)
                    if self.started and instance.enabled:
                        self.schedulers[instance.id].start()
            for instance_id in list(self.managers):
                if instance_id not in configured:
                    scheduler = self.schedulers.pop(instance_id, None)
                    if scheduler:
                        scheduler.stop()
                    self.managers.pop(instance_id, None)

    def get(self, instance_id: str | None = None) -> LocalManager:
        target = str(instance_id or self.cfg.active_instance_id or "default")
        with self.lock:
            if target not in self.managers:
                raise KeyError(f"Unknown server instance: {target}")
            return self.managers[target]

    def scheduler(self, instance_id: str | None = None) -> AgentScheduler:
        target = str(instance_id or self.cfg.active_instance_id or "default")
        with self.lock:
            if target not in self.schedulers:
                raise KeyError(f"Unknown server instance: {target}")
            return self.schedulers[target]

    def start(self) -> None:
        with self.lock:
            self.started = True
            for instance in self.cfg.instances:
                if instance.enabled:
                    self.schedulers[instance.id].start()

    def stop(self) -> None:
        with self.lock:
            self.started = False
            for scheduler in self.schedulers.values():
                scheduler.stop()

    def list(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        with self.lock:
            for instance in self.cfg.instances:
                manager = self.managers[instance.id]
                try:
                    service = manager.service.status()
                except Exception as exc:
                    service = {"state": "unknown", "pid": 0, "error": str(exc)}
                rows.append({
                    "id": instance.id,
                    "name": instance.name,
                    "enabled": instance.enabled,
                    "service_name": instance.server.service_name,
                    "install_dir": instance.server.install_dir,
                    "game_port": instance.server.game_port,
                    "rest_api_port": instance.server.rest_api_port,
                    "state": service.get("state", "unknown"),
                    "pid": service.get("pid", 0),
                })
        return rows

    @staticmethod
    def _normalized_path(value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        try:
            return os.path.realpath(os.path.abspath(raw))
        except Exception:
            return raw.rstrip("/")

    def find_discovered_match(self, server: dict[str, Any]) -> str:
        """Find the already-managed instance that represents a discovered install.

        Discovery/adoption must be idempotent.  Older releases could register a
        partially installed server and then, on the next discovery pass, try to
        create a second instance for the same physical Palworld installation.
        Match strongly on canonical paths first, then on service+port identity.
        """
        install_dir = self._normalized_path(str(server.get("install_dir") or ""))
        config_path = self._normalized_path(str(server.get("config_path") or ""))
        service_name = str(server.get("service_name") or "").strip()
        game_port = int(server.get("game_port") or 0)
        rest_port = int(server.get("rest_api_port") or 0)
        ranked: list[tuple[int, str]] = []
        with self.lock:
            for instance in self.cfg.instances:
                current = instance.server
                score = 0
                if install_dir and self._normalized_path(current.install_dir) == install_dir:
                    score += 100
                if config_path and self._normalized_path(current.config_path) == config_path:
                    score += 90
                if service_name and str(current.service_name or "").strip() == service_name:
                    score += 45
                if game_port and int(current.game_port or 0) == game_port:
                    score += 25
                if rest_port and int(current.rest_api_port or 0) == rest_port:
                    score += 15
                # Path equality is definitive.  Otherwise require at least
                # service+game-port identity so a shared default REST port alone
                # cannot accidentally merge two different local servers.
                if score >= 70:
                    ranked.append((score, instance.id))
        if not ranked:
            return ""
        ranked.sort(key=lambda row: row[0], reverse=True)
        return ranked[0][1]

    def prune_stale_discovery_duplicates(self, server: dict[str, Any], keep_id: str) -> list[str]:
        """Remove only stale duplicate registrations created by old adopt failures.

        A duplicate is safe to discard when it points at the exact same install
        directory as the canonical instance, or when it conflicts on the same
        service/ports but its configured install directory no longer contains a
        PalServer.sh.  Real installations on disk are never removed here.
        """
        removed: list[str] = []
        install_dir = self._normalized_path(str(server.get("install_dir") or ""))
        service_name = str(server.get("service_name") or "").strip()
        game_port = int(server.get("game_port") or 0)
        rest_port = int(server.get("rest_api_port") or 0)
        with self.lock:
            for instance in list(self.cfg.instances):
                if instance.id == keep_id:
                    continue
                current = instance.server
                current_install = self._normalized_path(current.install_dir)
                same_install = bool(install_dir and current_install == install_dir)
                same_identity = bool(
                    service_name
                    and str(current.service_name or "").strip() == service_name
                    and game_port
                    and int(current.game_port or 0) == game_port
                    and (not rest_port or int(current.rest_api_port or 0) == rest_port)
                )
                missing_install = not bool(current_install and os.path.isfile(os.path.join(current_install, "PalServer.sh")))
                if not same_install and not (same_identity and missing_install):
                    continue
                scheduler = self.schedulers.pop(instance.id, None)
                if scheduler:
                    scheduler.stop()
                self.managers.pop(instance.id, None)
                self.cfg.instances = [row for row in self.cfg.instances if row.id != instance.id]
                removed.append(instance.id)
            if removed:
                if self.cfg.active_instance_id not in {row.id for row in self.cfg.instances}:
                    self.cfg.active_instance_id = keep_id
                save_config(self.cfg)
        return removed

    def validate_discovered_resources(self, server: dict[str, Any], exclude_id: str = "") -> None:
        """Reject genuine host-local conflicts before mutating the registry."""
        install_dir = self._normalized_path(str(server.get("install_dir") or ""))
        service_name = str(server.get("service_name") or "").strip()
        game_port = int(server.get("game_port") or 0)
        rest_port = int(server.get("rest_api_port") or 0)
        with self.lock:
            for instance in self.cfg.instances:
                if instance.id == exclude_id:
                    continue
                current = instance.server
                if game_port and int(current.game_port or 0) == game_port:
                    raise ValueError(f"Game port {game_port} is already used by server instance '{instance.name}' on this host")
                if rest_port and int(current.rest_api_port or 0) == rest_port:
                    raise ValueError(f"REST API port {rest_port} is already used by server instance '{instance.name}' on this host")
                if service_name and str(current.service_name or "").strip() == service_name:
                    raise ValueError(f"Service name '{service_name}' is already used by server instance '{instance.name}' on this host")
                if install_dir and self._normalized_path(current.install_dir) == install_dir:
                    raise ValueError(f"Install directory '{server.get('install_dir')}' is already used by server instance '{instance.name}' on this host")

    def discard_uncommitted(self, instance_id: str) -> None:
        """Rollback a registry entry created inside a failed adopt transaction."""
        with self.lock:
            scheduler = self.schedulers.pop(instance_id, None)
            if scheduler:
                scheduler.stop()
            self.managers.pop(instance_id, None)
            self.cfg.instances = [row for row in self.cfg.instances if row.id != instance_id]
            if self.cfg.instances:
                if self.cfg.active_instance_id == instance_id:
                    self.cfg.active_instance_id = self.cfg.instances[0].id
            else:
                self.cfg.active_instance_id = ""
                self.cfg.host_only_mode = True
            save_config(self.cfg)

    def bootstrap_placeholder_id(self) -> str:
        """Return the synthetic first-run instance ID, if this host still has one.

        Older agent bootstrap releases loaded AppConfig before marking a newly
        provisioned host as host-only. That created a fake ``default`` /
        ``Primary Server`` instance using the normal 8211/8212 defaults even
        though no Palworld server had been adopted yet. The placeholder must
        never reserve ports against the first real server installed on this
        physical host.

        Be deliberately conservative: only the untouched default instance with
        no REST/admin credential is considered synthetic.
        """
        with self.lock:
            if len(self.cfg.instances) != 1:
                return ""
            instance = self.cfg.instances[0]
            server = instance.server
            if instance.id != "default" or instance.name != "Primary Server":
                return ""
            if str(server.admin_password or "").strip():
                return ""
            if str(server.service_name or "") not in {"", "palworld"}:
                return ""
            if int(server.game_port or 0) != 8211 or int(server.rest_api_port or 0) != 8212:
                return ""
            return instance.id

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            instance = self.cfg.add_instance(payload)
            save_config(self.cfg)
            manager = LocalManager(self.cfg, instance.id)
            scheduler = AgentScheduler(manager)
            self.managers[instance.id] = manager
            self.schedulers[instance.id] = scheduler
            if self.started and instance.enabled:
                scheduler.start()
            return manager.current_instance()

    def update(self, instance_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            instance = self.cfg.instance(instance_id)
            was_enabled = instance.enabled
            if "name" in payload:
                instance.name = str(payload.get("name") or instance.name).strip() or instance.name
            if "enabled" in payload:
                instance.enabled = bool(payload.get("enabled"))
            manager = self.managers[instance.id]
            if isinstance(payload.get("server"), dict):
                manager.update_server_config(dict(payload["server"]))
            manager.instance_name = instance.name
            save_config(self.cfg)
            scheduler = self.schedulers[instance.id]
            if self.started and instance.enabled and not was_enabled:
                # AgentScheduler objects are one-shot after stop(); replace it.
                scheduler = AgentScheduler(manager)
                self.schedulers[instance.id] = scheduler
                scheduler.start()
            elif was_enabled and not instance.enabled:
                scheduler.stop()
            return manager.current_instance()

    def delete(self, instance_id: str) -> dict[str, Any]:
        with self.lock:
            manager = self.get(instance_id)
            state = str(manager.service.status().get("state", "")).lower()
            if state in {"active", "running"}:
                raise RuntimeError("Stop this server instance before removing it from PalServer Manager")
            scheduler = self.schedulers.pop(instance_id, None)
            if scheduler:
                scheduler.stop()
            self.managers.pop(instance_id, None)
            if len(self.cfg.instances) <= 1:
                removed = self.cfg.instance(instance_id)
                self.cfg.instances = []
                self.cfg.active_instance_id = ""
                self.cfg.host_only_mode = True
            else:
                removed = self.cfg.remove_instance(instance_id)
            save_config(self.cfg)
            return {"deleted": removed.id, "active_instance_id": self.cfg.active_instance_id, "host_only": not bool(self.cfg.instances)}

    def unregister_after_uninstall(self, instance_id: str) -> dict[str, Any]:
        """Forget a game instance after its service/files were removed.

        Unlike the normal configuration-only delete operation, uninstalling
        the final Palworld instance on a provisioned host is valid: the agent
        remains installed in host-only mode and can provision another server
        later.
        """
        with self.lock:
            target = self.cfg.instance(instance_id)
            scheduler = self.schedulers.pop(target.id, None)
            if scheduler:
                scheduler.stop()
            self.managers.pop(target.id, None)
            self.cfg.instances = [row for row in self.cfg.instances if row.id != target.id]
            if self.cfg.instances:
                if self.cfg.active_instance_id == target.id:
                    self.cfg.active_instance_id = self.cfg.instances[0].id
                primary = next((row for row in self.cfg.instances if row.id == "default"), self.cfg.instances[0])
                self.cfg.server = primary.server
                self.cfg.backup = primary.backup
                self.cfg.updates = primary.updates
                self.cfg.health = primary.health
            else:
                self.cfg.active_instance_id = ""
                self.cfg.host_only_mode = True
            save_config(self.cfg)
            return {"deleted": target.id, "active_instance_id": self.cfg.active_instance_id, "host_only": not bool(self.cfg.instances)}


class ManagerProxy:
    def __init__(self, registry: InstanceRegistry, current_instance: contextvars.ContextVar[str]):
        self.registry = registry
        self.current_instance = current_instance

    def __getattr__(self, name: str):
        manager = self.registry.get(self.current_instance.get())
        return getattr(manager, name)


def create_app():
    try:
        from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
    except ImportError as exc:
        raise RuntimeError("Agent dependencies are not installed. Run: pip install 'palserver-manager[agent]'") from exc

    cfg = load_config()
    registry = InstanceRegistry(cfg)
    current_instance: contextvars.ContextVar[str] = contextvars.ContextVar(
        "palserver_manager_instance", default=cfg.active_instance_id
    )
    manager = ManagerProxy(registry, current_instance)

    def authenticate(x_palmanager_token: str | None = Header(default=None)) -> None:
        expected = cfg.agent.token or ""
        supplied = x_palmanager_token or ""
        if not expected or not hmac.compare_digest(expected, supplied):
            raise HTTPException(status_code=401, detail="Invalid management token")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        registry.start()
        yield
        registry.stop()

    app = FastAPI(
        title="PalServer Manager Agent",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def select_instance_for_request(request: Request, call_next):
        requested = request.headers.get("X-PalManager-Instance") or cfg.active_instance_id
        token = current_instance.set(str(requested))
        try:
            return await call_next(request)
        finally:
            current_instance.reset(token)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "instances": len(cfg.instances)}

    @app.get("/v1/instances", dependencies=[Depends(authenticate)])
    def instances():
        return registry.list()

    @app.post("/v1/instances", dependencies=[Depends(authenticate)])
    def create_instance(payload: dict[str, Any] = Body(...)):
        try:
            return registry.create(payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/v1/instances/{instance_id}", dependencies=[Depends(authenticate)])
    def update_instance(instance_id: str, payload: dict[str, Any] = Body(...)):
        try:
            return registry.update(instance_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/v1/instances/{instance_id}", dependencies=[Depends(authenticate)])
    def delete_instance(instance_id: str):
        try:
            return registry.delete(instance_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/server-config", dependencies=[Depends(authenticate)])
    def server_config():
        return manager.server_config()

    @app.put("/v1/server-config", dependencies=[Depends(authenticate)])
    def update_server_config(payload: dict[str, Any] = Body(...)):
        try:
            return manager.update_server_config(payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/status", dependencies=[Depends(authenticate)])
    def status(include_update: bool = Query(False)):
        return manager.status(include_update)

    @app.get("/v1/overview", dependencies=[Depends(authenticate)])
    def overview():
        return manager.overview()

    @app.get("/v1/watchdog", dependencies=[Depends(authenticate)])
    def watchdog():
        return manager.watchdog_snapshot()

    @app.get("/v1/health", dependencies=[Depends(authenticate)])
    def health():
        return manager.health()

    @app.get("/v1/settings/compare-defaults", dependencies=[Depends(authenticate)])
    def compare_defaults():
        return manager.compare_defaults()

    @app.get("/v1/settings", dependencies=[Depends(authenticate)])
    def settings(q: str = Query("")):
        return manager.settings(q)

    @app.post("/v1/settings/reset-defaults", dependencies=[Depends(authenticate)])
    def reset_defaults(payload: dict[str, Any] = Body(default={})):
        try:
            return manager.reset_defaults(payload.get("keys"))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/v1/settings/{key}", dependencies=[Depends(authenticate)])
    def set_setting(key: str, payload: dict[str, Any] = Body(...)):
        if "value" not in payload:
            raise HTTPException(status_code=400, detail="Missing value")
        try:
            return manager.set_setting(key, payload["value"])
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/profiles", dependencies=[Depends(authenticate)])
    def profiles():
        return manager.profiles_list()

    @app.post("/v1/profiles/apply", dependencies=[Depends(authenticate)])
    def apply_profile(payload: dict[str, Any] = Body(...)):
        try:
            return manager.profile_apply(str(payload.get("name", "")))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/service/{action}", dependencies=[Depends(authenticate)])
    def service_action(action: str):
        if action not in {"start", "stop", "restart"}:
            raise HTTPException(status_code=400, detail="Unsupported service action")
        try:
            return manager.service_action(action)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/v1/update", dependencies=[Depends(authenticate)])
    def update_check():
        try:
            return manager.update_check()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/v1/update", dependencies=[Depends(authenticate)])
    def update_server(payload: dict[str, Any] = Body(default={})):
        try:
            return manager.update_server(bool(payload.get("backup", True)), bool(payload.get("restart", True)))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/v1/backups", dependencies=[Depends(authenticate)])
    def backup_list():
        return manager.backup_list()

    @app.post("/v1/backups", dependencies=[Depends(authenticate)])
    def backup_create(payload: dict[str, Any] = Body(default={})):
        try:
            return manager.backup_create(str(payload.get("label", "manual")))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/v1/backups/{name}/restore", dependencies=[Depends(authenticate)])
    def backup_restore(name: str):
        try:
            return manager.backup_restore(name)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.delete("/v1/backups/{name}", dependencies=[Depends(authenticate)])
    def backup_delete(name: str):
        try:
            return manager.backup_delete(name)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/logs", dependencies=[Depends(authenticate)])
    def logs(lines: int = Query(100, ge=1, le=5000), errors_only: bool = Query(False)):
        return manager.logs_tail(lines, errors_only)

    @app.get("/v1/logs/crashes", dependencies=[Depends(authenticate)])
    def crash_summary():
        return manager.crash_summary()

    @app.get("/v1/players", dependencies=[Depends(authenticate)])
    def players():
        try:
            return manager.players()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Palworld REST API unavailable: {exc}") from exc

    @app.get("/v1/bans", dependencies=[Depends(authenticate)])
    def bans():
        return manager.banned_players()

    @app.post("/v1/announce", dependencies=[Depends(authenticate)])
    def announce(payload: dict[str, Any] = Body(...)):
        try:
            return manager.announce(str(payload.get("message", "")))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/v1/players/{action}", dependencies=[Depends(authenticate)])
    def player_action(action: str, payload: dict[str, Any] = Body(...)):
        if action not in {"kick", "ban", "unban"}:
            raise HTTPException(status_code=400, detail="Unsupported player action")
        try:
            return manager.player_action(action, str(payload.get("userid", "")), str(payload.get("message", "")))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/v1/world/save", dependencies=[Depends(authenticate)])
    def save_world():
        try:
            return manager.save_world()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/v1/world/shutdown", dependencies=[Depends(authenticate)])
    def shutdown_world(payload: dict[str, Any] = Body(default={})):
        try:
            return manager.graceful_shutdown(int(payload.get("waittime", 30)), str(payload.get("message", "Server shutting down")))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/v1/worlds", dependencies=[Depends(authenticate)])
    def world_list():
        return manager.world_list()

    @app.post("/v1/worlds/new", dependencies=[Depends(authenticate)])
    def world_new():
        try:
            return manager.world_new()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/v1/worlds/{guid}/archive", dependencies=[Depends(authenticate)])
    def world_archive(guid: str):
        try:
            return manager.world_archive(guid)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/v1/worlds/{guid}", dependencies=[Depends(authenticate)])
    def world_delete(guid: str):
        try:
            return manager.world_delete(guid)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/network", dependencies=[Depends(authenticate)])
    def network(include_public_ip: bool = Query(True)):
        return manager.network_diagnostics(include_public_ip)

    @app.get("/v1/scheduler", dependencies=[Depends(authenticate)])
    def scheduler_get():
        result = manager.scheduler_config()
        result["runtime"] = registry.scheduler(current_instance.get()).state()
        return result

    @app.put("/v1/scheduler", dependencies=[Depends(authenticate)])
    def scheduler_set(payload: dict[str, Any] = Body(...)):
        return manager.scheduler_update(payload)

    @app.get("/v1/diagnostics", dependencies=[Depends(authenticate)])
    def diagnostics():
        return manager.diagnostics()

    @app.get("/v1/mods", dependencies=[Depends(authenticate)])
    def mods_status():
        try:
            return manager.mods_status()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/v1/mods/runtime/enable", dependencies=[Depends(authenticate)])
    def mods_runtime_enable(payload: dict[str, Any] = Body(default={})): 
        try:
            return manager.mod_runtime_enable(str((payload or {}).get("source_url") or ""))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/v1/mods/runtime/disable", dependencies=[Depends(authenticate)])
    def mods_runtime_disable():
        try:
            return manager.mod_runtime_disable()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/v1/mods/validate", dependencies=[Depends(authenticate)])
    def mods_validate():
        try:
            return manager.mod_validate()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/v1/mods/log", dependencies=[Depends(authenticate)])
    def mods_log(lines: int = Query(160, ge=1, le=5000)):
        return manager.mod_log(lines)

    @app.post("/v1/mods/packages", dependencies=[Depends(authenticate)])
    async def mods_install_package(request: Request, filename: str = Query("mod.zip")):
        try:
            body = await request.body()
            if len(body) > 512 * 1024 * 1024:
                raise ValueError("Mod package exceeds the 512 MiB agent upload limit")
            return manager.mod_install_package(body, filename)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/v1/mods/{mod_id}/enable", dependencies=[Depends(authenticate)])
    def mods_enable(mod_id: str):
        try:
            return manager.mod_set_enabled(mod_id, True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/v1/mods/{mod_id}/disable", dependencies=[Depends(authenticate)])
    def mods_disable(mod_id: str):
        try:
            return manager.mod_set_enabled(mod_id, False)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.delete("/v1/mods/{mod_id}", dependencies=[Depends(authenticate)])
    def mods_remove(mod_id: str):
        try:
            return manager.mod_remove(mod_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/v1/mods/client-pack", dependencies=[Depends(authenticate)])
    def mods_client_pack():
        try:
            from fastapi.responses import Response
            result = manager.mod_client_pack()
            path = result.get("path")
            data = open(path, "rb").read()
            return Response(
                content=data,
                media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="{result.get("name", "palserver-client-modpack.zip")}"'},
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/v1/host/info", dependencies=[Depends(authenticate)])
    def host_info():
        import platform
        return {
            "hostname": platform.node(),
            "os": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "agent_version": __version__,
            "instances": len(cfg.instances),
        }

    @app.get("/v1/host/discover", dependencies=[Depends(authenticate)])
    def host_discover():
        try:
            rows = discover_palworld_servers()
            for row in rows:
                matched_id = registry.find_discovered_match(row)
                if matched_id:
                    repaired = registry.prune_stale_discovery_duplicates(row, matched_id)
                    managed = registry.get(matched_id).current_instance()
                    row["managed_instance_id"] = matched_id
                    row["managed_instance_name"] = managed.get("name", matched_id)
                    row["repaired_duplicate_ids"] = repaired
                else:
                    row["managed_instance_id"] = ""
                    row["repaired_duplicate_ids"] = []
            return rows
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/v1/host/update-os", dependencies=[Depends(authenticate)])
    def host_update_os():
        try:
            return update_linux_host()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/v1/host/adopt", dependencies=[Depends(authenticate)])
    def host_adopt(payload: dict[str, Any] = Body(...)):
        """Register a discovered Palworld install with this host agent.

        Adoption is deliberately idempotent.  Clicking Discover & Link Servers
        repeatedly must never create a second agent instance for a server that
        is already managed locally.
        """
        created_id = ""
        try:
            server = dict(payload.get("server") or {})
            instance_id = str(payload.get("id") or "server")
            name = str(payload.get("name") or server.get("name") or instance_id)

            # First identify an already-managed physical installation.  This is
            # stronger than the old raw-string install-dir comparison because
            # it canonicalizes paths and can recognize a prior partial
            # registration by service+port identity.
            existing_id = registry.find_discovered_match(server)
            if existing_id:
                removed = registry.prune_stale_discovery_duplicates(server, existing_id)
                local = registry.get(existing_id)
                current = local.current_instance()
                return {
                    **current,
                    "already_managed": True,
                    "adopted": False,
                    "repaired_duplicate_ids": removed,
                    "restart_required": False,
                }

            # Do not mutate the registry until genuine host-local resource
            # collisions are ruled out.  Older releases created the new row
            # first and could leave an orphan duplicate behind when the later
            # validation failed.
            registry.validate_discovered_resources(server)

            # The REST API is loopback/private. Preserve an existing admin
            # password when the file has one; otherwise generate a fresh one.
            import secrets
            config_path = str(server.get("config_path") or "")
            existing_admin = ""
            if config_path and os.path.isfile(config_path):
                try:
                    from .settings import IniManager, unquote
                    probe = IniManager(config_path, os.path.join(os.path.dirname(config_path), ".palserver-manager"))
                    existing_admin = unquote(probe.values(reveal_secrets=True).get("AdminPassword", '""'))
                except Exception:
                    existing_admin = ""
            server["admin_password"] = existing_admin or secrets.token_urlsafe(24)
            server.setdefault("rest_api_host", "127.0.0.1")
            server.setdefault("rest_api_username", "admin")

            created = registry.create({"id": instance_id, "name": name, "server": server})
            created_id = str(created.get("id") or instance_id)
            local = registry.get(created_id)
            local.update_server_config({
                **server,
                "admin_password": server["admin_password"],
                "rest_api_port": int(server.get("rest_api_port") or 8212),
                "sync_palworld_rest": True,
            })
            try:
                local.set_setting("ServerName", name)
            except Exception:
                pass
            state = str(local.service.status().get("state", "")).lower()
            return {
                **local.current_instance(),
                "already_managed": False,
                "adopted": True,
                "restart_required": state in {"active", "running"},
            }
        except Exception as exc:
            if created_id:
                try:
                    registry.discard_uncommitted(created_id)
                except Exception:
                    pass
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    install_jobs_lock = threading.RLock()
    install_jobs: dict[str, dict[str, Any]] = {}

    def _install_job_log(job_id: str, message: str) -> None:
        text = str(message or "").replace("\r", "\n")
        lines = [line.rstrip() for line in text.splitlines() if line.rstrip()]
        if not lines:
            return
        with install_jobs_lock:
            job = install_jobs.get(job_id)
            if job is None:
                return
            job_lines = job.setdefault("lines", [])
            job_lines.extend(lines)
            # Bound memory usage while retaining a substantial installation history.
            if len(job_lines) > 10000:
                del job_lines[:-10000]

    def _perform_host_install_palworld(payload: dict[str, Any], progress=None) -> dict[str, Any]:
        instance_id = str(payload.get("id") or "001")
        name = str(payload.get("name") or f"Palworld Server {instance_id}").strip()
        game_port = int(payload.get("game_port") or 8211)
        rest_port = int(payload.get("rest_api_port") or 8212)
        install_dir = str(payload.get("install_dir") or f"/opt/palworld-{instance_id}")
        service_name = str(payload.get("service_name") or f"palworld-{instance_id}")
        max_players = int(payload.get("max_players") or 32)
        replace_bootstrap = bool(payload.get("replace_bootstrap_placeholder", False))
        placeholder_id = registry.bootstrap_placeholder_id() if replace_bootstrap else ""

        if progress:
            progress(f"Validating host-local ports, service name, and install directory for {name}...")
        for row in registry.list():
            row_id = str(row.get("id") or "")
            if placeholder_id and row_id == placeholder_id:
                continue
            if int(row.get("game_port") or 0) == game_port:
                raise ValueError(f"Game port {game_port} is already used by server instance '{row.get('name') or row_id}' on this host")
            if int(row.get("rest_api_port") or 0) == rest_port:
                raise ValueError(f"REST API port {rest_port} is already used by server instance '{row.get('name') or row_id}' on this host")
            if str(row.get("service_name") or "") == service_name:
                raise ValueError(f"Service name '{service_name}' is already used by server instance '{row.get('name') or row_id}' on this host")
            if str(row.get("install_dir") or "") == install_dir:
                raise ValueError(f"Install directory '{install_dir}' is already used by server instance '{row.get('name') or row_id}' on this host")

        prepared = install_palworld_files(
            instance_id=instance_id,
            name=name,
            install_dir=install_dir,
            service_name=service_name,
            game_port=game_port,
            rest_api_port=rest_port,
            max_players=max_players,
            progress=progress,
        )
        if progress:
            progress("Registering the new Palworld instance with the local agent...")
        if placeholder_id:
            created = registry.update(placeholder_id, {"name": name, "server": prepared["server"]})
            local = registry.get(placeholder_id)
        else:
            created = registry.create({"id": instance_id, "name": name, "server": prepared["server"]})
            local = registry.get(str(created.get("id") or instance_id))
        if progress:
            progress("Applying PalServer Manager REST API and server settings...")
        local.update_server_config({**prepared["server"], "sync_palworld_rest": True})
        local.set_setting("ServerName", name)
        local.set_setting("ServerPlayerMaxNum", max_players)
        if progress:
            progress(f"Starting systemd service {service_name}...")
        local.service.start()
        status = local.service.status()
        if progress:
            progress(f"Palworld service start requested successfully; current state: {status.get('state', 'unknown')}.")
        return {**local.current_instance(), "installed": True, "service": status}

    @app.post("/v1/host/install-palworld", dependencies=[Depends(authenticate)])
    def host_install_palworld(payload: dict[str, Any] = Body(...)):
        """Backward-compatible blocking Palworld install endpoint."""
        try:
            return _perform_host_install_palworld(payload)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/v1/host/install-palworld/start", dependencies=[Depends(authenticate)])
    def host_install_palworld_start(payload: dict[str, Any] = Body(...)):
        """Start a Palworld install job and return immediately for live polling."""
        job_id = uuid.uuid4().hex
        with install_jobs_lock:
            # Keep only a bounded set of completed jobs.
            completed = [key for key, value in install_jobs.items() if value.get("status") in {"completed", "failed"}]
            for old_id in completed[:-32]:
                install_jobs.pop(old_id, None)
            install_jobs[job_id] = {
                "id": job_id,
                "status": "running",
                "lines": [],
                "result": None,
                "error": "",
            }

        def progress(message: str) -> None:
            _install_job_log(job_id, message)

        progress("Installation job accepted by the remote agent.")

        def runner() -> None:
            try:
                result = _perform_host_install_palworld(dict(payload), progress=progress)
            except Exception as exc:
                progress(f"ERROR: {exc}")
                with install_jobs_lock:
                    job = install_jobs.get(job_id)
                    if job is not None:
                        job["status"] = "failed"
                        job["error"] = str(exc)
            else:
                progress("Palworld installation job completed successfully.")
                with install_jobs_lock:
                    job = install_jobs.get(job_id)
                    if job is not None:
                        job["status"] = "completed"
                        job["result"] = result

        threading.Thread(target=runner, name=f"palworld-install-{job_id[:8]}", daemon=True).start()
        return {"job_id": job_id, "status": "running"}

    @app.get("/v1/host/install-palworld/jobs/{job_id}", dependencies=[Depends(authenticate)])
    def host_install_palworld_job(job_id: str, offset: int = Query(0, ge=0)):
        with install_jobs_lock:
            job = install_jobs.get(str(job_id))
            if job is None:
                raise HTTPException(status_code=404, detail="Installation job was not found")
            lines = list(job.get("lines") or [])
            start = min(int(offset), len(lines))
            return {
                "job_id": job_id,
                "status": job.get("status", "running"),
                "lines": lines[start:],
                "next_offset": len(lines),
                "result": job.get("result"),
                "error": job.get("error", ""),
            }

    @app.post("/v1/host/uninstall-palworld", dependencies=[Depends(authenticate)])
    def host_uninstall_palworld(payload: dict[str, Any] = Body(...)):
        """Stop, back up, and uninstall one Palworld instance from this host."""
        try:
            instance_id = str(payload.get("id") or current_instance.get() or "").strip()
            if not instance_id:
                raise ValueError("A server instance ID is required")
            local = registry.get(instance_id)
            server = local.cfg.server
            install_dir = str(server.install_dir or "")
            service_name = str(server.service_name or "")
            save_dir = str(server.save_dir or "")
            final_backup = None
            # Protect player data by making one final manager backup whenever
            # a save directory actually exists.  If that backup fails we stop
            # the uninstall rather than silently deleting the world.
            if save_dir and os.path.isdir(save_dir):
                final_backup = local.backup_create("pre-uninstall")
            try:
                state = str(local.service.status().get("state", "")).lower()
                if state in {"active", "running"}:
                    local.service.stop()
            except Exception:
                # uninstall_palworld_files also performs a best-effort stop.
                pass
            removed = uninstall_palworld_files(install_dir=install_dir, service_name=service_name)
            unregistered = registry.unregister_after_uninstall(instance_id)
            return {**removed, **unregistered, "final_backup": final_backup}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/v1/tools", dependencies=[Depends(authenticate)])
    def tools():
        from .tools import TOOLS
        return [tool.__dict__ for tool in TOOLS]

    return app


app = None


def main() -> None:
    parser = argparse.ArgumentParser(description="PalServer Manager remote agent")
    parser.add_argument("--host", help="Bind address; defaults to config")
    parser.add_argument("--port", type=int, help="Bind port; defaults to config")
    parser.add_argument("--config", help="Override config path")
    args = parser.parse_args()

    if args.config:
        os.environ["PALSERVER_MANAGER_CONFIG"] = args.config
    cfg = load_config()
    host = args.host or cfg.agent.bind_host
    port = args.port or cfg.agent.port

    if host not in {"127.0.0.1", "localhost", "::1"}:
        if not cfg.agent.allow_direct_wan:
            raise SystemExit(
                "Refusing non-loopback agent bind. Set agent.allow_direct_wan=true only when using TLS and a firewall. "
                "Preferred remote mode is an SSH/VPN tunnel to the loopback agent."
            )
        if not (cfg.agent.tls_cert and cfg.agent.tls_key):
            raise SystemExit("Direct WAN mode requires agent.tls_cert and agent.tls_key.")

    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("Install agent dependencies: pip install 'palserver-manager[agent]'") from exc

    uvicorn.run(
        "palserver_manager.agent:create_app",
        factory=True,
        host=host,
        port=port,
        ssl_certfile=cfg.agent.tls_cert or None,
        ssl_keyfile=cfg.agent.tls_key or None,
        log_level="info",
    )


if __name__ == "__main__":
    main()
