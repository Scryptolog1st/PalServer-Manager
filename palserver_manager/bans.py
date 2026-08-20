from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


class BanRegistry:
    """Persistent registry of bans performed through PalServer Manager.

    Palworld's REST API supports ban/unban operations but does not provide a
    list-bans endpoint.  The manager therefore records successful ban actions
    so administrators can review and instantly unban bans created through the
    manager.  The registry is intentionally server-side so every remote client
    sees the same list.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _read(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self.path.exists():
                return []
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return []
            if not isinstance(data, list):
                return []
            rows = [dict(item) for item in data if isinstance(item, dict)]
            rows.sort(key=lambda item: float(item.get("banned_at", 0) or 0), reverse=True)
            return rows

    def _write(self, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
            if os.name != "nt":
                try:
                    self.path.chmod(0o600)
                except OSError:
                    pass

    def list(self) -> list[dict[str, Any]]:
        return self._read()

    def add(
        self,
        user_id: str,
        *,
        player_name: str = "",
        account_name: str = "",
        platform: str = "Unknown",
        reason: str = "",
    ) -> dict[str, Any]:
        user_id = str(user_id or "").strip()
        if not user_id:
            raise ValueError("A user ID is required")
        with self._lock:
            rows = [row for row in self._read() if str(row.get("user_id", "")) != user_id]
            record = {
                "user_id": user_id,
                "player_name": str(player_name or ""),
                "account_name": str(account_name or ""),
                "platform": str(platform or "Unknown"),
                "reason": str(reason or ""),
                "banned_at": time.time(),
            }
            rows.insert(0, record)
            self._write(rows)
            return record

    def remove(self, user_id: str) -> bool:
        user_id = str(user_id or "").strip()
        with self._lock:
            rows = self._read()
            kept = [row for row in rows if str(row.get("user_id", "")) != user_id]
            changed = len(kept) != len(rows)
            if changed:
                self._write(kept)
            return changed
