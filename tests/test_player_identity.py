from palserver_manager.player_identity import platform_from_user_id, account_id_without_platform_prefix


def test_common_platform_prefixes():
    assert platform_from_user_id("steam_76561198000000000") == "Steam"
    assert platform_from_user_id("gdk_2535435690904903") == "Xbox / Microsoft"
    assert platform_from_user_id("xbox_123") == "Xbox / Microsoft"
    assert platform_from_user_id("psn_abc") == "PlayStation"
    assert platform_from_user_id("eos_xyz") == "Epic / EOS"
    assert platform_from_user_id("mystery_123") == "MYSTERY"
    assert platform_from_user_id("") == "Unknown"


def test_account_id_without_known_prefix():
    assert account_id_without_platform_prefix("steam_123") == "123"
    assert account_id_without_platform_prefix("gdk_456") == "456"
    assert account_id_without_platform_prefix("custom_789") == "custom_789"
