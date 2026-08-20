from pathlib import Path

from palserver_manager.backup import BackupManager
from palserver_manager.config import AppConfig


def build_cfg(tmp_path: Path):
    install = tmp_path / "PalServer"
    config = install / "Pal" / "Saved" / "Config" / "LinuxServer"
    saves = install / "Pal" / "Saved" / "SaveGames"
    config.mkdir(parents=True)
    saves.mkdir(parents=True)
    (config / "PalWorldSettings.ini").write_text("test", encoding="utf-8")
    (saves / "world.sav").write_text("save", encoding="utf-8")
    cfg = AppConfig()
    cfg.server.install_dir = str(install)
    cfg.server.config_path = str(config / "PalWorldSettings.ini")
    cfg.server.save_dir = str(saves)
    cfg.server.backup_dir = str(tmp_path / "backups")
    cfg.backup.retention_count = 2
    return cfg


def test_backup_create_and_list(tmp_path: Path):
    cfg = build_cfg(tmp_path)
    manager = BackupManager(cfg)
    created = manager.create("test")
    assert Path(created["path"]).exists()
    assert manager.list()[0]["name"].endswith(".tar.gz")
