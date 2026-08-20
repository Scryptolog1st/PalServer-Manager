from pathlib import Path

from palserver_manager.config import AppConfig
from palserver_manager.worlds import WorldManager


def test_world_list_archive_delete(tmp_path: Path):
    cfg = AppConfig()
    cfg.server.install_dir = str(tmp_path / "PalServer")
    cfg.server.save_dir = str(tmp_path / "PalServer" / "Pal" / "Saved" / "SaveGames")
    cfg.server.backup_dir = str(tmp_path / "backups")
    world = Path(cfg.server.save_dir) / "0" / "ABC123"
    world.mkdir(parents=True)
    (world / "Level.sav").write_bytes(b"world")
    manager = WorldManager(cfg)
    rows = manager.list_worlds()
    assert rows[0]["guid"] == "ABC123"
    archive = manager.archive("ABC123")
    assert Path(archive["archive"]).exists()
    manager.delete("ABC123", archive_first=False)
    assert manager.list_worlds() == []
