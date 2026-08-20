from __future__ import annotations

import os
import re
import secrets
import shlex
import shutil
import subprocess
import select
import time
from pathlib import Path
from typing import Any, Callable


ProgressCallback = Callable[[str], None]


def _emit_progress(callback: ProgressCallback | None, message: str) -> None:
    if callback is None:
        return
    try:
        callback(str(message))
    except Exception:
        # Progress reporting must never make a provisioning operation fail.
        pass


def _run_streaming(
    cmd: list[str] | str,
    *,
    check: bool = True,
    timeout: int = 1800,
    progress: ProgressCallback | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command while forwarding combined stdout/stderr line-by-line.

    Host provisioning runs only on Linux, so ``select`` lets us stream output
    without blocking forever on ``readline`` while still enforcing a timeout.
    The returned object mirrors ``subprocess.run`` closely enough for existing
    error handling and tests.
    """
    process = subprocess.Popen(
        cmd,
        shell=isinstance(cmd, str),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        env=env,
    )
    lines: list[str] = []
    started = time.monotonic()
    stream = process.stdout
    try:
        while True:
            if timeout and time.monotonic() - started > timeout:
                process.kill()
                try:
                    process.wait(timeout=5)
                except Exception:
                    pass
                raise TimeoutError(f"Command timed out after {timeout} seconds")
            if stream is not None:
                readable, _, _ = select.select([stream], [], [], 0.25)
                if readable:
                    line = stream.readline()
                    if line:
                        clean = line.rstrip("\r\n")
                        lines.append(clean)
                        if clean:
                            _emit_progress(progress, clean)
                        continue
            if process.poll() is not None:
                if stream is not None:
                    remainder = stream.read() or ""
                    for raw in remainder.splitlines():
                        lines.append(raw)
                        if raw:
                            _emit_progress(progress, raw)
                break
        result = subprocess.CompletedProcess(cmd, int(process.returncode or 0), "\n".join(lines), "")
    finally:
        if stream is not None:
            stream.close()
    if check and result.returncode != 0:
        raise RuntimeError(f"Command exited with status {result.returncode}:\n{_combined_output(result)}")
    return result


def _combined_output(result: subprocess.CompletedProcess[str], limit: int = 12000) -> str:
    """Return useful command diagnostics without hiding stdout behind stderr."""
    chunks: list[str] = []
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if stdout:
        chunks.append(stdout)
    if stderr and stderr != stdout:
        chunks.append(stderr)
    text = "\n".join(chunks).strip() or "command failed"
    return text[-limit:]


def _run(cmd: list[str] | str, *, check: bool = True, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        shell=isinstance(cmd, str),
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"Command exited with status {result.returncode}:\n{_combined_output(result)}")
    return result


def _read_ini_value(path: Path, key: str, default: str = "") -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return default
    # PalWorldSettings.ini stores most values on a single OptionSettings line.
    match = re.search(rf"(?:^|[,\(])\s*{re.escape(key)}\s*=\s*(\"(?:[^\"\\]|\\.)*\"|[^,\)]*)", text)
    if not match:
        return default
    value = match.group(1).strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value


def _service_for_install(install_dir: Path) -> tuple[str, str]:
    candidates = list(Path("/etc/systemd/system").glob("*.service")) + list(Path("/lib/systemd/system").glob("*.service")) + list(Path("/usr/lib/systemd/system").glob("*.service"))
    needle = str(install_dir)
    for unit in candidates:
        try:
            text = unit.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if needle in text and ("PalServer.sh" in text or "PalServer-Linux" in text):
            return unit.stem, text
    return "", ""


def discover_palworld_servers() -> list[dict[str, Any]]:
    """Discover Palworld installs on a Linux agent host without changing them."""
    roots = [Path("/opt"), Path("/srv"), Path("/home")]
    found: dict[str, Path] = {}
    for root in roots:
        if not root.exists():
            continue
        try:
            result = _run(["find", str(root), "-maxdepth", "6", "-type", "f", "-name", "PalServer.sh", "-print"], check=False, timeout=30)
        except Exception:
            continue
        for line in (result.stdout or "").splitlines():
            path = Path(line.strip())
            if path.is_file():
                found[str(path.parent.resolve())] = path.parent.resolve()

    rows: list[dict[str, Any]] = []
    for install_dir in sorted(found.values(), key=lambda p: str(p)):
        config_path = install_dir / "Pal" / "Saved" / "Config" / "LinuxServer" / "PalWorldSettings.ini"
        save_dir = install_dir / "Pal" / "Saved" / "SaveGames"
        service_name, unit_text = _service_for_install(install_dir)
        game_port = 8211
        port_match = re.search(r"(?:^|\s)-port=(\d+)", unit_text)
        if port_match:
            game_port = int(port_match.group(1))
        rest_port = int(_read_ini_value(config_path, "RESTAPIPort", "8212") or 8212)
        server_name = _read_ini_value(config_path, "ServerName", install_dir.name)
        max_players = int(float(_read_ini_value(config_path, "ServerPlayerMaxNum", "32") or 32))
        rows.append({
            "name": server_name,
            "install_dir": str(install_dir),
            "config_path": str(config_path),
            "save_dir": str(save_dir),
            "service_name": service_name,
            "game_port": game_port,
            "rest_api_port": rest_port,
            "max_players": max_players,
            "has_config": config_path.is_file(),
            "has_service": bool(service_name),
        })
    return rows


def _ensure_linux_root() -> None:
    if os.name == "nt" or not Path("/proc").exists():
        raise RuntimeError("Automatic host provisioning currently supports Linux only")
    if os.geteuid() != 0:
        raise PermissionError("The PalServer Manager agent must run as root for automatic game-server installation")


def _steamcmd_path() -> Path:
    for candidate in (Path("/usr/games/steamcmd"), Path("/usr/bin/steamcmd"), Path("/opt/steamcmd/steamcmd.sh")):
        if candidate.exists():
            return candidate
    return Path("/opt/steamcmd/steamcmd.sh")


def _prepare_steamcmd_for_user(path: Path, steam_user: str) -> None:
    """Make a SteamCMD installation executable/writable by the game account.

    SteamCMD self-updates in its own directory.  A manually extracted
    /opt/steamcmd tree can therefore fail with EACCES when the agent later
    launches it through runuser.  Repair only the private /opt/steamcmd tree;
    distro-managed /usr paths keep their package ownership.
    """
    if not path.exists():
        return
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    opt_root = Path("/opt/steamcmd")
    if resolved == opt_root or opt_root in resolved.parents:
        _run(["chown", "-R", f"{steam_user}:{steam_user}", str(opt_root)])
        _run(["chmod", "-R", "u+rwX,go+rX", str(opt_root)])
    # The launcher itself must always be executable.  This is harmless for a
    # package-installed /usr/games/steamcmd and fixes archives extracted with
    # restrictive modes.
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError as exc:
        raise PermissionError(f"SteamCMD exists but could not be made executable: {path}: {exc}") from exc
    if not os.access(path, os.X_OK):
        raise PermissionError(f"SteamCMD is not executable: {path}")


def _steam_user_home(steam_user: str) -> str:
    try:
        import pwd  # POSIX-only; keep host_ops importable on Windows clients.
        return pwd.getpwnam(steam_user).pw_dir or f"/home/{steam_user}"
    except (ImportError, KeyError):
        return f"/home/{steam_user}"


def _steamcmd_as_user(command: list[str], steam_user: str) -> list[str]:
    """Run SteamCMD with the game account's real HOME/environment.

    `runuser -u user -- command` can preserve root's HOME on some systems.
    SteamCMD self-updates and writes user state beneath HOME, so that can make
    an otherwise-correct /opt/steamcmd installation fail after its restart.
    Ubuntu normally has sudo available, where `sudo -u USER -H` gives SteamCMD
    the expected home directory.  The runuser fallback explicitly supplies the
    same environment.
    """
    home = _steam_user_home(steam_user)
    if shutil_which("sudo"):
        return ["sudo", "-u", steam_user, "-H", "--", *command]
    if shutil_which("runuser"):
        return [
            "runuser", "-u", steam_user, "--",
            "env", f"HOME={home}", f"USER={steam_user}", f"LOGNAME={steam_user}",
            *command,
        ]
    raise RuntimeError("Neither sudo nor runuser is available to launch SteamCMD as the Palworld service account")


def _steamcmd_invocation(steamcmd: str, args: list[str], steam_user: str) -> list[str]:
    # The manually downloaded Valve launcher is a shell script.  Invoking it
    # through bash avoids an old/noexec launcher bit while its linux32 binary
    # still executes normally after _prepare_steamcmd_for_user repairs the tree.
    base = ["bash", steamcmd] if Path(steamcmd).name == "steamcmd.sh" else [steamcmd]
    return _steamcmd_as_user([*base, *args], steam_user)


def _run_steamcmd(steamcmd: str, args: list[str], steam_user: str, *, timeout: int = 3600, progress: ProgressCallback | None = None) -> subprocess.CompletedProcess[str]:
    """Run SteamCMD robustly across its first-run self-update/restart.

    Valve's launcher can request a restart while updating itself.  If that
    first invocation exits non-zero immediately after the restart request,
    repair ownership/modes again and retry once.  The complete stdout+stderr
    tail is preserved on failure so the GUI shows the real SteamCMD error.
    """
    command = _steamcmd_invocation(steamcmd, args, steam_user)
    runner = _run_streaming if progress is not None else _run
    result = runner(command, check=False, timeout=timeout, **({"progress": progress} if progress is not None else {}))
    if result.returncode == 0:
        return result
    first_output = _combined_output(result)
    if "Restarting steamcmd by request" in first_output:
        _prepare_steamcmd_for_user(Path(steamcmd), steam_user)
        _emit_progress(progress, "SteamCMD requested a self-update restart; retrying automatically...")
        retry_command = _steamcmd_invocation(steamcmd, args, steam_user)
        result2 = runner(retry_command, check=False, timeout=timeout, **({"progress": progress} if progress is not None else {}))
        if result2.returncode == 0:
            return result2
        second_output = _combined_output(result2)
        raise RuntimeError(
            f"SteamCMD exited with status {result2.returncode} after its self-update restart and one automatic retry.\n"
            f"First attempt:\n{first_output}\n\nRetry:\n{second_output}"
        )
    raise RuntimeError(f"SteamCMD exited with status {result.returncode}:\n{first_output}")


def ensure_steamcmd(steam_user: str = "palworld", progress: ProgressCallback | None = None) -> str:
    _ensure_linux_root()
    existing = _steamcmd_path()
    if existing.exists():
        _emit_progress(progress, f"SteamCMD found at {existing}; checking ownership and permissions...")
        _prepare_steamcmd_for_user(existing, steam_user)
        return str(existing)
    if not Path("/usr/bin/apt-get").exists():
        raise RuntimeError("Automatic SteamCMD installation currently supports Debian/Ubuntu Linux hosts")
    env = dict(os.environ)
    env["DEBIAN_FRONTEND"] = "noninteractive"
    _emit_progress(progress, "SteamCMD is not installed. Refreshing Ubuntu package metadata...")
    result = _run_streaming(["apt-get", "update"], check=False, timeout=900, progress=progress, env=env)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())
    _emit_progress(progress, "Installing SteamCMD prerequisites...")
    result = _run_streaming(["apt-get", "install", "-y", "curl", "ca-certificates", "tar", "lib32gcc-s1"], check=False, timeout=1200, progress=progress, env=env)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())
    Path("/opt/steamcmd").mkdir(parents=True, exist_ok=True)
    archive = Path("/tmp/steamcmd_linux.tar.gz")
    _emit_progress(progress, "Downloading the SteamCMD bootstrap archive from Valve...")
    _run_streaming(["curl", "-fL", "--progress-bar", "-o", str(archive), "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz"], timeout=300, progress=progress)
    _emit_progress(progress, "Extracting SteamCMD to /opt/steamcmd...")
    _run_streaming(["tar", "-xzf", str(archive), "-C", "/opt/steamcmd"], progress=progress)
    existing = _steamcmd_path()
    if not existing.exists():
        raise RuntimeError("SteamCMD download completed but steamcmd.sh was not found")
    _prepare_steamcmd_for_user(existing, steam_user)
    return str(existing)


def update_linux_host() -> dict[str, Any]:
    """Install normal OS package updates without rebooting the host."""
    _ensure_linux_root()
    if not Path("/usr/bin/apt-get").exists():
        raise RuntimeError("Automatic OS updates currently support Debian/Ubuntu Linux hosts")
    env = dict(os.environ)
    env["DEBIAN_FRONTEND"] = "noninteractive"
    for cmd in (["apt-get", "update"], ["apt-get", "upgrade", "-y"]):
        result = subprocess.run(cmd, text=True, capture_output=True, env=env, timeout=3600)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip())
    return {"updated": True, "reboot_required": Path("/var/run/reboot-required").exists()}


def install_palworld_files(
    *,
    instance_id: str,
    name: str,
    install_dir: str,
    service_name: str,
    game_port: int,
    rest_api_port: int,
    max_players: int = 32,
    steam_user: str = "palworld",
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Install Palworld dedicated-server files and a systemd service.

    The service is created but not started. The caller should register the
    instance, configure the REST API/password, then enable/start it.
    """
    _emit_progress(progress, "Beginning Palworld dedicated-server installation...")
    _ensure_linux_root()
    if not Path("/run/systemd/system").exists():
        raise RuntimeError("Automatic Palworld installation currently requires systemd")
    install = Path(install_dir)
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", service_name):
        raise ValueError("Service name contains unsupported characters")
    if not (1 <= int(game_port) <= 65535 and 1 <= int(rest_api_port) <= 65535):
        raise ValueError("Game and REST API ports must be between 1 and 65535")
    if int(game_port) == int(rest_api_port):
        raise ValueError("Game and REST API ports must be different")

    # Dedicated low-privilege game account. The management agent itself is
    # root, but PalServer should not run as root.
    _emit_progress(progress, f"Checking dedicated Linux service account: {steam_user}...")
    if _run(["id", "-u", steam_user], check=False).returncode != 0:
        _emit_progress(progress, f"Creating low-privilege service account {steam_user}...")
        _run(["useradd", "--create-home", "--shell", "/bin/bash", steam_user])
    # Prepare SteamCMD only after the service account exists so an existing
    # /opt/steamcmd install can have its ownership/execute bits repaired.
    _emit_progress(progress, "Preparing SteamCMD...")
    steamcmd = ensure_steamcmd(steam_user, progress=progress)
    install.mkdir(parents=True, exist_ok=True)
    _run(["chown", "-R", f"{steam_user}:{steam_user}", str(install)])

    # First let SteamCMD complete its own bootstrap/update in a clean user
    # environment.  Keeping this separate from the Palworld app install makes
    # the common first-run restart deterministic and gives much clearer errors.
    _emit_progress(progress, "Running SteamCMD bootstrap/self-update...")
    _run_steamcmd(steamcmd, ["+quit"], steam_user, timeout=900, progress=progress)
    _prepare_steamcmd_for_user(Path(steamcmd), steam_user)
    _emit_progress(progress, "Downloading/updating Palworld Dedicated Server (Steam app 2394010). This can take several minutes...")
    _run_steamcmd(
        steamcmd,
        [
            "+force_install_dir", str(install),
            "+login", "anonymous",
            "+app_update", "2394010", "validate",
            "+quit",
        ],
        steam_user,
        timeout=3600,
        progress=progress,
    )
    _emit_progress(progress, "SteamCMD reports the Palworld app install/update completed.")

    palserver = install / "PalServer.sh"
    default_ini = install / "DefaultPalWorldSettings.ini"
    config_path = install / "Pal" / "Saved" / "Config" / "LinuxServer" / "PalWorldSettings.ini"
    save_dir = install / "Pal" / "Saved" / "SaveGames"
    if not palserver.exists():
        raise RuntimeError(f"Palworld install completed but {palserver} was not found")
    _emit_progress(progress, "Preparing Palworld configuration and save directories...")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    save_dir.mkdir(parents=True, exist_ok=True)
    if default_ini.exists() and not config_path.exists():
        config_path.write_bytes(default_ini.read_bytes())
    _run(["chown", "-R", f"{steam_user}:{steam_user}", str(install)])

    unit = f"""[Unit]\nDescription=Palworld Dedicated Server ({name})\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\nUser={steam_user}\nGroup={steam_user}\nWorkingDirectory={install}\nExecStart={palserver} -port={int(game_port)} -players={int(max_players)}\nRestart=on-failure\nRestartSec=5\nLimitNOFILE=100000\n\n[Install]\nWantedBy=multi-user.target\n"""
    _emit_progress(progress, f"Creating systemd service {service_name}.service...")
    unit_path = Path("/etc/systemd/system") / f"{service_name}.service"
    unit_path.write_text(unit, encoding="utf-8")
    _run(["systemctl", "daemon-reload"])
    _run(["systemctl", "enable", service_name])
    _emit_progress(progress, "Palworld files and systemd service are ready. Finalizing manager configuration...")

    return {
        "instance_id": instance_id,
        "name": name,
        "server": {
            "install_dir": str(install),
            "steamcmd_path": steamcmd,
            "steam_user": steam_user,
            "service_name": service_name,
            "config_path": str(config_path),
            "save_dir": str(save_dir),
            "backup_dir": str(Path("/opt") / f"palworld-backups-{instance_id}"),
            "log_file": str(Path("/var/log/palserver-manager") / f"palserver-{instance_id}.log"),
            "rest_api_host": "127.0.0.1",
            "rest_api_port": int(rest_api_port),
            "rest_api_username": "admin",
            "admin_password": secrets.token_urlsafe(24),
            "game_port": int(game_port),
        },
    }



def _safe_remove_tree(path: str, label: str) -> Path:
    target = Path(path).expanduser()
    if not target.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    resolved = target.resolve(strict=False)
    protected = {Path("/"), Path("/opt"), Path("/srv"), Path("/home"), Path("/usr"), Path("/etc"), Path("/var")}
    if resolved in protected or len(resolved.parts) < 3:
        raise ValueError(f"Refusing to remove unsafe {label}: {resolved}")
    return resolved


def uninstall_palworld_files(*, install_dir: str, service_name: str) -> dict[str, Any]:
    """Remove one Palworld installation and its systemd unit.

    Shared SteamCMD files, the Linux service account, manager backups, and
    manager logs are deliberately preserved.  Callers should create a final
    backup before invoking this destructive operation.
    """
    _ensure_linux_root()
    install = _safe_remove_tree(install_dir, "Palworld install directory")
    if service_name and not re.fullmatch(r"[A-Za-z0-9_.@-]+", service_name):
        raise ValueError("Service name contains unsupported characters")

    removed_unit = False
    if service_name and Path("/run/systemd/system").exists():
        _run(["systemctl", "stop", service_name], check=False, timeout=120)
        _run(["systemctl", "disable", service_name], check=False, timeout=120)
        unit_path = Path("/etc/systemd/system") / f"{service_name}.service"
        if unit_path.exists():
            unit_path.unlink()
            removed_unit = True
        # Remove manager/runtime drop-ins too.  Leaving an old UE4SS
        # Environment/ExecStart override behind would affect a future server
        # that reuses the same service name after this installation is gone.
        dropin_dir = Path("/etc/systemd/system") / f"{service_name}.service.d"
        if dropin_dir.exists():
            shutil.rmtree(dropin_dir)
        _run(["systemctl", "daemon-reload"], check=False, timeout=120)
        _run(["systemctl", "reset-failed", service_name], check=False, timeout=60)

    removed_install = False
    if install.exists():
        shutil.rmtree(install)
        removed_install = True
    return {
        "uninstalled": True,
        "install_dir": str(install),
        "service_name": service_name,
        "removed_install_dir": removed_install,
        "removed_service_unit": removed_unit,
        "steamcmd_preserved": True,
        "backups_preserved": True,
    }

def shutil_which(name: str) -> str | None:
    # Local helper avoids importing the whole shutil module in hot agent paths.
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(folder) / name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None
