from __future__ import annotations

from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from .config import AppConfig


class PalworldAPI:
    """Client for Palworld's built-in REST API.

    The official API is intended for LAN/local use. PalServer Manager's agent
    talks to it locally and can expose a separate authenticated management API.
    """

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        host = cfg.server.rest_api_host
        port = cfg.server.rest_api_port
        self.base = f"http://{host}:{port}/v1/api"
        self.auth = HTTPBasicAuth(cfg.server.rest_api_username, cfg.server.admin_password)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = requests.request(
            method,
            self.base + path,
            auth=self.auth,
            timeout=8,
            headers={"Accept": "application/json"},
            **kwargs,
        )
        response.raise_for_status()
        if not response.content:
            return {"ok": True}
        try:
            return response.json()
        except ValueError:
            return {"ok": True, "text": response.text}

    def info(self) -> dict:
        return self._request("GET", "/info")

    def players(self) -> list[dict]:
        result = self._request("GET", "/players")
        return result.get("players", []) if isinstance(result, dict) else []

    def settings(self) -> dict:
        return self._request("GET", "/settings")

    def metrics(self) -> dict:
        return self._request("GET", "/metrics")

    def announce(self, message: str) -> dict:
        return self._request("POST", "/announce", json={"message": message})

    def kick(self, user_id: str, message: str = "Kicked by administrator") -> dict:
        return self._request("POST", "/kick", json={"userid": user_id, "message": message})

    def ban(self, user_id: str, message: str = "Banned by administrator") -> dict:
        return self._request("POST", "/ban", json={"userid": user_id, "message": message})

    def unban(self, user_id: str) -> dict:
        return self._request("POST", "/unban", json={"userid": user_id})

    def save(self) -> dict:
        return self._request("POST", "/save")

    def shutdown(self, waittime: int = 30, message: str = "Server shutting down") -> dict:
        return self._request("POST", "/shutdown", json={"waittime": int(waittime), "message": message})

    def force_stop(self) -> dict:
        return self._request("POST", "/stop")

    def available(self) -> bool:
        try:
            self.info()
            return True
        except Exception:
            return False
