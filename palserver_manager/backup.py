from __future__ import annotations

import os
import shutil
import tarfile
from datetime import datetime
from pathlib import Path

from .config import AppConfig


class BackupManager:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.root = Path(cfg.server.backup_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, label: str = "manual") -> dict:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = self.root / f"palworld-{label}-{stamp}.tar.gz"
        install = Path(self.cfg.server.install_dir)
        targets = [
            Path(self.cfg.server.save_dir),
            Path(self.cfg.server.config_path).parent,
        ]
        with tarfile.open(destination, "w:gz") as archive:
            for target in targets:
                if target.exists():
                    try:
                        arcname = target.relative_to(install)
                    except ValueError:
                        arcname = Path(target.name)
                    archive.add(target, arcname=str(arcname), recursive=True)
        self.enforce_retention()
        return {"path": str(destination), "size": destination.stat().st_size, "created": destination.stat().st_mtime}

    def list(self) -> list[dict]:
        rows = []
        for path in sorted(self.root.glob("palworld-*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True):
            stat = path.stat()
            rows.append({"name": path.name, "path": str(path), "size": stat.st_size, "created": stat.st_mtime})
        return rows

    def delete(self, name: str) -> dict:
        path = self._safe_backup(name)
        path.unlink()
        return {"deleted": name}

    def restore(self, name: str) -> dict:
        path = self._safe_backup(name)
        install = Path(self.cfg.server.install_dir).resolve()
        if self.cfg.backup.backup_before_restore:
            self.create("pre-restore")
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                target = (install / member.name).resolve()
                if install not in target.parents and target != install:
                    raise ValueError("Unsafe path found in backup archive")
            archive.extractall(install)
        return {"restored": name}

    def enforce_retention(self) -> None:
        keep = max(1, int(self.cfg.backup.retention_count))
        backups = self.list()
        for row in backups[keep:]:
            Path(row["path"]).unlink(missing_ok=True)

    def latest_age_seconds(self) -> float | None:
        rows = self.list()
        if not rows:
            return None
        return max(0.0, datetime.now().timestamp() - float(rows[0]["created"]))

    def _safe_backup(self, name: str) -> Path:
        if Path(name).name != name:
            raise ValueError("Backup name must not contain a path")
        path = (self.root / name).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError("Backup path escaped backup directory")
        if not path.exists():
            raise FileNotFoundError(path)
        return path
