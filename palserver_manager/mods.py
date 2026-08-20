from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

import requests
import psutil

from .config import AppConfig
from .service import service_controller


DEFAULT_UE4SS_REPO = "BlackBookOfficial/ue4ss-linux-palworld"
PACKAGE_MANIFEST = "palserver-mod.json"
MANIFEST_VERSION = 1


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9._-]+", "-", str(value or "").strip().lower()).strip("-.")
    return value or "mod"


def _within(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _safe_rel(value: str) -> Path:
    raw = str(value or "").replace("\\", "/").lstrip("/")
    rel = Path(raw)
    if not raw or rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"Unsafe package path: {value}")
    return rel


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(list(args), text=True, capture_output=True)
    if check and result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "Command failed").strip())
    return result


class ModManager:
    """Per-instance Palworld mod runtime and package manager.

    Manager metadata lives outside the game tree under the instance backup
    directory so Steam validation cannot erase it. Runtime files are staged in
    Pal/Binaries/Linux and generic managed mod packages use a small manifest
    plus server/ and client/ payload roots.
    """

    def __init__(self, cfg: AppConfig, instance_id: str = "default"):
        self.cfg = cfg
        self.instance_id = str(instance_id or "default")
        self.install_root = Path(cfg.server.install_dir)
        self.stage = self.install_root / "Pal" / "Binaries" / "Linux"
        self.mods_dir = self.stage / "Mods"
        self.mods_txt = self.mods_dir / "mods.txt"
        self.runtime_lib = self.stage / "libUE4SS.so"
        self.runtime_launcher = self.stage / "run_ue4ss.sh"
        self.runtime_settings = self.stage / "UE4SS-settings.ini"
        self.runtime_log = self.stage / "UE4SS.log"
        self.runtime_marker = self.stage / ".palserver-manager-ue4ss-enabled"
        self.meta_root = Path(cfg.server.backup_dir) / ".palserver-manager" / "mods"
        self.state_path = self.meta_root / "manifest.json"
        self.client_root = self.meta_root / "client-assets"
        self.disabled_root = self.meta_root / "disabled-server-files"
        self.original_root = self.meta_root / "original-server-files"
        self.pack_root = self.meta_root / "client-packs"

    def _default_state(self) -> dict[str, Any]:
        return {
            "manifest_version": MANIFEST_VERSION,
            "modset_version": 0,
            "runtime": {
                "enabled": False,
                "type": "vanilla",
                "version": "",
                "source_repo": DEFAULT_UE4SS_REPO,
                "source_url": "",
                "asset_name": "",
                "sha256": "",
                "installed_at": None,
                "last_validated": None,
            },
            "mods": [],
        }

    def _load(self) -> dict[str, Any]:
        state = self._default_state()
        try:
            if self.state_path.exists():
                raw = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    state.update({k: v for k, v in raw.items() if k in state})
                    if isinstance(raw.get("runtime"), dict):
                        state["runtime"].update(raw["runtime"])
                    if isinstance(raw.get("mods"), list):
                        state["mods"] = raw["mods"]
        except Exception:
            pass
        return state

    def _save(self, state: dict[str, Any]) -> dict[str, Any]:
        self.meta_root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)
        return state

    def _bump(self, state: dict[str, Any]) -> None:
        state["modset_version"] = int(state.get("modset_version") or 0) + 1

    def _service_running(self) -> bool:
        state = str(service_controller(self.cfg, self.instance_id).status().get("state", "")).lower()
        return state in {"active", "running"}

    def _systemd_dropin(self) -> Path:
        return Path("/etc/systemd/system") / f"{self.cfg.server.service_name}.service.d" / "20-palserver-manager-ue4ss.conf"

    def _configure_launch(self, enabled: bool) -> None:
        self.stage.mkdir(parents=True, exist_ok=True)
        if enabled:
            self.runtime_marker.write_text("enabled\n", encoding="utf-8")
            try:
                self.runtime_marker.chmod(0o644)
            except OSError:
                pass
        else:
            self.runtime_marker.unlink(missing_ok=True)

        if os.name != "nt" and Path("/run/systemd/system").exists() and self.cfg.server.service_name:
            dropin = self._systemd_dropin()
            if enabled:
                dropin.parent.mkdir(parents=True, exist_ok=True)
                crash_dir = self.stage / "UE4SS-crashes"
                crash_dir.mkdir(parents=True, exist_ok=True)
                # Systemd Environment preserves the existing ExecStart exactly,
                # including custom port/player flags, while scoping LD_PRELOAD to
                # this one server service.
                dropin.write_text(
                    "[Service]\n"
                    f'Environment="LD_PRELOAD={self.runtime_lib}"\n'
                    f'Environment="UE4SS_CRASH_LOG_DIR={crash_dir}"\n',
                    encoding="utf-8",
                )
            else:
                dropin.unlink(missing_ok=True)
                try:
                    dropin.parent.rmdir()
                except OSError:
                    pass
            _run("systemctl", "daemon-reload", check=False)

    def _runtime_release(self, source_url: str = "", source_repo: str = DEFAULT_UE4SS_REPO) -> dict[str, str]:
        if source_url:
            name = source_url.rsplit("/", 1)[-1] or "ue4ss-linux-runtime"
            return {"version": "custom", "url": source_url, "name": name, "digest": ""}
        response = requests.get(
            f"https://api.github.com/repos/{source_repo}/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "PalServer-Manager"},
            timeout=30,
        )
        response.raise_for_status()
        release = response.json()
        assets = release.get("assets") or []
        candidates = []
        for asset in assets:
            name = str(asset.get("name") or "")
            lower = name.lower()
            if lower.endswith((".tar.gz", ".tgz", ".zip")) and ("linux" in lower or "ue4ss" in lower):
                score = 0
                if "palworld" in lower: score += 20
                if lower.endswith(".tar.gz"): score += 4
                if "release" in lower or "build" in lower: score += 2
                candidates.append((score, asset))
        if not candidates:
            raise RuntimeError(f"No Linux UE4SS release archive was found in {source_repo}'s latest GitHub release")
        candidates.sort(key=lambda row: row[0], reverse=True)
        asset = candidates[0][1]
        return {
            "version": str(release.get("tag_name") or release.get("name") or "latest"),
            "url": str(asset.get("browser_download_url") or ""),
            "name": str(asset.get("name") or "ue4ss-linux-runtime"),
            "digest": str(asset.get("digest") or ""),
        }

    def _extract_runtime(self, archive: Path, destination: Path) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        lower = archive.name.lower()
        if lower.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                for info in zf.infolist():
                    rel = _safe_rel(info.filename)
                    target = destination / rel
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
        else:
            with tarfile.open(archive, "r:*") as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    rel = _safe_rel(member.name)
                    target = destination / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    src = tf.extractfile(member)
                    if src is not None:
                        with src, target.open("wb") as dst:
                            shutil.copyfileobj(src, dst)
        libs = list(destination.rglob("libUE4SS.so"))
        if not libs:
            raise RuntimeError("UE4SS archive did not contain libUE4SS.so")
        return libs[0]

    def install_runtime(self, *, source_url: str = "", source_repo: str = DEFAULT_UE4SS_REPO) -> dict[str, Any]:
        if os.name == "nt" or platform.system().lower() != "linux":
            raise RuntimeError("Automatic UE4SS runtime installation currently supports native Linux Palworld servers only")
        machine = platform.machine().lower()
        if machine not in {"x86_64", "amd64"}:
            raise RuntimeError(f"UE4SS Linux runtime requires x86_64; this host reports {machine}")
        shipping = self.stage / "PalServer-Linux-Shipping"
        if not shipping.exists():
            raise FileNotFoundError(f"Native Linux Palworld executable was not found: {shipping}")

        release = self._runtime_release(source_url, source_repo)
        if not release["url"]:
            raise RuntimeError("UE4SS release did not provide a downloadable asset URL")
        self.stage.mkdir(parents=True, exist_ok=True)
        self.mods_dir.mkdir(parents=True, exist_ok=True)
        self.mods_txt.touch(exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="palserver-ue4ss-") as tmp_name:
            tmp = Path(tmp_name)
            archive = tmp / release["name"]
            with requests.get(release["url"], stream=True, timeout=120, headers={"User-Agent": "PalServer-Manager"}) as response:
                response.raise_for_status()
                sha = hashlib.sha256()
                with archive.open("wb") as handle:
                    for chunk in response.iter_content(1024 * 1024):
                        if chunk:
                            sha.update(chunk)
                            handle.write(chunk)
            digest = sha.hexdigest()
            advertised = release.get("digest") or ""
            if advertised.startswith("sha256:") and digest.lower() != advertised.split(":", 1)[1].lower():
                raise RuntimeError("UE4SS release SHA-256 digest did not match GitHub release metadata")
            extracted = tmp / "extracted"
            lib = self._extract_runtime(archive, extracted)
            shutil.copy2(lib, self.runtime_lib)
            try:
                self.runtime_lib.chmod(0o755)
            except OSError:
                pass

            launcher = next(iter(extracted.rglob("run_ue4ss.sh")), None)
            if launcher:
                shutil.copy2(launcher, self.runtime_launcher)
                try: self.runtime_launcher.chmod(0o755)
                except OSError: pass
            else:
                # Keep a manager-owned wrapper available for direct-process
                # launches and troubleshooting even when a release only ships
                # libUE4SS.so. systemd instances use an Environment drop-in so
                # their existing ExecStart/arguments are preserved exactly.
                self.runtime_launcher.write_text(
                    "#!/usr/bin/env bash\nset -euo pipefail\n"
                    "stage=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")\" && pwd)\"\n"
                    "export LD_PRELOAD=\"$stage/libUE4SS.so${LD_PRELOAD:+:$LD_PRELOAD}\"\n"
                    "exec \"$@\"\n",
                    encoding="utf-8",
                )
                self.runtime_launcher.chmod(0o755)

            settings = next(iter(extracted.rglob("UE4SS-settings.ini")), None)
            if settings:
                shutil.copy2(settings, self.runtime_settings)
            elif not self.runtime_settings.exists():
                self.runtime_settings.write_text(
                    "[Debug]\nDebugConsoleEnabled=false\nSimpleConsoleEnabled=true\n\n"
                    "[Overrides]\nModsFolderPath=./Mods\n",
                    encoding="utf-8",
                )
            for extra_name in ("MemberVariableLayout.ini", "VTableLayout.ini", "ObjectDumper-0.1.0.lua"):
                extra = next(iter(extracted.rglob(extra_name)), None)
                if extra:
                    shutil.copy2(extra, self.stage / extra_name)

        self._configure_launch(True)
        state = self._load()
        state["runtime"].update({
            "enabled": True,
            "type": "ue4ss-linux",
            "version": release["version"],
            "source_repo": source_repo,
            "source_url": release["url"],
            "asset_name": release["name"],
            "sha256": digest,
            "installed_at": time.time(),
        })
        self._bump(state)
        self._save(state)
        return self.status()

    def disable_runtime(self) -> dict[str, Any]:
        state = self._load()
        self._configure_launch(False)
        state["runtime"]["enabled"] = False
        state["runtime"]["type"] = "vanilla"
        self._bump(state)
        self._save(state)
        return self.status()

    def repair_after_update(self) -> dict[str, Any]:
        state = self._load()
        runtime = state.get("runtime") or {}
        if not runtime.get("enabled"):
            return self.status()
        if not self.runtime_lib.exists():
            url = str(runtime.get("source_url") or "")
            repo = str(runtime.get("source_repo") or DEFAULT_UE4SS_REPO)
            self.install_runtime(source_url=url, source_repo=repo)
        else:
            self._configure_launch(True)
        return self.validate_runtime()

    def _tail_log(self, lines: int = 160) -> list[str]:
        if not self.runtime_log.exists():
            return []
        try:
            return self.runtime_log.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, int(lines)):]
        except OSError:
            return []

    def validate_runtime(self) -> dict[str, Any]:
        state = self._load()
        runtime = state.get("runtime") or {}
        enabled = bool(runtime.get("enabled"))
        running = self._service_running()
        checks: dict[str, Any] = {
            "library": self.runtime_lib.exists(),
            "launcher": self.runtime_launcher.exists(),
            "settings": self.runtime_settings.exists(),
            "mods_directory": self.mods_dir.exists(),
            "launch_marker": self.runtime_marker.exists() if enabled else True,
            "loaded_in_process": None,
        }
        loaded = None
        if enabled and running and os.name != "nt":
            try:
                status = service_controller(self.cfg, self.instance_id).status()
                root_pid = int(status.get("pid") or 0)
                candidates = []
                if root_pid and psutil.pid_exists(root_pid):
                    root_proc = psutil.Process(root_pid)
                    candidates = [root_proc, *root_proc.children(recursive=True)]
                install_hint = str(self.install_root).lower().rstrip("/")
                # The shipping process can be re-parented away from the shell
                # wrapper, so include a tightly filtered process-table fallback.
                for proc in psutil.process_iter(["pid", "name", "cmdline", "exe"]):
                    try:
                        text = " ".join([
                            str(proc.info.get("name") or ""),
                            " ".join(proc.info.get("cmdline") or []),
                            str(proc.info.get("exe") or ""),
                        ]).lower()
                        if "palserver-linux-shipping" in text and (not install_hint or install_hint in text):
                            if all(existing.pid != proc.pid for existing in candidates):
                                candidates.append(proc)
                    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                        continue
                inspected = False
                found = False
                for proc in candidates:
                    maps = Path(f"/proc/{proc.pid}/maps")
                    if not maps.exists():
                        continue
                    inspected = True
                    if "libue4ss.so" in maps.read_text(encoding="utf-8", errors="ignore").lower():
                        found = True
                        break
                loaded = found if inspected else None
            except Exception:
                loaded = None
        checks["loaded_in_process"] = loaded
        log_lines = self._tail_log(220)
        lower_log = "\n".join(log_lines).lower()
        refused = [line for line in log_lines if "refused" in line.lower()][-8:]
        errors = [line for line in log_lines if any(word in line.lower() for word in ("fatal", "error", "sigsegv", "sigabrt"))][-8:]
        health = "disabled"
        detail = "Vanilla startup is selected."
        if enabled:
            missing = [key for key in ("library", "settings", "mods_directory", "launch_marker") if checks.get(key) is False]
            if missing:
                health = "failed"
                detail = "Missing runtime component(s): " + ", ".join(missing)
            elif running and loaded is False:
                health = "failed"
                detail = "Palworld is running but libUE4SS.so is not mapped into the service process."
            elif errors or refused:
                health = "warning"
                detail = "UE4SS.log contains recent error/refused-hook markers."
            elif running and (loaded is True or "ue4ss" in lower_log):
                health = "healthy"
                detail = "UE4SS runtime appears loaded."
            elif not running:
                health = "stopped"
                detail = "Runtime is configured; start the server to verify loading."
            else:
                health = "warning"
                detail = "Runtime files are present but loading could not yet be confirmed."
        state["runtime"]["last_validated"] = time.time()
        self._save(state)
        return {
            "health": health,
            "detail": detail,
            "checks": checks,
            "refused_hooks": refused,
            "recent_errors": errors,
            "log_tail": log_lines[-80:],
        }

    def status(self) -> dict[str, Any]:
        state = self._load()
        runtime = dict(state.get("runtime") or {})
        mods = []
        for row in state.get("mods") or []:
            if not isinstance(row, dict):
                continue
            copy = dict(row)
            copy["enabled"] = bool(copy.get("enabled", True))
            mods.append(copy)
        enabled_client = [m for m in mods if m.get("enabled") and m.get("client_required")]
        return {
            "runtime": runtime,
            "modset_version": int(state.get("modset_version") or 0),
            "mods": mods,
            "client_pack": {
                "required": bool(enabled_client),
                "mod_count": len(enabled_client),
                "version": int(state.get("modset_version") or 0),
            },
            "paths": {
                "stage": str(self.stage),
                "mods": str(self.mods_dir),
                "log": str(self.runtime_log),
                "manifest": str(self.state_path),
            },
        }

    def log_tail(self, lines: int = 160) -> list[str]:
        return self._tail_log(lines)

    def _read_package_manifest(self, zf: zipfile.ZipFile) -> dict[str, Any]:
        names = {name.replace("\\", "/"): name for name in zf.namelist()}
        raw_name = names.get(PACKAGE_MANIFEST)
        if raw_name is None:
            raise ValueError(
                f"Managed mod packages must contain {PACKAGE_MANIFEST} at the ZIP root. "
                "The package may then contain server/ and/or client/ payloads."
            )
        manifest = json.loads(zf.read(raw_name).decode("utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("Mod package manifest must be a JSON object")
        mod_id = _slug(manifest.get("id") or manifest.get("name"))
        manifest["id"] = mod_id
        manifest["name"] = str(manifest.get("name") or mod_id).strip() or mod_id
        manifest["version"] = str(manifest.get("version") or "0.0.0")
        manifest["runtime"] = str(manifest.get("runtime") or "ue4ss").lower()
        manifest["type"] = str(manifest.get("type") or "generic")
        manifest["server_required"] = bool(manifest.get("server_required", True))
        manifest["client_required"] = bool(manifest.get("client_required", False))
        manifest["ue4ss_mod_name"] = str(manifest.get("ue4ss_mod_name") or "").strip()
        names = manifest.get("ue4ss_mod_names")
        if isinstance(names, list):
            manifest["ue4ss_mod_names"] = [str(value).strip() for value in names if str(value).strip()]
        else:
            manifest["ue4ss_mod_names"] = [manifest["ue4ss_mod_name"]] if manifest["ue4ss_mod_name"] else []
        compatibility = manifest.get("compatibility")
        manifest["compatibility"] = compatibility if isinstance(compatibility, dict) else {}
        return manifest

    def install_package(self, archive_bytes: bytes, filename: str = "mod.zip") -> dict[str, Any]:
        if not archive_bytes:
            raise ValueError("Mod package is empty")
        self.meta_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="palserver-mod-") as tmp_name:
            archive = Path(tmp_name) / (Path(filename).name or "mod.zip")
            archive.write_bytes(archive_bytes)
            try:
                zf = zipfile.ZipFile(archive)
            except zipfile.BadZipFile as exc:
                raise ValueError("Managed mod packages must be ZIP archives") from exc
            with zf:
                manifest = self._read_package_manifest(zf)
                mod_id = manifest["id"]
                state = self._load()
                runtime_name = str(manifest.get("runtime") or "").lower()
                if runtime_name in {"ue4ss", "ue4ss-linux"} and not bool((state.get("runtime") or {}).get("enabled")):
                    raise RuntimeError("This package requires UE4SS. Enable Mod Support for this server before installing the package.")
                if any(str(row.get("id")) == mod_id for row in state.get("mods") or []):
                    raise ValueError(f"Mod '{mod_id}' is already installed. Remove it before installing a different package version.")

                # Validate the entire package before touching the live server.
                # This prevents a malformed archive from leaving half-installed
                # files behind after a later validation failure.
                server_entries: list[tuple[zipfile.ZipInfo, Path]] = []
                client_entries: list[tuple[zipfile.ZipInfo, Path]] = []
                for info in zf.infolist():
                    name = info.filename.replace("\\", "/")
                    if info.is_dir() or name == PACKAGE_MANIFEST:
                        continue
                    rel = _safe_rel(name)
                    parts = rel.parts
                    if not parts:
                        continue
                    if parts[0] == "server" and len(parts) > 1:
                        target_rel = Path(*parts[1:])
                        target = self.install_root / target_rel
                        if not _within(self.install_root, target):
                            raise ValueError(f"Unsafe server payload target: {target_rel}")
                        server_entries.append((info, target_rel))
                    elif parts[0] == "client" and len(parts) > 1:
                        client_entries.append((info, Path(*parts[1:])))

                if manifest["server_required"] and not server_entries:
                    raise ValueError("Package declares server_required=true but contains no server/ payload files")
                if manifest["client_required"] and not client_entries:
                    raise ValueError("Package declares client_required=true but contains no client/ payload files")

                server_files: list[str] = []
                client_files: list[str] = []
                original_files: list[str] = []
                written_live: list[Path] = []
                written_client: list[Path] = []
                original_mod_root = self.original_root / mod_id
                try:
                    for info, target_rel in server_entries:
                        target = self.install_root / target_rel
                        # Managed packages may intentionally overlay a file.
                        # Preserve the original so disable/remove and failed
                        # installs can restore the vanilla/pre-existing state.
                        if target.exists():
                            if not target.is_file():
                                raise ValueError(f"Server payload target is not a file: {target_rel}")
                            backup = original_mod_root / target_rel
                            backup.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(target, backup)
                            original_files.append(target_rel.as_posix())
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(info) as src, target.open("wb") as dst:
                            shutil.copyfileobj(src, dst)
                        written_live.append(target)
                        server_files.append(target_rel.as_posix())

                    for info, target_rel in client_entries:
                        target = self.client_root / mod_id / target_rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(info) as src, target.open("wb") as dst:
                            shutil.copyfileobj(src, dst)
                        written_client.append(target)
                        client_files.append(target_rel.as_posix())

                    manifest.update({
                        "enabled": True,
                        "server_files": server_files,
                        "client_files": client_files,
                        "original_files": original_files,
                        "installed_at": time.time(),
                        "source_filename": Path(filename).name,
                    })
                    state.setdefault("mods", []).append(manifest)
                    self._set_mods_txt(manifest, True)
                    self._bump(state)
                    self._save(state)
                except Exception:
                    # Roll the filesystem back before surfacing the failure.
                    for target in reversed(written_live):
                        try:
                            target.unlink(missing_ok=True)
                        except OSError:
                            pass
                    for rel_text in original_files:
                        rel = _safe_rel(rel_text)
                        backup = original_mod_root / rel
                        target = self.install_root / rel
                        if backup.exists():
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(backup, target)
                    shutil.rmtree(self.client_root / mod_id, ignore_errors=True)
                    shutil.rmtree(original_mod_root, ignore_errors=True)
                    raise
        return self.status()

    def _set_mods_txt(self, mod: dict[str, Any], enabled: bool) -> None:
        names = mod.get("ue4ss_mod_names")
        if not isinstance(names, list):
            names = []
        normalized = [str(value).strip() for value in names if str(value).strip()]
        fallback = str(mod.get("ue4ss_mod_name") or "").strip()
        if fallback and fallback not in normalized:
            normalized.insert(0, fallback)
        if not normalized:
            return
        self.mods_dir.mkdir(parents=True, exist_ok=True)
        rows = []
        if self.mods_txt.exists():
            rows = self.mods_txt.read_text(encoding="utf-8", errors="replace").splitlines()
        out = list(rows)
        for name in normalized:
            pattern = re.compile(rf"^\s*{re.escape(name)}\s*:\s*[01]\s*$", re.I)
            replacement = f"{name} : {1 if enabled else 0}"
            found = False
            rewritten = []
            for row in out:
                if pattern.match(row):
                    if not found:
                        rewritten.append(replacement)
                        found = True
                else:
                    rewritten.append(row)
            if not found:
                rewritten.append(replacement)
            out = rewritten
        self.mods_txt.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")

    def _mod_row(self, state: dict[str, Any], mod_id: str) -> dict[str, Any]:
        for row in state.get("mods") or []:
            if str(row.get("id")) == str(mod_id):
                return row
        raise KeyError(f"Unknown installed mod: {mod_id}")

    def set_enabled(self, mod_id: str, enabled: bool) -> dict[str, Any]:
        state = self._load()
        row = self._mod_row(state, mod_id)
        if bool(row.get("enabled", True)) == bool(enabled):
            return self.status()
        mod_disabled_root = self.disabled_root / str(row["id"])
        original_mod_root = self.original_root / str(row["id"])
        original_set = {str(value) for value in (row.get("original_files") or [])}
        if enabled:
            for rel_text in row.get("server_files") or []:
                rel = _safe_rel(rel_text)
                parked = mod_disabled_root / rel
                target = self.install_root / rel
                if parked.exists():
                    # If disabling restored an original file, park that
                    # original again before putting the modded file back.
                    if rel_text in original_set and target.exists() and target.is_file():
                        original = original_mod_root / rel
                        original.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(target), str(original))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(parked), str(target))
        else:
            for rel_text in row.get("server_files") or []:
                rel = _safe_rel(rel_text)
                target = self.install_root / rel
                parked = mod_disabled_root / rel
                if target.exists() and target.is_file():
                    parked.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target), str(parked))
                if rel_text in original_set:
                    original = original_mod_root / rel
                    if original.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(original), str(target))
        row["enabled"] = bool(enabled)
        self._set_mods_txt(row, bool(enabled))
        self._bump(state)
        self._save(state)
        return self.status()

    def remove(self, mod_id: str) -> dict[str, Any]:
        state = self._load()
        row = self._mod_row(state, mod_id)
        self._set_mods_txt(row, False)
        original_mod_root = self.original_root / str(row["id"])
        original_set = {str(value) for value in (row.get("original_files") or [])}
        enabled = bool(row.get("enabled", True))
        for rel_text in row.get("server_files") or []:
            rel = _safe_rel(rel_text)
            live = self.install_root / rel
            parked = self.disabled_root / str(row["id"]) / rel
            if enabled:
                if live.exists() and live.is_file():
                    live.unlink()
            else:
                if parked.exists() and parked.is_file():
                    parked.unlink()
            if rel_text in original_set:
                original = original_mod_root / rel
                if original.exists():
                    # An original may already be live when the mod was
                    # disabled. Only restore it when the live path is absent.
                    if not live.exists():
                        live.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(original), str(live))
                    else:
                        original.unlink()
        shutil.rmtree(self.client_root / str(row["id"]), ignore_errors=True)
        shutil.rmtree(self.disabled_root / str(row["id"]), ignore_errors=True)
        shutil.rmtree(original_mod_root, ignore_errors=True)
        state["mods"] = [item for item in state.get("mods") or [] if str(item.get("id")) != str(mod_id)]
        self._bump(state)
        self._save(state)
        return self.status()

    def build_client_pack(self) -> dict[str, Any]:
        state = self._load()
        selected = [row for row in state.get("mods") or [] if row.get("enabled") and row.get("client_required")]
        self.pack_root.mkdir(parents=True, exist_ok=True)
        version = int(state.get("modset_version") or 0)
        name = f"palserver-client-modpack-v{version}.zip"
        path = self.pack_root / name
        manifest = {
            "server": self.instance_id,
            "modset_version": version,
            "generated_at": time.time(),
            "mods": [{k: row.get(k) for k in ("id", "name", "version", "type", "runtime", "compatibility")} for row in selected],
        }
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("README.txt", "PalServer Manager client mod pack. Install these files in the matching Palworld client directories. All players must use a compatible mod set.\n")
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            for row in selected:
                source_root = self.client_root / str(row.get("id"))
                if not source_root.exists():
                    continue
                for file in source_root.rglob("*"):
                    if file.is_file():
                        zf.write(file, file.relative_to(source_root).as_posix())
        return {"path": str(path), "name": name, "size": path.stat().st_size, "version": version, "mods": len(selected)}

    def client_pack_path(self) -> Path:
        result = self.build_client_pack()
        return Path(result["path"])
