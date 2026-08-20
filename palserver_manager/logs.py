from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .config import AppConfig


class LogManager:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg

    def tail(self, lines: int = 100, errors_only: bool = False) -> list[str]:
        lines = max(1, min(int(lines), 5000))
        if os.name != "nt" and Path("/run/systemd/system").exists() and self.cfg.server.service_name:
            cmd = ["journalctl", "-u", self.cfg.server.service_name, "-n", str(lines), "--no-pager", "-o", "short-iso"]
            result = subprocess.run(cmd, text=True, capture_output=True)
            output = result.stdout.splitlines()
        else:
            path = Path(self.cfg.server.log_file)
            if path.exists():
                output = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
            else:
                output = ["No manager-captured PalServer log file is available yet."]
        if errors_only:
            needles = ("error", "warning", "fatal", "crash", "segv", "oom", "exception")
            output = [line for line in output if any(n in line.lower() for n in needles)]
        return output[-lines:]


    def game_version(self) -> str | None:
        import re
        rows = self.tail(500)
        for line in reversed(rows):
            match = re.search(r"Game version is\s+([^\s]+)", line)
            if match:
                return match.group(1)
        return None

    def crash_summary(self) -> dict:
        rows = self.tail(1500)
        lowered = [line.lower() for line in rows]
        return {
            "error_lines": sum("error" in line for line in lowered),
            "warning_lines": sum("warning" in line for line in lowered),
            "crash_markers": sum(any(x in line for x in ("fatal", "crash", "segv", "out of memory", "oom")) for line in lowered),
        }
