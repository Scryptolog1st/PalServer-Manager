from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolSpec:
    id: str
    name: str
    description: str
    category: str
    destructive: bool = False


TOOLS = [
    ToolSpec("dashboard", "Dashboard", "Server, host, players, build, port and health overview.", "Monitor"),
    ToolSpec("watchdog", "Live Watchdog", "Refresh CPU, RAM, storage, server metrics and console logs.", "Monitor"),
    ToolSpec("health", "Health Check", "Evaluate CPU, memory, disk, service, game port, FPS and backups.", "Monitor"),
    ToolSpec("logs", "Log Viewer", "Tail console/journal logs or show only warnings and errors.", "Monitor"),
    ToolSpec("crashes", "Crash History", "Summarize recent error, warning and crash markers.", "Monitor"),
    ToolSpec("settings", "Settings Manager", "Browse and edit PalWorldSettings.ini with verified writes.", "Configuration"),
    ToolSpec("mods", "Mods & Runtime", "Manage Linux UE4SS runtime, managed mods, runtime health, and client mod packs.", "Configuration"),
    ToolSpec("search", "Search Settings", "Search setting names and human-readable descriptions.", "Configuration"),
    ToolSpec("compare", "Compare Defaults", "Show only settings that differ from DefaultPalWorldSettings.ini.", "Configuration"),
    ToolSpec("profiles", "Config Profiles", "Apply Vanilla, Casual PvE, Hardcore, PvP or custom profiles.", "Configuration"),
    ToolSpec("start", "Start Server", "Start the Palworld server service/process.", "Server Control"),
    ToolSpec("stop", "Stop Server", "Stop the Palworld server service/process.", "Server Control", True),
    ToolSpec("restart", "Restart Server", "Restart the Palworld server service/process.", "Server Control", True),
    ToolSpec("save", "Save World", "Ask Palworld's REST API to save the world immediately.", "Server Control"),
    ToolSpec("update-check", "Check Game Update", "Compare installed Steam build with the public branch.", "Maintenance"),
    ToolSpec("update", "Update Game Server", "Backup, gracefully stop, validate/update with SteamCMD and restart.", "Maintenance", True),
    ToolSpec("backups", "Backup Manager", "Create, list, restore and delete compressed server backups.", "Maintenance"),
    ToolSpec("worlds", "World Manager", "List, archive, delete or create a fresh server world with safety backups.", "Maintenance", True),
    ToolSpec("scheduler", "Automation", "Configure scheduled backups and maintenance-window updates.", "Maintenance"),
    ToolSpec("players", "Player Manager", "List, kick, ban and unban players using the official REST API.", "Players"),
    ToolSpec("broadcast", "Broadcast Message", "Send an announcement to all connected players.", "Players"),
    ToolSpec("network", "Network Diagnostics", "Inspect IPs, gateway, NAT indication and listening ports.", "Diagnostics"),
    ToolSpec("diagnostics", "Installation Diagnostics", "Validate paths, service, SteamCMD, config permissions and API.", "Diagnostics"),
    ToolSpec("setup", "Server Setup", "Configure install paths, SteamCMD, service/process mode, ports and REST API credentials.", "Configuration"),
    ToolSpec("connection", "Connection Manager", "Switch between local, direct HTTPS and SSH-tunnel remote mode.", "Remote"),
    ToolSpec("self-update", "Manager Update", "Check or install a PalServer Manager GitHub release.", "Maintenance"),
]
