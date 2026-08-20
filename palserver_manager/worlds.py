from __future__ import annotations

import shutil
import tarfile
from datetime import datetime
from pathlib import Path

from .config import AppConfig


def _dir_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            pass
    return total


class WorldManager:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.root = Path(cfg.server.save_dir) / "0"
        self.archive_root = Path(cfg.server.backup_dir) / "world-archives"
        self.archive_root.mkdir(parents=True, exist_ok=True)

    def list_worlds(self) -> list[dict]:
        if not self.root.exists():
            return []
        rows = []
        for path in self.root.iterdir():
            if not path.is_dir():
                continue
            level = path / "Level.sav"
            if not level.exists():
                continue
            stat = level.stat()
            rows.append({
                "guid": path.name,
                "path": str(path),
                "size": _dir_size(path),
                "modified": stat.st_mtime,
                "has_world_option": (path / "WorldOption.sav").exists(),
                "level_sav": str(level),
            })
        rows.sort(key=lambda row: row["modified"], reverse=True)
        return rows

    def archive(self, guid: str, label: str = "world") -> dict:
        world = self._world(guid)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = self.archive_root / f"{guid}-{label}-{stamp}.tar.gz"
        with tarfile.open(destination, "w:gz") as archive:
            archive.add(world, arcname=guid)
        return {"guid": guid, "archive": str(destination), "size": destination.stat().st_size}

    def delete(self, guid: str, archive_first: bool = True) -> dict:
        world = self._world(guid)
        archived = self.archive(guid, "pre-delete") if archive_first else None
        shutil.rmtree(world)
        return {"deleted": guid, "archive": archived}

    def archive_all_and_clear(self) -> dict:
        worlds = self.list_worlds()
        archives = []
        for row in worlds:
            archives.append(self.archive(row["guid"], "pre-new-world"))
        for row in worlds:
            shutil.rmtree(Path(row["path"]))
        self.root.mkdir(parents=True, exist_ok=True)
        return {"archived": archives, "cleared_worlds": len(worlds)}

    def _world(self, guid: str) -> Path:
        if Path(guid).name != guid:
            raise ValueError("Invalid world GUID")
        path = (self.root / guid).resolve()
        root = self.root.resolve()
        if root not in path.parents:
            raise ValueError("World path escaped save root")
        if not path.is_dir() or not (path / "Level.sav").exists():
            raise FileNotFoundError(path)
        return path
