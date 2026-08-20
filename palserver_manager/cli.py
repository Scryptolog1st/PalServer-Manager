from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from . import APP_NAME, __version__
from .config import config_path, load_config, save_config
from .remote import manager_from_config
from .self_update import SelfUpdater
from .tools import TOOLS, ToolSpec


console = Console()


def fmt_bytes(value: int | float | None) -> str:
    if value is None:
        return "-"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit not in {"B"} else f"{size:.0f} B"
        size /= 1024
    return f"{size:.1f} TiB"


def print_json(data: Any) -> None:
    console.print_json(json.dumps(data, default=str))


class InteractiveCLI:
    def __init__(self):
        self.cfg = load_config()
        self.manager = manager_from_config(self.cfg)
        self.page = 0

    def close(self):
        if hasattr(self.manager, "close"):
            try:
                self.manager.close()
            except Exception:
                pass

    def reconnect(self):
        self.close()
        self.cfg = load_config()
        self.manager = manager_from_config(self.cfg)

    def header(self):
        try:
            status = self.manager.status(False)
            health = self.manager.health()
            service = status.get("service", {})
            state = str(service.get("state", "unknown"))
            color = "green" if state in {"active", "running"} else "red"
            health_state = str(health.get("overall", "unknown"))
            health_color = {"healthy": "green", "warning": "yellow", "critical": "red"}.get(health_state, "white")
            build = status.get("build", {})
            lines = [
                f"[bold]{status.get('server_name', 'Palworld Server')}[/bold]   [{color}]{state.upper()}[/{color}]   Health: [{health_color}]{health_state.upper()}[/{health_color}]",
                f"Host: {status.get('host', {}).get('hostname', '-')} ({status.get('host', {}).get('os', '-')})   LAN: {status.get('lan_ip', '-')}:{status.get('game_port', '-')}/UDP   Socket: {'Listening' if status.get('udp_listening') else 'Not listening'}",
                f"Players: {status.get('current_players', '-')}/{status.get('max_players', '-')}   FPS: {status.get('server_fps', '-')}   Build: {build.get('installed') or '-'}   Mode: {self.cfg.connection.mode}",
            ]
        except Exception as exc:
            lines = [f"[red]Unable to load server status: {exc}[/red]", f"Connection mode: {self.cfg.connection.mode}"]
        console.print(Panel("\n".join(lines), title=f"{APP_NAME} v{__version__}", expand=True))

    def run(self):
        try:
            while True:
                console.clear()
                self.header()
                page_size = max(4, int(self.cfg.page_size))
                pages = max(1, (len(TOOLS) + page_size - 1) // page_size)
                self.page = min(self.page, pages - 1)
                start = self.page * page_size
                visible = TOOLS[start:start + page_size]
                table = Table(title=f"Tools - page {self.page + 1}/{pages}", show_lines=False)
                table.add_column("#", style="cyan", width=4)
                table.add_column("Tool", style="bold")
                table.add_column("Category", style="magenta")
                table.add_column("Description")
                for index, tool in enumerate(visible, 1):
                    table.add_row(str(index), tool.name, tool.category, tool.description)
                console.print(table)
                console.print("[dim][N] Next page   [P] Previous page   [R] Refresh   [G] Windows/Linux GUI   [Q] Quit[/dim]")
                choice = Prompt.ask("Select tool").strip().lower()
                if choice == "q":
                    break
                if choice == "n":
                    self.page = (self.page + 1) % pages
                    continue
                if choice == "p":
                    self.page = (self.page - 1) % pages
                    continue
                if choice == "r":
                    continue
                if choice == "g":
                    from .gui import main as gui_main
                    gui_main()
                    continue
                if choice.isdigit() and 1 <= int(choice) <= len(visible):
                    self.execute(visible[int(choice) - 1])
        finally:
            self.close()

    def pause(self):
        Prompt.ask("\nPress Enter to continue", default="")

    def execute(self, tool: ToolSpec):
        console.clear()
        console.rule(f"[bold cyan]{tool.name}")
        try:
            method = getattr(self, f"tool_{tool.id.replace('-', '_')}")
            method()
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            console.print(f"[bold red]Error:[/bold red] {exc}")
        self.pause()

    def tool_dashboard(self):
        status = self.manager.status(True)
        health = self.manager.health()
        print_json({"status": status, "health": health})

    def tool_watchdog(self):
        console.print("Live watchdog. Press Ctrl+C to return.")
        try:
            while True:
                console.clear()
                self.header()
                health = self.manager.health()
                status = self.manager.status(False)
                table = Table(title="Live Host / Palworld Metrics")
                table.add_column("Metric")
                table.add_column("Value")
                table.add_row("CPU", f"{health.get('cpu_percent', 0):.1f}%")
                table.add_row("RAM", f"{fmt_bytes(health.get('memory_used'))} / {fmt_bytes(health.get('memory_total'))} ({health.get('memory_percent', 0):.1f}%)")
                table.add_row("Storage", f"{fmt_bytes(health.get('disk_used'))} / {fmt_bytes(health.get('disk_total'))} ({health.get('disk_percent', 0):.1f}%)")
                table.add_row("Players", f"{status.get('current_players', '-')}/{status.get('max_players', '-')}")
                table.add_row("Server FPS", str(status.get("server_fps", "-")))
                table.add_row("PalServer RAM", fmt_bytes(status.get("process", {}).get("rss")))
                table.add_row("PalServer CPU", f"{status.get('process', {}).get('cpu_percent', 0):.1f}%")
                console.print(table)
                console.print(Panel("\n".join(self.manager.logs_tail(16)), title="Palworld Console / Logs"))
                time.sleep(2)
        except KeyboardInterrupt:
            return

    def tool_health(self):
        data = self.manager.health()
        table = Table(title=f"Overall: {data.get('overall', 'unknown').upper()}")
        table.add_column("Check")
        table.add_column("State")
        table.add_column("Value")
        for row in data.get("checks", []):
            table.add_row(str(row.get("name")), str(row.get("state")), f"{row.get('value')} {row.get('unit', '')}".strip())
        console.print(table)

    def tool_logs(self):
        lines = IntPrompt.ask("Number of lines", default=100)
        errors = Confirm.ask("Errors/warnings only?", default=False)
        console.print(Panel("\n".join(self.manager.logs_tail(lines, errors)), title="Logs"))

    def tool_crashes(self):
        print_json(self.manager.crash_summary())

    def _settings_table(self, rows: list[dict]):
        table = Table(title=f"Settings ({len(rows)})")
        table.add_column("#", width=5)
        table.add_column("Setting")
        table.add_column("Value")
        table.add_column("Category")
        table.add_column("Description", overflow="fold")
        for idx, row in enumerate(rows, 1):
            table.add_row(str(idx), row["key"], row["display_value"], row["category"], row["description"])
        console.print(table)

    def tool_settings(self):
        query = Prompt.ask("Filter (blank = all)", default="")
        rows = self.manager.settings(query)
        self._settings_table(rows)
        selection = Prompt.ask("Setting number to edit (blank = cancel)", default="")
        if not selection.isdigit() or not 1 <= int(selection) <= len(rows):
            return
        row = rows[int(selection) - 1]
        value = Prompt.ask(f"New value for {row['key']}")
        if Confirm.ask(f"Save {row['key']}={value}?", default=False):
            print_json(self.manager.set_setting(row["key"], value))
            console.print("[yellow]Restart Palworld for settings that are only loaded at startup.[/yellow]")

    def tool_search(self):
        query = Prompt.ask("Search settings")
        self._settings_table(self.manager.settings(query))

    def tool_compare(self):
        rows = self.manager.compare_defaults()
        table = Table(title=f"Non-default Settings ({len(rows)})")
        table.add_column("Setting")
        table.add_column("Default")
        table.add_column("Current")
        for row in rows:
            table.add_row(row["key"], row["default"], row["current"])
        console.print(table)

    def tool_profiles(self):
        profiles = self.manager.profiles_list()
        names = list(profiles)
        for i, name in enumerate(names, 1):
            console.print(f"{i}) {name} ({len(profiles[name])} overrides)")
        selection = IntPrompt.ask("Apply profile number", default=0)
        if 1 <= selection <= len(names) and Confirm.ask(f"Apply {names[selection - 1]}?", default=False):
            print_json(self.manager.profile_apply(names[selection - 1]))

    def tool_start(self):
        print_json(self.manager.service_action("start"))

    def tool_stop(self):
        if Confirm.ask("Stop the server and disconnect players?", default=False):
            print_json(self.manager.service_action("stop"))

    def tool_restart(self):
        if Confirm.ask("Restart the server?", default=False):
            print_json(self.manager.service_action("restart"))

    def tool_save(self):
        print_json(self.manager.save_world())

    def tool_update_check(self):
        console.print("Checking Steam public branch...")
        print_json(self.manager.update_check())

    def tool_update(self):
        if Confirm.ask("Backup, update/validate the Palworld server and restart it?", default=False):
            print_json(self.manager.update_server(True, True))

    def tool_backups(self):
        while True:
            console.print("\n1) Create backup\n2) List backups\n3) Restore backup\n4) Delete backup\n0) Back")
            choice = Prompt.ask("Choice", default="0")
            if choice == "0":
                return
            if choice == "1":
                print_json(self.manager.backup_create("manual"))
            elif choice in {"2", "3", "4"}:
                rows = self.manager.backup_list()
                table = Table(title="Backups")
                table.add_column("#")
                table.add_column("Name")
                table.add_column("Size")
                for i, row in enumerate(rows, 1):
                    table.add_row(str(i), row["name"], fmt_bytes(row["size"]))
                console.print(table)
                if choice == "2":
                    continue
                idx = IntPrompt.ask("Backup number", default=0)
                if not 1 <= idx <= len(rows):
                    continue
                name = rows[idx - 1]["name"]
                if choice == "3" and Confirm.ask(f"Restore {name}? Current server data will be replaced.", default=False):
                    print_json(self.manager.backup_restore(name))
                if choice == "4" and Confirm.ask(f"Delete {name}?", default=False):
                    print_json(self.manager.backup_delete(name))

    def tool_worlds(self):
        rows = self.manager.world_list()
        table = Table(title="Worlds")
        table.add_column("#")
        table.add_column("GUID")
        table.add_column("Size")
        table.add_column("Modified")
        table.add_column("WorldOption")
        for i, row in enumerate(rows, 1):
            table.add_row(str(i), row["guid"], fmt_bytes(row["size"]), datetime.fromtimestamp(float(row["modified"])).strftime("%Y-%m-%d %H:%M:%S"), "Yes" if row.get("has_world_option") else "No")
        console.print(table)
        console.print("A) Archive selected   D) Delete selected   N) Create fresh world   Enter) Back")
        action = Prompt.ask("Action", default="").lower()
        if action in {"a", "d"}:
            idx = IntPrompt.ask("World number", default=0)
            if not 1 <= idx <= len(rows):
                return
            guid = rows[idx - 1]["guid"]
            if action == "a":
                print_json(self.manager.world_archive(guid))
            elif Confirm.ask(f"Delete world {guid}? An archive will be created first.", default=False):
                print_json(self.manager.world_delete(guid))
        elif action == "n":
            if Confirm.ask("Archive all current worlds, clear the active world directory, and let Palworld create a fresh world on next start?", default=False):
                print_json(self.manager.world_new())

    def tool_scheduler(self):
        current = self.manager.scheduler_config()
        print_json(current)
        if not Confirm.ask("Change automation settings?", default=False):
            return
        interval = IntPrompt.ask("Backup interval in minutes", default=int(current["backup"]["interval_minutes"]))
        retention = IntPrompt.ask("Backup retention count", default=int(current["backup"]["retention_count"]))
        auto_update = Confirm.ask("Automatically install available updates in maintenance window?", default=bool(current["updates"]["auto_install"]))
        payload = {
            "backup": {"enabled": True, "interval_minutes": interval, "retention_count": retention},
            "updates": {"auto_install": auto_update},
        }
        print_json(self.manager.scheduler_update(payload))

    def tool_players(self):
        rows = self.manager.players()
        table = Table(title="Connected Players")
        table.add_column("#")
        table.add_column("Name")
        table.add_column("User ID")
        table.add_column("Level")
        table.add_column("Ping")
        table.add_column("IP")
        for i, row in enumerate(rows, 1):
            table.add_row(str(i), str(row.get("name", "")), str(row.get("userId", "")), str(row.get("level", "")), str(row.get("ping", "")), str(row.get("ip", "")))
        console.print(table)
        action = Prompt.ask("Action: [K]ick [B]an [U]nban [Enter] back", default="").lower()
        if action not in {"k", "b", "u"}:
            return
        if action == "u":
            user_id = Prompt.ask("User ID to unban")
            print_json(self.manager.player_action("unban", user_id))
            return
        idx = IntPrompt.ask("Player number", default=0)
        if not 1 <= idx <= len(rows):
            return
        user_id = str(rows[idx - 1].get("userId", ""))
        message = Prompt.ask("Message", default="Kicked by administrator" if action == "k" else "Banned by administrator")
        print_json(self.manager.player_action("kick" if action == "k" else "ban", user_id, message))

    def tool_broadcast(self):
        message = Prompt.ask("Broadcast message")
        print_json(self.manager.announce(message))

    def tool_network(self):
        console.print("Running network diagnostics (includes an optional public-IP lookup)...")
        print_json(self.manager.network_diagnostics(True))

    def tool_diagnostics(self):
        print_json(self.manager.diagnostics())

    def tool_setup(self):
        current = self.manager.server_config()
        console.print(f"Connection mode: {self.cfg.connection.mode}")
        payload = {
            "install_dir": Prompt.ask("Palworld install directory", default=str(current.get("install_dir", ""))),
            "steamcmd_path": Prompt.ask("SteamCMD path", default=str(current.get("steamcmd_path", ""))),
            "steam_user": Prompt.ask("Linux Steam/Palworld user", default=str(current.get("steam_user", "palworld"))),
            "service_name": Prompt.ask("OS service name (blank uses direct process fallback where supported)", default=str(current.get("service_name", ""))),
            "game_port": IntPrompt.ask("Game listen port", default=int(current.get("game_port", 8211))),
            "rest_api_host": Prompt.ask("Palworld REST API host", default=str(current.get("rest_api_host", "127.0.0.1"))),
            "rest_api_port": IntPrompt.ask("Palworld REST API port", default=int(current.get("rest_api_port", 8212))),
            "rest_api_username": Prompt.ask("REST API username", default=str(current.get("rest_api_username", "admin"))),
        }
        password = Prompt.ask("Admin password (blank keeps current)", default="", password=True)
        if password:
            payload["admin_password"] = password
        payload["sync_palworld_rest"] = Confirm.ask("Enable/sync RESTAPIEnabled, RESTAPIPort and AdminPassword in PalWorldSettings.ini?", default=True)
        result = self.manager.update_server_config(payload)
        print_json(result)
        if self.cfg.connection.mode == "local":
            # Reload local user config in case paths were changed.
            self.cfg = load_config()
            self.reconnect()
        console.print("[green]Server configuration saved.[/green]")

    def tool_connection(self):
        console.print(f"Current mode: [bold]{self.cfg.connection.mode}[/bold]")
        console.print("1) Local\n2) Direct HTTPS remote agent\n3) SSH tunnel to remote loopback agent")
        choice = Prompt.ask("Mode", choices=["1", "2", "3"], default="1")
        if choice == "1":
            self.cfg.connection.mode = "local"
        elif choice == "2":
            self.cfg.connection.mode = "direct"
            self.cfg.connection.remote_url = Prompt.ask("Agent URL", default=self.cfg.connection.remote_url)
            self.cfg.connection.remote_token = Prompt.ask("Agent token", password=True)
            self.cfg.connection.verify_tls = Confirm.ask("Verify TLS certificate?", default=True)
        else:
            self.cfg.connection.mode = "ssh"
            self.cfg.connection.ssh_host = Prompt.ask("SSH host", default=self.cfg.connection.ssh_host)
            self.cfg.connection.ssh_user = Prompt.ask("SSH user", default=self.cfg.connection.ssh_user)
            self.cfg.connection.ssh_port = IntPrompt.ask("SSH port", default=self.cfg.connection.ssh_port)
            self.cfg.connection.ssh_key_file = Prompt.ask("SSH private key path (blank = ssh-agent/default)", default=self.cfg.connection.ssh_key_file)
            self.cfg.connection.remote_token = Prompt.ask("Agent token", password=True)
        save_config(self.cfg)
        self.reconnect()
        console.print(f"[green]Connection mode saved in {config_path()}[/green]")

    def tool_self_update(self):
        updater = SelfUpdater(self.cfg)
        check = updater.check()
        print_json(check)
        if check.get("state") == "available" and Confirm.ask("Install the latest manager release?", default=False):
            print_json(updater.install_latest())


def run_command(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.remote_url:
        cfg.connection.mode = "direct"
        cfg.connection.remote_url = args.remote_url
        cfg.connection.remote_token = args.token or cfg.connection.remote_token
    manager = manager_from_config(cfg)
    try:
        if args.command == "status":
            print_json(manager.status(args.update))
        elif args.command == "health":
            print_json(manager.health())
        elif args.command in {"start", "stop", "restart"}:
            print_json(manager.service_action(args.command))
        elif args.command == "update":
            print_json(manager.update_server(True, True))
        elif args.command == "backup":
            print_json(manager.backup_create("cli"))
        elif args.command == "logs":
            console.print("\n".join(manager.logs_tail(args.lines, args.errors_only)))
        elif args.command == "players":
            print_json(manager.players())
        elif args.command == "gui":
            from .gui import main as gui_main
            gui_main()
        else:
            InteractiveCLI().run()
    finally:
        if hasattr(manager, "close"):
            manager.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} v{__version__}")
    parser.add_argument("--remote-url", help="Use a remote HTTPS agent for this command")
    parser.add_argument("--token", help="Remote agent token")
    sub = parser.add_subparsers(dest="command")
    p_status = sub.add_parser("status")
    p_status.add_argument("--update", action="store_true")
    sub.add_parser("health")
    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("restart")
    sub.add_parser("update")
    sub.add_parser("backup")
    p_logs = sub.add_parser("logs")
    p_logs.add_argument("--lines", type=int, default=100)
    p_logs.add_argument("--errors-only", action="store_true")
    sub.add_parser("players")
    sub.add_parser("gui")
    args = parser.parse_args()
    raise SystemExit(run_command(args))


if __name__ == "__main__":
    main()
