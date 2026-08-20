import io
import json
import zipfile
from pathlib import Path

import pytest

from palserver_manager.config import AppConfig
from palserver_manager.mods import ModManager


def make_manager(tmp_path: Path) -> ModManager:
    cfg = AppConfig(host_only_mode=True)
    cfg.server.install_dir = str(tmp_path / "palworld")
    cfg.server.backup_dir = str(tmp_path / "backups")
    cfg.server.service_name = "palworld-test"
    cfg.server.resolve()
    Path(cfg.server.install_dir).mkdir(parents=True, exist_ok=True)
    return ModManager(cfg, "test")


def make_package(manifest: dict, files: dict[str, bytes | str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("palserver-mod.json", json.dumps(manifest))
        for name, content in files.items():
            zf.writestr(name, content.encode() if isinstance(content, str) else content)
    return output.getvalue()


def enable_runtime_metadata(manager: ModManager) -> None:
    state = manager._load()
    state["runtime"].update({"enabled": True, "type": "ue4ss-linux", "version": "test"})
    manager._save(state)


def test_mod_state_begins_vanilla(tmp_path):
    manager = make_manager(tmp_path)
    status = manager.status()
    assert status["runtime"]["enabled"] is False
    assert status["runtime"]["type"] == "vanilla"
    assert status["mods"] == []
    assert status["client_pack"]["required"] is False


def test_ue4ss_package_requires_runtime(tmp_path):
    manager = make_manager(tmp_path)
    package = make_package(
        {"id": "admin-tools", "name": "Admin Tools", "runtime": "ue4ss"},
        {"server/Pal/Binaries/Linux/Mods/AdminTools/scripts/main.lua": "print('ok')"},
    )
    with pytest.raises(RuntimeError, match="requires UE4SS"):
        manager.install_package(package, "admin-tools.zip")


def test_install_toggle_remove_and_client_pack(tmp_path):
    manager = make_manager(tmp_path)
    enable_runtime_metadata(manager)
    original = Path(manager.install_root) / "Pal" / "Binaries" / "Linux" / "Mods" / "Example" / "scripts" / "main.lua"
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_text("print('original')", encoding="utf-8")

    package = make_package(
        {
            "id": "example-mod",
            "name": "Example Mod",
            "version": "1.2.3",
            "runtime": "ue4ss",
            "type": "custom-pal",
            "server_required": True,
            "client_required": True,
            "ue4ss_mod_name": "Example",
            "compatibility": {"palworld": "1.0.x", "ue4ss": ">=1.0"},
        },
        {
            "server/Pal/Binaries/Linux/Mods/Example/scripts/main.lua": "print('modded')",
            "client/Paks/ExampleMod.pak": b"pak-data",
        },
    )
    status = manager.install_package(package, "example.zip")
    assert status["mods"][0]["type"] == "custom-pal"
    assert status["client_pack"]["required"] is True
    assert original.read_text(encoding="utf-8") == "print('modded')"
    assert "Example : 1" in manager.mods_txt.read_text(encoding="utf-8")

    manager.set_enabled("example-mod", False)
    assert original.read_text(encoding="utf-8") == "print('original')"
    assert "Example : 0" in manager.mods_txt.read_text(encoding="utf-8")

    manager.set_enabled("example-mod", True)
    assert original.read_text(encoding="utf-8") == "print('modded')"
    assert "Example : 1" in manager.mods_txt.read_text(encoding="utf-8")

    pack = manager.build_client_pack()
    with zipfile.ZipFile(pack["path"]) as zf:
        assert "Paks/ExampleMod.pak" in zf.namelist()
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["mods"][0]["id"] == "example-mod"

    manager.remove("example-mod")
    assert original.read_text(encoding="utf-8") == "print('original')"
    assert manager.status()["mods"] == []


def test_package_validation_happens_before_live_file_changes(tmp_path):
    manager = make_manager(tmp_path)
    enable_runtime_metadata(manager)
    live = Path(manager.install_root) / "Pal" / "Binaries" / "Linux" / "Mods" / "Example" / "scripts" / "main.lua"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text("keep-me", encoding="utf-8")

    # client_required is deliberately true but there is no client payload.
    package = make_package(
        {
            "id": "bad-package",
            "runtime": "ue4ss",
            "server_required": True,
            "client_required": True,
        },
        {"server/Pal/Binaries/Linux/Mods/Example/scripts/main.lua": "overwrite"},
    )
    with pytest.raises(ValueError, match="client_required"):
        manager.install_package(package, "bad.zip")
    assert live.read_text(encoding="utf-8") == "keep-me"
    assert manager.status()["mods"] == []


def test_package_rejects_path_traversal(tmp_path):
    manager = make_manager(tmp_path)
    enable_runtime_metadata(manager)
    package = make_package(
        {"id": "unsafe", "runtime": "ue4ss", "server_required": True},
        {"server/../../escape.txt": "nope"},
    )
    with pytest.raises(ValueError, match="Unsafe package path"):
        manager.install_package(package, "unsafe.zip")
    assert not (tmp_path / "escape.txt").exists()
