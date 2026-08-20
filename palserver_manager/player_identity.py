from __future__ import annotations


def platform_from_user_id(user_id: str) -> str:
    """Return a human-readable platform label for a Palworld user ID.

    Palworld exposes a generic ``userId`` string through the REST player list.
    The platform is not returned as a separate schema field, so the manager
    labels common platform prefixes while preserving the original ID.
    """
    value = str(user_id or "").strip().lower()
    if value.startswith("steam_"):
        return "Steam"
    if value.startswith(("gdk_", "xbox_", "xuid_")):
        return "Xbox / Microsoft"
    if value.startswith(("psn_", "ps5_", "sce_", "playstation_")):
        return "PlayStation"
    if value.startswith(("eos_", "epic_")):
        return "Epic / EOS"
    if value.startswith("nintendo_"):
        return "Nintendo"
    if "_" in value:
        prefix = value.split("_", 1)[0].strip()
        if prefix:
            return prefix.upper()
    return "Unknown"


def account_id_without_platform_prefix(user_id: str) -> str:
    value = str(user_id or "").strip()
    if "_" not in value:
        return value
    prefix, remainder = value.split("_", 1)
    if prefix.lower() in {
        "steam", "gdk", "xbox", "xuid", "psn", "ps5", "sce", "playstation",
        "eos", "epic", "nintendo",
    }:
        return remainder
    return value
