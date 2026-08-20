from __future__ import annotations

from pathlib import Path
from typing import Any

import psutil

from .backup import BackupManager
from .config import AppConfig
from .network import udp_listening


def _level(value: float, warning: float, critical: float) -> str:
    if value >= critical:
        return "critical"
    if value >= warning:
        return "warning"
    return "healthy"


def build_health(cfg: AppConfig, service_status: dict, metrics: dict | None = None) -> dict[str, Any]:
    cpu = psutil.cpu_percent(interval=0.15)
    memory = psutil.virtual_memory()
    install = Path(cfg.server.install_dir)
    disk = psutil.disk_usage(str(install if install.exists() else Path.cwd()))
    backup_age = BackupManager(cfg).latest_age_seconds()

    checks = []
    checks.append({"name": "CPU", "state": _level(cpu, cfg.health.cpu_warning, cfg.health.cpu_critical), "value": round(cpu, 1), "unit": "%"})
    checks.append({"name": "RAM", "state": _level(memory.percent, cfg.health.ram_warning, cfg.health.ram_critical), "value": round(memory.percent, 1), "unit": "%"})
    checks.append({"name": "Disk", "state": _level(disk.percent, cfg.health.disk_warning, cfg.health.disk_critical), "value": round(disk.percent, 1), "unit": "%"})

    active_states = {"active", "running"}
    service_state = str(service_status.get("state", "unknown")).lower()
    checks.append({"name": "Service", "state": "healthy" if service_state in active_states else "critical", "value": service_state})
    listening = udp_listening(cfg.server.game_port)
    checks.append({"name": "Game Port", "state": "healthy" if listening else "critical", "value": "listening" if listening else "not listening"})

    stale = cfg.health.stale_backup_hours * 3600
    if backup_age is None:
        checks.append({"name": "Backup", "state": "warning", "value": "none"})
    else:
        checks.append({"name": "Backup", "state": "warning" if backup_age > stale else "healthy", "value": int(backup_age), "unit": "seconds old"})

    if metrics:
        fps = float(metrics.get("serverfps", 0) or 0)
        checks.append({"name": "Server FPS", "state": "healthy" if fps >= 30 else ("warning" if fps >= 15 else "critical"), "value": fps})

    states = [row["state"] for row in checks]
    overall = "critical" if "critical" in states else ("warning" if "warning" in states else "healthy")
    return {
        "overall": overall,
        "cpu_percent": cpu,
        "memory_percent": memory.percent,
        "memory_used": memory.used,
        "memory_total": memory.total,
        "disk_percent": disk.percent,
        "disk_used": disk.used,
        "disk_total": disk.total,
        "thresholds": {
            "cpu": {"warning": cfg.health.cpu_warning, "critical": cfg.health.cpu_critical},
            "ram": {"warning": cfg.health.ram_warning, "critical": cfg.health.ram_critical},
            "disk": {"warning": cfg.health.disk_warning, "critical": cfg.health.disk_critical},
            "backup_stale_hours": cfg.health.stale_backup_hours,
        },
        "checks": checks,
    }
