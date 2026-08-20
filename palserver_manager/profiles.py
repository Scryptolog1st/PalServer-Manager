from __future__ import annotations

import json
from pathlib import Path

from .config import user_data_dir


BUILTIN_PROFILES = {
    "Vanilla": {},
    "Casual PvE": {
        "ExpRate": "2.0",
        "PalCaptureRate": "1.5",
        "CollectionDropRate": "2.0",
        "CollectionObjectRespawnSpeedRate": "0.5",
        "PalEggDefaultHatchingTime": "0.5",
        "DeathPenalty": "None",
        "bIsPvP": "False",
    },
    "Hardcore": {
        "bHardcore": "True",
        "bPalLost": "True",
        "DeathPenalty": "All",
    },
    "PvP": {
        "bIsPvP": "True",
        "bEnablePlayerToPlayerDamage": "True",
        "DeathPenalty": "All",
    },
}


class ProfileManager:
    def __init__(self):
        self.path = user_data_dir() / "profiles.json"

    def all(self) -> dict[str, dict[str, str]]:
        result = dict(BUILTIN_PROFILES)
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    result.update(data)
            except Exception:
                pass
        return result

    def save(self, name: str, values: dict[str, str]) -> None:
        custom = {}
        if self.path.exists():
            try:
                custom = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                custom = {}
        custom[name] = values
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(custom, indent=2), encoding="utf-8")
