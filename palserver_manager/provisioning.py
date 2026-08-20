from __future__ import annotations

import json
import os
import queue
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__


class ProvisioningError(RuntimeError):
    pass


def find_free_local_port(start: int = 18765, stop: int = 19765) -> int:
    for port in range(start, stop + 1):
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", port))
            return port
        except OSError:
            pass
        finally:
            sock.close()
    raise ProvisioningError("No free local SSH tunnel port was found")


class LinuxHostBootstrapper:
    """Install/upgrade the loopback agent over an existing SSH key login.

    Passwords are deliberately not stored. Automatic bootstrap currently
    requires public-key SSH and root or passwordless sudo on the target host.
    """

    def __init__(self, host: str, user: str, port: int = 22, key_file: str = ""):
        self.host = str(host).strip()
        self.user = str(user).strip()
        self.port = int(port)
        self.key_file = str(key_file).strip()
        if not self.host or not self.user:
            raise ValueError("SSH host and user are required")
        if not self.key_file or not Path(self.key_file).is_file():
            raise ValueError("A valid SSH private key is required for automatic provisioning")
        self.ssh = shutil.which("ssh")
        self.scp = shutil.which("scp")
        if not self.ssh or not self.scp:
            raise FileNotFoundError("OpenSSH ssh/scp were not found on this computer")

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"

    def _base_ssh(self) -> list[str]:
        return [
            self.ssh, "-T", "-p", str(self.port), "-i", self.key_file,
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=12",
            "-o", "StrictHostKeyChecking=accept-new", self.target,
        ]

    @staticmethod
    def _emit_progress(progress, message: str) -> None:
        if progress is None:
            return
        try:
            progress(str(message))
        except Exception:
            # Provisioning must never fail because a UI/log consumer failed.
            pass

    def _run_process(self, command: list[str], timeout: int, progress=None, error_label: str = "Command failed") -> str:
        """Run a local ssh/scp process and optionally stream merged output.

        A small reader thread keeps this portable on Windows while still
        allowing the worker to enforce a real timeout when the remote command
        is quiet for a long time.
        """
        kwargs: dict[str, Any] = {
            "text": True,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "bufsize": 1,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(command, **kwargs)
        output_queue: queue.Queue[str | None] = queue.Queue()

        def read_output():
            try:
                if proc.stdout is not None:
                    for raw in iter(proc.stdout.readline, ""):
                        output_queue.put(raw)
            finally:
                output_queue.put(None)

        threading.Thread(target=read_output, daemon=True).start()
        lines: list[str] = []
        reader_done = False
        deadline = time.monotonic() + max(1, int(timeout))
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    proc.kill()
                    raise ProvisioningError(f"{error_label}: timed out after {timeout} seconds")
                try:
                    item = output_queue.get(timeout=min(0.20, remaining))
                except queue.Empty:
                    item = ...
                if item is None:
                    reader_done = True
                elif item is not ...:
                    # Normalize carriage-return progress output into readable
                    # console lines without exposing any locally generated
                    # credentials.
                    for part in str(item).replace("\r", "\n").splitlines():
                        line = part.rstrip()
                        if not line:
                            continue
                        lines.append(line)
                        self._emit_progress(progress, line)
                if reader_done and proc.poll() is not None:
                    break
            rc = proc.wait(timeout=2)
        finally:
            if proc.poll() is None:
                proc.kill()
        output = "\n".join(lines).strip()
        if rc != 0:
            raise ProvisioningError(output or error_label)
        return output

    def run(self, command: str, timeout: int = 120, progress=None) -> str:
        cmd = [*self._base_ssh(), command]
        if progress is None:
            # Keep stdout and stderr separate for commands whose stdout is
            # machine-parsed (hostname, UID, token, systemd state, etc.).
            # SSH host-key warnings are written to stderr and must not corrupt
            # those values.
            kwargs: dict[str, Any] = {"text": True, "capture_output": True, "timeout": timeout}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(cmd, **kwargs)
            if result.returncode != 0:
                raise ProvisioningError((result.stderr or result.stdout or "SSH command failed").strip())
            return (result.stdout or "").strip()
        return self._run_process(cmd, timeout, progress=progress, error_label="SSH command failed")

    def test_connection(self) -> dict[str, Any]:
        raw = self.run("printf '%s\\n' \"$(uname -s)\" \"$(hostname)\" \"$(id -u)\"; command -v sudo || true", 30)
        lines = raw.splitlines()
        if not lines or lines[0].strip() != "Linux":
            raise ProvisioningError("Automatic provisioning currently supports Linux hosts only")
        uid = int(lines[2].strip()) if len(lines) > 2 and lines[2].strip().isdigit() else -1
        if uid != 0:
            self.run("sudo -n true", 15)
        return {"os": "Linux", "hostname": lines[1].strip() if len(lines) > 1 else self.host, "uid": uid}

    def _sudo(self) -> str:
        uid = self.run("id -u", 15).strip()
        return "" if uid == "0" else "sudo -n "

    def restart_agent(self, progress=None) -> dict[str, Any]:
        """Restart an already-provisioned agent through the host SSH channel."""
        info = self.test_connection()
        sudo = self._sudo()
        self._emit_progress(progress, f"Restarting palserver-manager-agent on {info.get('hostname') or self.host}...")
        self.run(f"{sudo}systemctl restart palserver-manager-agent", 60, progress=progress)
        # systemd restart can return before the service has fully settled.
        time.sleep(1.0)
        state = self.run(f"{sudo}systemctl is-active palserver-manager-agent", 30).strip()
        if state != "active":
            raise ProvisioningError(f"Agent restart completed but systemd state is {state}")
        self._emit_progress(progress, "Agent systemd service is active after restart.")
        return {**info, "state": state}

    def uninstall_agent(self, progress=None) -> dict[str, Any]:
        """Remove the PalServer Manager agent while preserving game servers."""
        info = self.test_connection()
        sudo = self._sudo()
        host_name = info.get("hostname") or self.host
        self._emit_progress(progress, f"Uninstalling PalServer Manager agent from {host_name}...")
        self._emit_progress(progress, "Stopping and disabling palserver-manager-agent...")
        command = (
            f"{sudo}sh -lc '"
            "systemctl stop palserver-manager-agent >/dev/null 2>&1 || true; "
            "systemctl disable palserver-manager-agent >/dev/null 2>&1 || true; "
            "rm -f /etc/systemd/system/palserver-manager-agent.service; "
            "systemctl daemon-reload; "
            "systemctl reset-failed palserver-manager-agent >/dev/null 2>&1 || true; "
            "rm -rf /opt/palserver-manager /etc/palserver-manager'"
        )
        self.run(command, 180, progress=progress)
        self._emit_progress(progress, "Agent files and systemd unit removed. Palworld server files, backups, SSH access, and the Linux user were preserved.")
        return {**info, "agent_uninstalled": True}

    def update_linux(self, progress=None) -> dict[str, Any]:
        """Run normal Debian/Ubuntu package updates over SSH without rebooting."""
        info = self.test_connection()
        sudo = self._sudo()
        self._emit_progress(progress, f"Updating Debian/Ubuntu packages on {info.get('hostname') or self.host}...")
        command = (
            f"{sudo}sh -lc 'export DEBIAN_FRONTEND=noninteractive; "
            "if command -v apt-get >/dev/null 2>&1; then apt-get update && apt-get upgrade -y; "
            "else echo UNSUPPORTED_PACKAGE_MANAGER >&2; exit 42; fi'"
        )
        self.run(command, 3700, progress=progress)
        reboot_required = self.run("test -f /var/run/reboot-required && echo yes || echo no", 15).strip() == "yes"
        if reboot_required:
            self._emit_progress(progress, "Linux reports that a reboot is recommended. PalServer Manager will not reboot automatically.")
        else:
            self._emit_progress(progress, "Linux package update completed; no reboot is currently reported as required.")
        return {**info, "updated": True, "reboot_required": reboot_required}

    def _make_bundle(self) -> Path:
        source_pkg = Path(__file__).resolve().parent
        temp = Path(tempfile.mkdtemp(prefix="palserver-manager-agent-"))
        shutil.copytree(source_pkg, temp / "palserver_manager", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (temp / "pyproject.toml").write_text(
            f'''[build-system]\nrequires=["setuptools>=69","wheel"]\nbuild-backend="setuptools.build_meta"\n\n[project]\nname="palserver-manager"\nversion="{__version__}"\nrequires-python=">=3.11"\ndependencies=["requests>=2.32","psutil>=6.0","rich>=13.9","fastapi>=0.115","uvicorn[standard]>=0.32"]\n\n[project.scripts]\npalserver-agent="palserver_manager.agent:main"\npalserver-manager="palserver_manager.cli:main"\n\n[tool.setuptools.packages.find]\ninclude=["palserver_manager*"]\n\n[tool.setuptools.package-data]\npalserver_manager=["assets/*.png"]\n''',
            encoding="utf-8",
        )
        return temp

    def _upload_bundle(self, bundle: Path, remote_dir: str, progress=None) -> None:
        cmd = [
            self.scp, "-r", "-P", str(self.port), "-i", self.key_file,
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            str(bundle), f"{self.target}:{remote_dir}",
        ]
        self._emit_progress(progress, f"Uploading agent package to {self.host}...")
        self._run_process(cmd, 300, progress=progress, error_label="SCP upload failed")
        self._emit_progress(progress, "Agent package upload complete.")

    def install_agent(self, update_os: bool = True, progress=None) -> dict[str, Any]:
        self._emit_progress(progress, f"Starting Linux host provisioning for {self.target}:{self.port}")
        self._emit_progress(progress, "[1/8] Testing SSH connectivity and sudo access...")
        info = self.test_connection()
        self._emit_progress(progress, f"Connected to {info.get('hostname') or self.host} ({info.get('os', 'Linux')}).")
        sudo = self._sudo()
        remote_parent = "/tmp"
        bundle = self._make_bundle()
        remote_bundle = f"/tmp/{bundle.name}"
        token = secrets.token_urlsafe(32)
        try:
            self._emit_progress(progress, "[2/8] Preparing and uploading the PalServer Manager agent...")
            self._upload_bundle(bundle, remote_parent, progress=progress)
            if update_os:
                self._emit_progress(progress, "[3/8] Updating Debian/Ubuntu packages. This can take several minutes...")
                update_cmd = (
                    f"{sudo}sh -lc 'export DEBIAN_FRONTEND=noninteractive; "
                    "if command -v apt-get >/dev/null 2>&1; then apt-get update && apt-get upgrade -y; "
                    "else echo UNSUPPORTED_PACKAGE_MANAGER >&2; exit 42; fi'"
                )
                self.run(update_cmd, 3700, progress=progress)
                self._emit_progress(progress, "Operating-system package update completed.")
            else:
                self._emit_progress(progress, "[3/8] Operating-system package update skipped by user.")
            self._emit_progress(progress, "[4/8] Installing Python prerequisites...")
            prereq = (
                f"{sudo}sh -lc 'export DEBIAN_FRONTEND=noninteractive; "
                "if command -v apt-get >/dev/null 2>&1; then apt-get update && apt-get install -y python3 python3-venv python3-pip; "
                "else echo Automatic agent bootstrap currently supports Debian/Ubuntu >&2; exit 42; fi'"
            )
            self.run(prereq, 1200, progress=progress)
            self._emit_progress(progress, "Python prerequisites are ready.")
            self._emit_progress(progress, "[5/8] Creating the agent virtual environment and installing PalServer Manager...")
            install = (
                f"{sudo}sh -lc 'mkdir -p /opt/palserver-manager /etc/palserver-manager; "
                "python3 -m venv /opt/palserver-manager/venv; "
                "/opt/palserver-manager/venv/bin/pip install --upgrade pip; "
                f"/opt/palserver-manager/venv/bin/pip install --upgrade {remote_bundle}'"
            )
            self.run(install, 1800, progress=progress)
            self._emit_progress(progress, "Agent Python package installation completed.")
            self._emit_progress(progress, "[6/8] Configuring the private loopback agent and authentication...")
            # Preserve an existing token when upgrading an already-linked host.
            token_script = (
                f"{sudo}env PALSERVER_MANAGER_CONFIG=/etc/palserver-manager/config.json "
                "/opt/palserver-manager/venv/bin/python -c \""
                "from pathlib import Path; from palserver_manager.config import load_config,save_config; "
                "p=Path('/etc/palserver-manager/config.json'); existed=p.exists(); "
                f"c=load_config(); c.agent.bind_host='127.0.0.1'; c.agent.port=8765; c.agent.token=c.agent.token or '{token}'; "
                "c.instances=(c.instances if existed else []); c.active_instance_id=(c.active_instance_id if existed else ''); c.host_only_mode=(c.host_only_mode if existed else True); "
                "save_config(c); print(c.agent.token)\""
            )
            # Never stream this command because its stdout is the agent token.
            active_token = self.run(token_script, 60).splitlines()[-1].strip()
            self._emit_progress(progress, "Agent authentication configured; management listener remains on 127.0.0.1:8765.")
            self._emit_progress(progress, "[7/8] Installing and starting the systemd service...")
            unit = """[Unit]\nDescription=PalServer Manager Agent\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\nEnvironment=PALSERVER_MANAGER_CONFIG=/etc/palserver-manager/config.json\nExecStart=/opt/palserver-manager/venv/bin/palserver-agent --host 127.0.0.1 --port 8765\nRestart=on-failure\nRestartSec=5\nUser=root\nGroup=root\n\n[Install]\nWantedBy=multi-user.target\n"""
            escaped = unit.replace("'", "'\\''")
            self.run(
                f"printf '%s' '{escaped}' | {sudo}tee /etc/systemd/system/palserver-manager-agent.service >/dev/null && "
                f"{sudo}chmod 600 /etc/palserver-manager/config.json && {sudo}systemctl daemon-reload && "
                f"{sudo}systemctl enable palserver-manager-agent && {sudo}systemctl restart palserver-manager-agent",
                120, progress=progress,
            )
            state = self.run(f"{sudo}systemctl is-active palserver-manager-agent", 30).strip()
            if state != "active":
                raise ProvisioningError(f"Agent installed but systemd state is {state}")
            self._emit_progress(progress, "Agent systemd service is active.")
            self._emit_progress(progress, "[8/8] Verifying host state and finishing provisioning...")
            reboot_required = self.run("test -f /var/run/reboot-required && echo yes || echo no", 15).strip() == "yes"
            if reboot_required:
                self._emit_progress(progress, "Linux reports that a reboot is recommended. PalServer Manager will not reboot automatically.")
            self._emit_progress(progress, f"Provisioning completed successfully. Agent version {__version__} is online.")
            return {
                **info,
                "agent_token": active_token,
                "agent_port": 8765,
                "agent_version": __version__,
                "reboot_required": reboot_required,
            }
        finally:
            shutil.rmtree(bundle, ignore_errors=True)
            try:
                self.run(f"rm -rf {remote_bundle}", 30)
            except Exception:
                pass
