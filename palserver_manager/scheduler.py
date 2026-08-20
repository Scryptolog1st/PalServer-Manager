from __future__ import annotations

import threading
import time
from datetime import datetime

from .manager import LocalManager


class AgentScheduler:
    """Simple cross-platform scheduler that runs inside the always-on agent."""

    def __init__(self, manager: LocalManager):
        self.manager = manager
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_backup = 0.0
        self.last_update_check = 0.0
        self.last_update_state: dict | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, name="palserver-scheduler", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _in_window(self) -> bool:
        cfg = self.manager.cfg.updates
        try:
            now = datetime.now().time()
            start = datetime.strptime(cfg.maintenance_start, "%H:%M").time()
            end = datetime.strptime(cfg.maintenance_end, "%H:%M").time()
            if start <= end:
                return start <= now <= end
            return now >= start or now <= end
        except ValueError:
            return False

    def _loop(self) -> None:
        while not self.stop_event.wait(30):
            now = time.time()
            try:
                backup_cfg = self.manager.cfg.backup
                if backup_cfg.enabled and now - self.last_backup >= max(5, backup_cfg.interval_minutes) * 60:
                    self.manager.backup_create("scheduled")
                    self.last_backup = now
            except Exception:
                pass

            try:
                update_cfg = self.manager.cfg.updates
                if update_cfg.auto_check and now - self.last_update_check >= max(10, update_cfg.check_interval_minutes) * 60:
                    self.last_update_state = self.manager.update_check()
                    self.last_update_check = now
                    if update_cfg.auto_install and self.last_update_state.get("state") == "available" and self._in_window():
                        if update_cfg.only_when_empty:
                            status = self.manager.status(False)
                            players = status.get("current_players")
                            if players not in (0, None):
                                continue
                        self.manager.update_server(backup=True, restart=True)
            except Exception:
                pass

    def state(self) -> dict:
        return {
            "running": bool(self.thread and self.thread.is_alive()),
            "last_backup": self.last_backup,
            "last_update_check": self.last_update_check,
            "last_update_state": self.last_update_state,
        }
