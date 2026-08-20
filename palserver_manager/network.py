from __future__ import annotations

import ipaddress
import os
import socket
import subprocess
from typing import Any

import psutil
import requests

from .config import AppConfig


def primary_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("1.1.1.1", 53))
        return sock.getsockname()[0]
    except OSError:
        return "Unavailable"
    finally:
        sock.close()


def public_ip() -> str:
    try:
        return requests.get("https://api.ipify.org", timeout=5).text.strip()
    except Exception:
        return "Unavailable"


def default_gateway() -> str:
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", "(Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Sort-Object RouteMetric | Select-Object -First 1).NextHop"],
                text=True, capture_output=True, timeout=5,
            )
            return result.stdout.strip() or "Unavailable"
        result = subprocess.run(["ip", "route", "show", "default"], text=True, capture_output=True, timeout=5)
        parts = result.stdout.split()
        if "via" in parts:
            return parts[parts.index("via") + 1]
    except Exception:
        pass
    return "Unavailable"


def udp_listening(port: int) -> bool:
    try:
        for connection in psutil.net_connections(kind="udp"):
            if connection.laddr and int(connection.laddr.port) == int(port):
                return True
    except (psutil.AccessDenied, OSError):
        pass
    return False


def diagnose(cfg: AppConfig, include_public_ip: bool = True) -> dict[str, Any]:
    local = primary_ip()
    public = public_ip() if include_public_ip else "Not checked"
    private = False
    try:
        private = ipaddress.ip_address(local).is_private
    except ValueError:
        pass
    return {
        "local_ip": local,
        "public_ip": public,
        "default_gateway": default_gateway(),
        "game_port": cfg.server.game_port,
        "game_udp_listening": udp_listening(cfg.server.game_port),
        "rest_api_port": cfg.server.rest_api_port,
        "rest_api_local_only": cfg.server.rest_api_host in ("127.0.0.1", "localhost", "::1"),
        "private_lan_address": private,
        "nat_likely": bool(private and public not in ("Unavailable", "Not checked", local)),
        "remote_note": "External UDP reachability must be tested from outside the server network; local checks cannot prove a port-forward is reachable.",
    }
