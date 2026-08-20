from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import psutil

from .config import AppConfig, user_data_dir


class ServiceController:
    def status(self) -> dict:
        raise NotImplementedError

    def start(self) -> dict:
        raise NotImplementedError

    def stop(self) -> dict:
        raise NotImplementedError

    def restart(self) -> dict:
        self.stop()
        time.sleep(1)
        return self.start()


class LinuxSystemdController(ServiceController):
    def __init__(self, service_name: str):
        self.name = service_name

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["systemctl", *args], text=True, capture_output=True)


    def installed(self) -> bool:
        result = self._run("show", self.name, "--property=LoadState", "--value")
        return result.returncode == 0 and (result.stdout or "").strip() not in {"", "not-found"}
    def status(self) -> dict:
        active = self._run("is-active", self.name)
        enabled = self._run("is-enabled", self.name)
        pid = self._run("show", self.name, "--property=MainPID", "--value")
        started = self._run("show", self.name, "--property=ActiveEnterTimestamp", "--value")
        try:
            pid_value = int((pid.stdout or "0").strip())
        except ValueError:
            pid_value = 0
        return {
            "service": self.name,
            "state": (active.stdout or "unknown").strip(),
            "enabled": (enabled.stdout or "unknown").strip(),
            "pid": pid_value,
            "started": (started.stdout or "").strip(),
        }

    def start(self) -> dict:
        result = self._run("start", self.name)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"Failed to start {self.name}")
        time.sleep(0.5)
        return self.status()

    def stop(self) -> dict:
        result = self._run("stop", self.name)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"Failed to stop {self.name}")
        time.sleep(0.5)
        return self.status()


class WindowsServiceController(ServiceController):
    def __init__(self, service_name: str):
        self.name = service_name

    def _ps(self, command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            text=True,
            capture_output=True,
        )

    def status(self) -> dict:
        escaped = self.name.replace("'", "''")
        script = (
            f"$s=Get-CimInstance Win32_Service -Filter \"Name='{escaped}'\" -ErrorAction SilentlyContinue; "
            "if($null -eq $s){exit 3}; "
            "$o=[pscustomobject]@{state=$s.State;startMode=$s.StartMode;pid=$s.ProcessId}; "
            "$o|ConvertTo-Json -Compress"
        )
        result = self._ps(script)
        if result.returncode == 3 or not result.stdout.strip():
            return {"service": self.name, "state": "not-installed", "enabled": "unknown", "pid": 0, "started": ""}
        import json
        data = json.loads(result.stdout)
        return {
            "service": self.name,
            "state": str(data.get("state", "unknown")).lower(),
            "enabled": str(data.get("startMode", "unknown")).lower(),
            "pid": int(data.get("pid") or 0),
            "started": "",
        }

    def start(self) -> dict:
        result = self._ps(f"Start-Service -Name '{self.name.replace(chr(39), chr(39)*2)}' -ErrorAction Stop")
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"Failed to start {self.name}")
        time.sleep(0.8)
        return self.status()

    def stop(self) -> dict:
        result = self._ps(f"Stop-Service -Name '{self.name.replace(chr(39), chr(39)*2)}' -Force -ErrorAction Stop")
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"Failed to stop {self.name}")
        time.sleep(0.8)
        return self.status()


class DirectProcessController(ServiceController):
    """Fallback for hosts where PalServer is not registered as an OS service."""

    def __init__(self, cfg: AppConfig, instance_id: str = "default"):
        self.cfg = cfg
        self.instance_id = str(instance_id or "default")
        self.pid_file = user_data_dir() / f"palserver-{self.instance_id}.pid"
        self.log_file = Path(cfg.server.log_file)

    def _pid(self) -> int:
        try:
            pid = int(self.pid_file.read_text().strip())
            if psutil.pid_exists(pid):
                return pid
        except Exception:
            pass
        # Adopt an already-running PalServer process so the manager does not
        # accidentally launch a duplicate when the server was started manually.
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = (proc.info.get("name") or "").lower()
                cmdline = " ".join(proc.info.get("cmdline") or []).lower()
                install_hint = str(Path(self.cfg.server.install_dir)).lower().rstrip("/\\")
                looks_like_palserver = name in {"palserver.exe", "palserver-linux-test", "palserver-linux-shipping"} or "palserver.exe" in cmdline or "/palserver.sh" in cmdline or "palserver-linux-shipping" in cmdline
                if looks_like_palserver and (not install_hint or install_hint in cmdline):
                    self.pid_file.parent.mkdir(parents=True, exist_ok=True)
                    self.pid_file.write_text(str(proc.info["pid"]), encoding="utf-8")
                    return int(proc.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        return 0

    def status(self) -> dict:
        pid = self._pid()
        return {
            "service": "direct-process",
            "state": "active" if pid else "inactive",
            "enabled": "manual",
            "pid": pid,
            "started": "",
        }

    def _executable(self) -> Path:
        root = Path(self.cfg.server.install_dir)
        return root / ("PalServer.exe" if os.name == "nt" else "PalServer.sh")

    def start(self) -> dict:
        if self._pid():
            return self.status()
        exe = self._executable()
        if not exe.exists():
            raise FileNotFoundError(exe)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        log = open(self.log_file, "a", encoding="utf-8")
        kwargs = dict(cwd=str(exe.parent), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
            # Direct-process Linux instances honor the same per-server UE4SS
            # marker used by systemd-managed instances. This keeps mod runtime
            # selection scoped to one Palworld process instead of exporting a
            # machine-wide LD_PRELOAD.
            stage = Path(self.cfg.server.install_dir) / "Pal" / "Binaries" / "Linux"
            marker = stage / ".palserver-manager-ue4ss-enabled"
            runtime = stage / "libUE4SS.so"
            if marker.exists() and runtime.exists():
                env = dict(os.environ)
                existing = env.get("LD_PRELOAD", "").strip()
                env["LD_PRELOAD"] = str(runtime) + ((":" + existing) if existing else "")
                env["UE4SS_CRASH_LOG_DIR"] = str(stage / "UE4SS-crashes")
                kwargs["env"] = env
        args = list(self.cfg.server.launch_args)
        if not any(str(arg).startswith("-port=") for arg in args):
            args.insert(0, f"-port={self.cfg.server.game_port}")
        process = subprocess.Popen([str(exe), *args], **kwargs)
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.pid_file.write_text(str(process.pid), encoding="utf-8")
        time.sleep(1)
        return self.status()

    def stop(self) -> dict:
        pid = self._pid()
        if not pid:
            return self.status()
        try:
            proc = psutil.Process(pid)
            if os.name == "nt":
                proc.terminate()
            else:
                os.killpg(os.getpgid(pid), signal.SIGINT)
            proc.wait(timeout=30)
        except psutil.TimeoutExpired:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
        self.pid_file.unlink(missing_ok=True)
        return self.status()


def service_controller(cfg: AppConfig, instance_id: str = "default") -> ServiceController:
    if os.name == "nt":
        controller = WindowsServiceController(cfg.server.service_name)
        if controller.status()["state"] != "not-installed":
            return controller
        return DirectProcessController(cfg, instance_id)
    if Path("/run/systemd/system").exists() and cfg.server.service_name:
        controller = LinuxSystemdController(cfg.server.service_name)
        if controller.installed():
            return controller
    return DirectProcessController(cfg, instance_id)
