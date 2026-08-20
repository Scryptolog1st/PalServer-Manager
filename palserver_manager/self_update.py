from __future__ import annotations

import subprocess
import sys

import requests

from . import __version__
from .config import AppConfig


class SelfUpdater:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg

    def check(self) -> dict:
        repo = self.cfg.github_repo.strip().strip("/")
        if not repo:
            return {"configured": False, "current": __version__, "latest": None, "state": "unconfigured"}
        response = requests.get(f"https://api.github.com/repos/{repo}/releases/latest", timeout=10, headers={"Accept": "application/vnd.github+json"})
        response.raise_for_status()
        data = response.json()
        latest = str(data.get("tag_name", "")).lstrip("v")
        state = "current" if latest == __version__ else "available"
        return {"configured": True, "current": __version__, "latest": latest, "tag": data.get("tag_name"), "state": state, "html_url": data.get("html_url")}

    def install_latest(self) -> dict:
        check = self.check()
        if check.get("state") != "available":
            return check
        repo = self.cfg.github_repo.strip().strip("/")
        tag = check["tag"]
        command = [sys.executable, "-m", "pip", "install", "--upgrade", f"git+https://github.com/{repo}.git@{tag}"]
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if result.returncode != 0:
            raise RuntimeError(result.stdout[-4000:])
        return {**check, "installed": True, "output_tail": result.stdout[-2000:]}
