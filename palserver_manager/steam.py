from __future__ import annotations

import os
import json
import time
import re
import subprocess
import shutil
from pathlib import Path

from .config import AppConfig, user_data_dir


class SteamManager:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.manifest = Path(cfg.server.install_dir) / "steamapps" / f"appmanifest_{cfg.server.app_id}.acf"
        self.cache_path = user_data_dir() / "steam_update_cache.json"

    def installed_build(self) -> str | None:
        if not self.manifest.exists():
            # Some SteamCMD layouts put the manifest one directory above install_dir.
            alt = Path(self.cfg.server.install_dir).parent.parent / "appmanifest_2394010.acf"
            manifest = alt if alt.exists() else self.manifest
        else:
            manifest = self.manifest
        if not manifest.exists():
            return None
        text = manifest.read_text(encoding="utf-8", errors="replace")
        match = re.search(r'"buildid"\s+"([^"]+)"', text)
        return match.group(1) if match else None

    @staticmethod
    def _parse_public_build(text: str) -> str | None:
        index = text.find('"branches"')
        section = text[index:] if index >= 0 else text
        match = re.search(r'"public"\s*\{.*?"buildid"\s+"([^"]+)"', section, re.DOTALL)
        if match:
            return match.group(1)
        matches = re.findall(r'"buildid"\s+"([^"]+)"', text)
        return matches[-1] if matches else None

    def _base_cmd(self) -> list[str]:
        steamcmd = self.cfg.server.steamcmd_path
        if os.name != "nt" and os.geteuid() == 0 and self.cfg.server.steam_user:
            if shutil.which("sudo"):
                return ["sudo", "-u", self.cfg.server.steam_user, "-H", steamcmd]
            if shutil.which("runuser"):
                return ["runuser", "-u", self.cfg.server.steam_user, "--", steamcmd]
        return [steamcmd]

    def latest_build(self, timeout: int = 45) -> str | None:
        if not Path(self.cfg.server.steamcmd_path).exists():
            raise FileNotFoundError(self.cfg.server.steamcmd_path)
        cmd = [*self._base_cmd(), "+login", "anonymous", "+app_info_update", "1", "+app_info_print", self.cfg.server.app_id, "+quit"]
        result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return self._parse_public_build(result.stdout or "")

    def cached_update_status(self, max_age_seconds: int = 600) -> dict:
        installed = self.installed_build()
        try:
            if self.cache_path.exists():
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if time.time() - float(data.get("checked_at", 0)) <= max_age_seconds:
                    # Installed build may have changed since the cache was written.
                    data["installed"] = installed
                    if installed and data.get("latest"):
                        data["state"] = "current" if installed == data["latest"] else "available"
                    return data
        except Exception:
            pass
        return {"installed": installed, "latest": None, "state": "not-checked", "checked_at": None}

    def update_status(self, force: bool = True) -> dict:
        if not force:
            cached = self.cached_update_status()
            if cached.get("state") != "not-checked":
                return cached
        installed = self.installed_build()
        latest = self.latest_build()
        state = "unknown"
        if installed and latest:
            state = "current" if installed == latest else "available"
        data = {"installed": installed, "latest": latest, "state": state, "checked_at": time.time()}
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass
        return data

    def update(self) -> dict:
        if not Path(self.cfg.server.steamcmd_path).exists():
            raise FileNotFoundError(self.cfg.server.steamcmd_path)
        cmd = [
            *self._base_cmd(),
            "+force_install_dir", self.cfg.server.install_dir,
            "+login", "anonymous",
            "+app_update", self.cfg.server.app_id, "validate",
            "+quit",
        ]
        result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if result.returncode != 0:
            raise RuntimeError(result.stdout[-4000:])
        return {"ok": True, "build": self.installed_build(), "output_tail": (result.stdout or "")[-4000:]}
