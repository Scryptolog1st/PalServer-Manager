from palserver_manager.bans import BanRegistry


def test_ban_registry_add_replace_remove(tmp_path):
    registry = BanRegistry(tmp_path / "bans.json")
    row = registry.add(
        "steam_123",
        player_name="PalUser",
        account_name="paluser",
        platform="Steam",
        reason="Testing",
    )
    assert row["user_id"] == "steam_123"
    assert registry.list()[0]["player_name"] == "PalUser"

    registry.add("steam_123", player_name="Renamed", platform="Steam", reason="Again")
    rows = registry.list()
    assert len(rows) == 1
    assert rows[0]["player_name"] == "Renamed"
    assert rows[0]["reason"] == "Again"

    assert registry.remove("steam_123") is True
    assert registry.list() == []
    assert registry.remove("steam_123") is False
