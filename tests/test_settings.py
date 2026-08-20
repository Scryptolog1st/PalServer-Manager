from pathlib import Path

from palserver_manager.settings import IniManager, parse_settings_text, settings_dict


SAMPLE = '''[/Script/Pal.PalGameWorldSettings]\nOptionSettings=(ServerName="Test Server",bPalLost=False,ExpRate=1.000000,CrossplayPlatforms=(Steam,Xbox,PS5,Mac),ServerPassword="")\n'''


def test_parse_nested_tuple():
    settings, _ = parse_settings_text(SAMPLE)
    data = settings_dict(settings)
    assert data["ServerName"] == '"Test Server"'
    assert data["CrossplayPlatforms"] == "(Steam,Xbox,PS5,Mac)"
    assert data["bPalLost"] == "False"


def test_verified_write(tmp_path: Path):
    path = tmp_path / "PalWorldSettings.ini"
    path.write_text(SAMPLE, encoding="utf-8")
    manager = IniManager(path, tmp_path / "backups")
    result = manager.set_value("bPalLost", "True")
    assert result["verified"] is True
    assert manager.values(reveal_secrets=True)["bPalLost"] == "True"
    assert list((tmp_path / "backups").glob("*.bak"))


def test_secret_mask(tmp_path: Path):
    path = tmp_path / "PalWorldSettings.ini"
    path.write_text(SAMPLE.replace('ServerPassword=""', 'ServerPassword="secret"'), encoding="utf-8")
    manager = IniManager(path)
    assert manager.values()["ServerPassword"] == '"********"'
    assert manager.values(reveal_secrets=True)["ServerPassword"] == '"secret"'


def test_setting_metadata_is_human_readable(tmp_path: Path):
    path = tmp_path / "PalWorldSettings.ini"
    path.write_text(SAMPLE, encoding="utf-8")
    manager = IniManager(path)
    rows = {row.key: row for row in manager.records()}
    assert rows["ServerName"].readable_name == "Server Name"
    assert "displayed" in rows["ServerName"].description.lower()
    assert "true" in rows["bPalLost"].allowed_values.lower()
    assert rows["bPalLost"].choices == ["True", "False"]
    assert "Steam" in rows["CrossplayPlatforms"].allowed_values
