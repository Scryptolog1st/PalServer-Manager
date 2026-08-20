from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


CATEGORIES: dict[str, list[str]] = {
    "Server / Network": [
        "ServerName", "ServerDescription", "ServerPassword", "AdminPassword",
        "ServerPlayerMaxNum", "PublicPort", "PublicIP", "Region",
        "RCONEnabled", "RCONPort", "RESTAPIEnabled", "RESTAPIPort",
        "bUseAuth", "BanListURL", "bShowPlayerList", "ChatPostLimitPerMinute",
        "CrossplayPlatforms", "AllowConnectPlatform", "bAllowClientMod", "LogFormatType",
        "bIsShowJoinLeftMessage", "bEnableVoiceChat",
        "VoiceChatMaxVolumeDistance", "VoiceChatZeroVolumeDistance",
    ],
    "World / Difficulty": [
        "Difficulty", "RandomizerType", "RandomizerSeed",
        "bIsRandomizerPalLevelRandom", "DayTimeSpeedRate", "NightTimeSpeedRate",
        "ExpRate", "PalCaptureRate", "PalSpawnNumRate", "EnemyDropItemRate",
        "SupplyDropSpan", "EnablePredatorBossPal", "bHardcore", "bPalLost",
        "bCharacterRecreateInHardcore", "ItemCorruptionMultiplier",
        "MonsterFarmActionSpeedRate",
    ],
    "Player": [
        "PlayerDamageRateAttack", "PlayerDamageRateDefense",
        "PlayerStomachDecreaceRate", "PlayerStaminaDecreaceRate",
        "PlayerAutoHPRegeneRate", "PlayerAutoHpRegeneRateInSleep", "ItemWeightRate",
        "CoopPlayerMaxNum", "bEnableAimAssistPad", "bEnableAimAssistKeyboard",
        "bEnableFastTravel", "bEnableFastTravelOnlyBaseCamp",
        "bIsStartLocationSelectByMap", "bExistPlayerAfterLogout",
        "bEnableNonLoginPenalty", "bAllowEnhanceStat_Health",
        "bAllowEnhanceStat_Attack", "bAllowEnhanceStat_Stamina",
        "bAllowEnhanceStat_Weight", "bAllowEnhanceStat_WorkSpeed",
    ],
    "Pal": [
        "PalDamageRateAttack", "PalDamageRateDefense", "PalStomachDecreaceRate",
        "PalStaminaDecreaceRate", "PalAutoHPRegeneRate", "PalAutoHpRegeneRateInSleep",
        "PalEggDefaultHatchingTime", "WorkSpeedRate",
    ],
    "Drops / Gathering": [
        "DropItemMaxNum", "PhysicsActiveDropItemMaxNum", "DropItemMaxNum_UNKO",
        "DropItemAliveMaxHours", "CollectionDropRate", "CollectionObjectHpRate",
        "CollectionObjectRespawnSpeedRate", "AdditionalDropItemWhenPlayerKillingInPvPMode",
        "AdditionalDropItemNumWhenPlayerKillingInPvPMode",
        "bAdditionalDropItemWhenPlayerKillingInPvPMode",
    ],
    "Building / Bases": [
        "BuildObjectHpRate", "BuildObjectDamageRate", "BuildObjectDeteriorationDamageRate",
        "BaseCampMaxNum", "BaseCampWorkerMaxNum", "BaseCampMaxNumInGuild",
        "bBuildAreaLimit", "bAllowEnemyCampSpawnNearBaseCamp", "MaxBuildingLimitNum", "ServerReplicatePawnCullDistance",
        "bEnableBuildingPlayerUIdDisplay", "BuildingNameDisplayCacheTTLSeconds",
        "ItemContainerForceMarkDirtyInterval", "PlayerDataPalStorageUpdateCheckTickInterval",
    ],
    "Guilds": [
        "GuildPlayerMaxNum", "bAutoResetGuildNoOnlinePlayers",
        "AutoResetGuildTimeNoOnlinePlayers", "GuildRejoinCooldownMinutes",
        "AutoTransferMasterCheckIntervalSeconds", "AutoTransferMasterThresholdDays",
        "MaxGuildsPerFrame",
    ],
    "PvP / Multiplayer": [
        "bIsMultiplay", "bIsPvP", "bEnablePlayerToPlayerDamage", "bEnableFriendlyFire",
        "bEnableInvaderEnemy", "bActiveUNKO", "bCanPickupOtherGuildDeathPenaltyDrop",
        "bEnableDefenseOtherGuildPlayer", "bInvisibleOtherGuildBaseCampAreaFX",
        "bDisplayPvPItemNumOnWorldMap_BaseCamp", "bDisplayPvPItemNumOnWorldMap_Player",
        "AdditionalDropItemWhenPlayerKillingInPvPMode",
        "AdditionalDropItemNumWhenPlayerKillingInPvPMode",
        "bAdditionalDropItemWhenPlayerKillingInPvPMode",
    ],
    "Save / Respawn": [
        "AutoSaveSpan", "DeathPenalty", "bIsUseBackupSaveData", "BlockRespawnTime",
        "RespawnPenaltyDurationThreshold", "RespawnPenaltyTimeScale",
        "DenyTechnologyList", "EquipmentDurabilityDamageRate",
    ],
    "Global Palbox": ["bAllowGlobalPalboxExport", "bAllowGlobalPalboxImport"],
}

CHOICES = {
    "Difficulty": ["None", "Normal", "Hard"],
    "DeathPenalty": ["None", "Item", "ItemAndEquipment", "All"],
    "LogFormatType": ["Text", "Json"],
    "RandomizerType": ["None", "Region", "All"],
}

SECRET_KEYS = {"AdminPassword", "ServerPassword"}

# A curated core. Unknown/new settings still work and receive generated descriptions.
DESCRIPTIONS = {
    "ServerName": "Name displayed for the dedicated server.",
    "ServerDescription": "Description displayed for the dedicated server.",
    "ServerPlayerMaxNum": "Maximum number of players allowed on the server.",
    "ServerPassword": "Password required to join the server.",
    "AdminPassword": "Password used for server administrative access and the REST API.",
    "PublicPort": "Publicly advertised server port. This does not itself change the listening port.",
    "PublicIP": "Explicit public IP used for community-server advertisement.",
    "RESTAPIEnabled": "Enables Palworld's built-in REST management API.",
    "RESTAPIPort": "Listening port for Palworld's REST API.",
    "RCONEnabled": "Enables legacy RCON. RCON is deprecated; prefer REST API.",
    "RCONPort": "Legacy RCON listening port.",
    "CrossplayPlatforms": "Platforms permitted to connect to the server.",
    "ExpRate": "Experience gain multiplier.",
    "PalCaptureRate": "Pal capture probability multiplier.",
    "PalSpawnNumRate": "Pal spawn quantity multiplier.",
    "DayTimeSpeedRate": "Daytime progression speed multiplier.",
    "NightTimeSpeedRate": "Nighttime progression speed multiplier.",
    "PalDamageRateAttack": "Damage dealt by Pals multiplier.",
    "PalDamageRateDefense": "Damage received by Pals multiplier.",
    "PlayerDamageRateAttack": "Damage dealt by players multiplier.",
    "PlayerDamageRateDefense": "Damage received by players multiplier.",
    "CollectionDropRate": "Gathered resource yield multiplier.",
    "CollectionObjectHpRate": "Resource-node health multiplier.",
    "CollectionObjectRespawnSpeedRate": "Resource respawn interval multiplier; lower values respawn faster.",
    "EnemyDropItemRate": "Enemy item drop quantity multiplier.",
    "DeathPenalty": "Controls what a player loses on death.",
    "bHardcore": "Enables hardcore player-death behavior.",
    "bPalLost": "Enables permanent Pal loss behavior where supported by the active world/server mode.",
    "bCharacterRecreateInHardcore": "Controls character recreation behavior in hardcore mode.",
    "bIsPvP": "Enables PvP mode.",
    "bEnablePlayerToPlayerDamage": "Allows players to damage other players.",
    "bEnableFriendlyFire": "Allows friendly fire.",
    "GuildPlayerMaxNum": "Maximum number of players in a guild.",
    "BaseCampWorkerMaxNum": "Maximum number of workers assigned to a base camp.",
    "BaseCampMaxNumInGuild": "Maximum number of base camps per guild.",
    "PalEggDefaultHatchingTime": "Base egg hatching time multiplier.",
    "WorkSpeedRate": "Work-speed multiplier.",
    "AutoSaveSpan": "Automatic save interval in seconds.",
    "bIsUseBackupSaveData": "Enables Palworld's built-in rotating save backups.",
    "bAllowGlobalPalboxExport": "Allows exporting Pals to the Global Palbox.",
    "bAllowGlobalPalboxImport": "Allows importing Pals from the Global Palbox.",
    "SupplyDropSpan": "Meteorite and supply-drop interval in minutes.",
    "ChatPostLimitPerMinute": "Maximum chat messages allowed per minute.",
}


DISPLAY_NAMES = {
    "ServerName": "Server Name",
    "ServerDescription": "Server Description",
    "ServerPassword": "Server Join Password",
    "AdminPassword": "Administrator Password",
    "ServerPlayerMaxNum": "Maximum Players",
    "PublicPort": "Public Advertised Port",
    "PublicIP": "Public Advertised IP Address",
    "Region": "Server Region",
    "RCONEnabled": "Enable Legacy RCON",
    "RCONPort": "Legacy RCON Port",
    "RESTAPIEnabled": "Enable REST API",
    "RESTAPIPort": "REST API Port",
    "bUseAuth": "Require Authentication",
    "BanListURL": "Ban List URL",
    "bShowPlayerList": "Show Player List",
    "ChatPostLimitPerMinute": "Chat Messages Per Minute",
    "CrossplayPlatforms": "Allowed Crossplay Platforms",
    "AllowConnectPlatform": "Legacy Allowed Platform",
    "bAllowClientMod": "Allow Modded Clients",
    "LogFormatType": "Server Log Format",
    "bIsShowJoinLeftMessage": "Show Join / Leave Messages",
    "bEnableVoiceChat": "Enable Voice Chat",
    "VoiceChatMaxVolumeDistance": "Voice Chat Full-Volume Distance",
    "VoiceChatZeroVolumeDistance": "Voice Chat Maximum Distance",
    "Difficulty": "Difficulty Preset",
    "RandomizerType": "Pal Spawn Randomizer Mode",
    "RandomizerSeed": "Pal Spawn Randomizer Seed",
    "bIsRandomizerPalLevelRandom": "Fully Randomize Wild Pal Levels",
    "DayTimeSpeedRate": "Daytime Speed",
    "NightTimeSpeedRate": "Nighttime Speed",
    "ExpRate": "Experience Gain Rate",
    "PalCaptureRate": "Pal Capture Rate",
    "PalSpawnNumRate": "Pal Spawn Rate",
    "EnemyDropItemRate": "Enemy Drop Quantity",
    "SupplyDropSpan": "Meteorite / Supply Drop Interval",
    "EnablePredatorBossPal": "Enable Predator Boss Pals",
    "bHardcore": "Enable Hardcore Mode",
    "bPalLost": "Lose Pals Permanently on Death",
    "bCharacterRecreateInHardcore": "Allow Character Recreation in Hardcore",
    "ItemCorruptionMultiplier": "Item Corruption Speed",
    "MonsterFarmActionSpeedRate": "Ranch / Grazing Production Speed",
    "PlayerDamageRateAttack": "Player Damage Dealt",
    "PlayerDamageRateDefense": "Player Damage Taken",
    "PlayerStomachDecreaceRate": "Player Hunger Depletion",
    "PlayerStaminaDecreaceRate": "Player Stamina Depletion",
    "PlayerAutoHPRegeneRate": "Player HP Regeneration",
    "PlayerAutoHpRegeneRateInSleep": "Player Sleeping HP Regeneration",
    "ItemWeightRate": "Item Weight",
    "CoopPlayerMaxNum": "Co-op Maximum Players",
    "bEnableAimAssistPad": "Controller Aim Assist",
    "bEnableAimAssistKeyboard": "Keyboard / Mouse Aim Assist",
    "bEnableFastTravel": "Enable Fast Travel",
    "bEnableFastTravelOnlyBaseCamp": "Restrict Fast Travel to Bases",
    "bIsStartLocationSelectByMap": "Allow Starting Location Selection",
    "bExistPlayerAfterLogout": "Keep Logged-Out Players in World",
    "bEnableNonLoginPenalty": "Enable Non-Login Penalty",
    "bAllowEnhanceStat_Health": "Allow HP Stat Investment",
    "bAllowEnhanceStat_Attack": "Allow Attack Stat Investment",
    "bAllowEnhanceStat_Stamina": "Allow Stamina Stat Investment",
    "bAllowEnhanceStat_Weight": "Allow Carry Weight Stat Investment",
    "bAllowEnhanceStat_WorkSpeed": "Allow Work Speed Stat Investment",
    "PalDamageRateAttack": "Pal Damage Dealt",
    "PalDamageRateDefense": "Pal Damage Taken",
    "PalStomachDecreaceRate": "Pal Hunger Depletion",
    "PalStaminaDecreaceRate": "Pal Stamina Depletion",
    "PalAutoHPRegeneRate": "Pal HP Regeneration",
    "PalAutoHpRegeneRateInSleep": "Palbox HP Regeneration",
    "PalEggDefaultHatchingTime": "Huge Egg Incubation Time",
    "WorkSpeedRate": "Work Speed",
    "DropItemMaxNum": "Maximum Dropped Items",
    "PhysicsActiveDropItemMaxNum": "Maximum Physics-Active Dropped Items",
    "DropItemMaxNum_UNKO": "Maximum Special Dropped Items",
    "DropItemAliveMaxHours": "Dropped Item Lifetime",
    "CollectionDropRate": "Gathered Resource Quantity",
    "CollectionObjectHpRate": "Resource Node Health",
    "CollectionObjectRespawnSpeedRate": "Resource Respawn Interval",
    "AdditionalDropItemWhenPlayerKillingInPvPMode": "PvP Kill Bonus Item ID",
    "AdditionalDropItemNumWhenPlayerKillingInPvPMode": "PvP Kill Bonus Item Quantity",
    "bAdditionalDropItemWhenPlayerKillingInPvPMode": "Enable PvP Kill Bonus Item",
    "BuildObjectHpRate": "Building Health",
    "BuildObjectDamageRate": "Building Damage Taken",
    "BuildObjectDeteriorationDamageRate": "Building Deterioration Rate",
    "BaseCampMaxNum": "Maximum Bases Across Server",
    "BaseCampWorkerMaxNum": "Maximum Pals Per Base",
    "BaseCampMaxNumInGuild": "Maximum Bases Per Guild",
    "bBuildAreaLimit": "Restrict Building Near Protected Structures",
    "MaxBuildingLimitNum": "Per-Player Building Limit",
    "ServerReplicatePawnCullDistance": "Pal Synchronization Distance",
    "bEnableBuildingPlayerUIdDisplay": "Show Structure Creator ID",
    "BuildingNameDisplayCacheTTLSeconds": "Building Name Cache Duration",
    "ItemContainerForceMarkDirtyInterval": "Container Re-Sync Interval",
    "PlayerDataPalStorageUpdateCheckTickInterval": "Pal Storage Update Check Interval",
    "GuildPlayerMaxNum": "Maximum Guild Members",
    "bAutoResetGuildNoOnlinePlayers": "Auto-Delete Inactive Guild Bases",
    "AutoResetGuildTimeNoOnlinePlayers": "Inactive Guild Reset Delay",
    "GuildRejoinCooldownMinutes": "Guild Rejoin Cooldown",
    "AutoTransferMasterCheckIntervalSeconds": "Guild Master Transfer Check Interval",
    "AutoTransferMasterThresholdDays": "Guild Master Inactivity Threshold",
    "MaxGuildsPerFrame": "Guild Processing Limit Per Frame",
    "bIsMultiplay": "Enable Multiplayer",
    "bIsPvP": "Enable PvP",
    "bEnablePlayerToPlayerDamage": "Enable Player-to-Player Damage",
    "bEnableFriendlyFire": "Enable Friendly Fire",
    "bEnableInvaderEnemy": "Enable Base Invasions",
    "bActiveUNKO": "Enable UNKO Feature",
    "bCanPickupOtherGuildDeathPenaltyDrop": "Allow Looting Other Guild Death Drops",
    "bEnableDefenseOtherGuildPlayer": "Allow Defense Against Other Guild Players",
    "bInvisibleOtherGuildBaseCampAreaFX": "Show Other Guild Base Boundaries",
    "bDisplayPvPItemNumOnWorldMap_BaseCamp": "Show PvP Base Items on Map",
    "bDisplayPvPItemNumOnWorldMap_Player": "Show PvP Player Items on Map",
    "AutoSaveSpan": "Automatic Save Interval",
    "DeathPenalty": "Death Penalty",
    "bIsUseBackupSaveData": "Enable Built-In World Backups",
    "BlockRespawnTime": "Base Respawn Cooldown",
    "RespawnPenaltyDurationThreshold": "Respawn Penalty Survival Threshold",
    "RespawnPenaltyTimeScale": "Respawn Cooldown Multiplier",
    "bAllowGlobalPalboxExport": "Allow Global Palbox Export",
    "bAllowGlobalPalboxImport": "Allow Global Palbox Import",
    "bAllowEnemyCampSpawnNearBaseCamp": "Allow Enemy Camps Near Player Bases",
    "DenyTechnologyList": "Disabled Technology IDs",
    "EquipmentDurabilityDamageRate": "Equipment Durability Loss",
}

# The following descriptions are based on Pocketpair's current dedicated-server
# configuration guide where documented. Entries not exposed by the current guide
# are still labeled clearly in the UI instead of pretending they are documented.
DESCRIPTIONS.update({
    "BaseCampMaxNum": "Controls the total number of bases that may exist across the entire server.",
    "BaseCampMaxNumInGuild": "Controls the maximum number of bases a single guild may own. Higher values increase server processing load.",
    "BaseCampWorkerMaxNum": "Controls the maximum number of Pals that can be assigned to one base. Higher values increase server processing load.",
    "ItemContainerForceMarkDirtyInterval": "Controls how often an open container is forcibly re-synchronized with clients, in seconds.",
    "MaxBuildingLimitNum": "Limits how many building pieces each player may place. A value of 0 means unlimited.",
    "PhysicsActiveDropItemMaxNum": "Limits how many dropped items may use physics simulation at the same time.",
    "ServerReplicatePawnCullDistance": "Controls how far from players Pal entities continue to synchronize, measured in centimeters.",
    "bAllowClientMod": "Controls whether clients with mods enabled are permitted to join the server.",
    "bEnableBuildingPlayerUIdDisplay": "Controls whether the player ID of a structure's creator is displayed on the structure.",
    "bIsShowJoinLeftMessage": "Controls whether dedicated-server join and leave messages are shown in game.",
    "bIsUseBackupSaveData": "Controls Palworld's built-in rotating world-save backup system. Enabling it increases disk activity.",
    "ChatPostLimitPerMinute": "Sets the maximum number of chat messages a player may post per minute.",
    "CrossplayPlatforms": "Controls which supported platforms are allowed to connect to the server.",
    "AllowConnectPlatform": "Legacy platform-selection setting. Pocketpair's current server guide says this parameter is not available in the current version and to use CrossplayPlatforms instead.",
    "LogFormatType": "Controls whether dedicated-server logs are written as human-readable text or JSON.",
    "bAllowEnemyCampSpawnNearBaseCamp": "Controls whether enemy camps are allowed to spawn near player bases.",
    "bAllowEnhanceStat_Attack": "Controls whether players may spend stat points on Attack.",
    "bAllowEnhanceStat_Health": "Controls whether players may spend stat points on HP.",
    "bAllowEnhanceStat_Stamina": "Controls whether players may spend stat points on Stamina.",
    "bAllowEnhanceStat_Weight": "Controls whether players may spend stat points on Carry Weight.",
    "bAllowEnhanceStat_WorkSpeed": "Controls whether players may spend stat points on Work Speed.",
    "bAllowGlobalPalboxExport": "Controls whether players may save/export Pals to the Global Palbox.",
    "bAllowGlobalPalboxImport": "Controls whether players may load/import Pals from the Global Palbox.",
    "bAutoResetGuildNoOnlinePlayers": "When enabled, inactive guilds can have their structures and base Pals automatically removed after the configured offline period.",
    "AutoResetGuildTimeNoOnlinePlayers": "Sets how long a guild may remain completely offline before the automatic inactive-guild reset occurs. Ignored when automatic guild reset is disabled.",
    "bBuildAreaLimit": "Controls whether building is blocked near protected world structures such as fast-travel points.",
    "bCharacterRecreateInHardcore": "Controls whether a player may recreate their character after dying in Hardcore mode.",
    "bDisplayPvPItemNumOnWorldMap_BaseCamp": "Controls whether the world map displays PvP-exclusive item counts for bases.",
    "bDisplayPvPItemNumOnWorldMap_Player": "Controls whether the world map displays player locations and PvP-exclusive item counts.",
    "bEnableFastTravel": "Enables or disables fast travel.",
    "bEnableFastTravelOnlyBaseCamp": "When enabled, fast travel is restricted to travel between player bases.",
    "bEnableInvaderEnemy": "Enables or disables enemy invasion events.",
    "bEnableVoiceChat": "Enables or disables in-game voice chat.",
    "bExistPlayerAfterLogout": "Controls whether logged-out players remain in the world in a sleeping state at their last location.",
    "bHardcore": "Enables Hardcore mode. In the officially documented behavior, players cannot normally respawn after death.",
    "bInvisibleOtherGuildBaseCampAreaFX": "Controls visibility of other guilds' base-area boundary effects.",
    "bIsPvP": "Enables or disables Player-versus-Player mode.",
    "bIsRandomizerPalLevelRandom": "Controls wild-Pal levels when spawn randomization is enabled: fully random when True, or constrained to the area's intended level range when False.",
    "bIsStartLocationSelectByMap": "Controls whether players may select their starting location from the map.",
    "bShowPlayerList": "Controls whether the player list is available from the ESC menu.",
    "RandomizerSeed": "Sets the seed used by Pal spawn randomization.",
    "RandomizerType": "Controls Pal spawn randomization: disabled, randomized by region, or randomized globally.",
    "VoiceChatMaxVolumeDistance": "Sets the distance within which voice chat remains at full volume.",
    "VoiceChatZeroVolumeDistance": "Sets the distance at which voice chat becomes completely inaudible.",
    "AdditionalDropItemNumWhenPlayerKillingInPvPMode": "Sets how many of the configured special item are dropped when a player is killed in PvP and the bonus-drop feature is enabled.",
    "AdditionalDropItemWhenPlayerKillingInPvPMode": "Sets the Technology/Item ID of the special item dropped for PvP kills when the bonus-drop feature is enabled.",
    "bAdditionalDropItemWhenPlayerKillingInPvPMode": "Enables or disables the special bonus item drop when a player is killed in PvP.",
    "BlockRespawnTime": "Sets the base cooldown before a player may respawn after death, in seconds.",
    "bPalLost": "Controls whether Pals are permanently lost when the player dies.",
    "BuildObjectDamageRate": "Multiplier for damage received by buildings. Higher values make structures take more damage.",
    "BuildObjectDeteriorationDamageRate": "Multiplier for building deterioration/decay damage. Higher values make structures deteriorate faster.",
    "CollectionDropRate": "Multiplier for the quantity of resources received from gathering.",
    "CollectionObjectHpRate": "Multiplier for the health of gatherable resource objects.",
    "CollectionObjectRespawnSpeedRate": "Multiplier for resource-object respawn intervals. Lower values generally make resources return sooner.",
    "DayTimeSpeedRate": "Multiplier controlling how quickly daytime passes.",
    "DeathPenalty": "Controls what a player drops or loses when they die.",
    "DenyTechnologyList": "Lists Technology IDs that are disabled on the server.",
    "EnemyDropItemRate": "Multiplier for the quantity of items dropped by enemies.",
    "EquipmentDurabilityDamageRate": "Multiplier for durability loss applied to equipment.",
    "ExpRate": "Multiplier for experience gained by players and Pals.",
    "GuildPlayerMaxNum": "Sets the maximum number of players allowed in one guild.",
    "GuildRejoinCooldownMinutes": "Sets the cooldown, in minutes, before a player may rejoin a guild after leaving.",
    "ItemCorruptionMultiplier": "Multiplier controlling item corruption speed.",
    "ItemWeightRate": "Multiplier applied to item weight.",
    "MonsterFarmActionSpeedRate": "Multiplier for item production speed from grazing/ranch activities.",
    "NightTimeSpeedRate": "Multiplier controlling how quickly nighttime passes.",
    "PalAutoHPRegeneRate": "Multiplier for a Pal's natural HP regeneration rate.",
    "PalAutoHpRegeneRateInSleep": "Multiplier for Pal HP regeneration while sleeping/in the Palbox.",
    "PalCaptureRate": "Multiplier applied to Pal capture probability.",
    "PalDamageRateAttack": "Multiplier for damage dealt by Pals.",
    "PalDamageRateDefense": "Multiplier for damage received by Pals.",
    "PalEggDefaultHatchingTime": "Sets the incubation time for a Huge Egg in hours; other egg sizes scale from this value.",
    "PalSpawnNumRate": "Multiplier for the number/rate of Pal spawns. Higher values can increase server load.",
    "PalStaminaDecreaceRate": "Multiplier for Pal stamina depletion. Higher values drain stamina faster.",
    "PalStomachDecreaceRate": "Multiplier for Pal hunger depletion. Higher values make hunger drain faster.",
    "PlayerAutoHPRegeneRate": "Multiplier for a player's natural HP regeneration rate.",
    "PlayerAutoHpRegeneRateInSleep": "Multiplier for player HP regeneration while sleeping.",
    "PlayerDamageRateAttack": "Multiplier for damage dealt by players.",
    "PlayerDamageRateDefense": "Multiplier for damage received by players.",
    "PlayerStaminaDecreaceRate": "Multiplier for player stamina depletion. Higher values drain stamina faster.",
    "PlayerStomachDecreaceRate": "Multiplier for player hunger depletion. Higher values make hunger drain faster.",
    "RespawnPenaltyDurationThreshold": "Sets how long a player must survive, in seconds, before the next death is eligible for the configured respawn-cooldown multiplier.",
    "RespawnPenaltyTimeScale": "Multiplier applied to the respawn cooldown when the respawn penalty is triggered.",
    "SupplyDropSpan": "Sets the interval between meteorite/supply-drop events, in minutes.",
})

VALUE_HINTS = {
    "Difficulty": "None, Normal, or Hard.",
    "DeathPenalty": "None = lose nothing; Item = drop items except equipment; ItemAndEquipment = drop all items including equipment; All = drop all items and all Pals in the active team.",
    "LogFormatType": "Text or Json.",
    "RandomizerType": "None = disabled; Region = randomize within each region; All = fully randomized across regions.",
    "CrossplayPlatforms": "A parenthesized platform list using supported values Steam, Xbox, PS5, and Mac; for example (Steam,Xbox,PS5,Mac).",
    "AllowConnectPlatform": "Deprecated/unavailable in the current documented server version. Use CrossplayPlatforms instead.",
    "BaseCampMaxNumInGuild": "Whole number. Official maximum: 10. Default documented value: 4.",
    "BaseCampWorkerMaxNum": "Whole number from 0 to 50. Higher values increase server load.",
    "ServerReplicatePawnCullDistance": "Whole number from 5000 to 15000 centimeters.",
    "MaxBuildingLimitNum": "Whole number; 0 means unlimited.",
    "PublicPort": "Valid UDP port number, usually 1-65535. This advertises a public port; it does not change the actual listen port.",
    "RCONPort": "Valid TCP port number, usually 1-65535. RCON is legacy/deprecated in Palworld's current server guidance.",
    "RESTAPIPort": "Valid TCP port number, usually 1-65535. Keep the REST API private/protected rather than exposing it directly to the Internet.",
    "ServerPlayerMaxNum": "Whole number greater than 0. The launch -players argument may also affect the live maximum.",
    "ChatPostLimitPerMinute": "Whole number of messages allowed per minute.",
    "RandomizerSeed": "Whole-number seed value.",
    "VoiceChatMaxVolumeDistance": "Non-negative distance value used by the game for voice attenuation.",
    "VoiceChatZeroVolumeDistance": "Non-negative distance value; should be greater than the full-volume distance.",
    "BlockRespawnTime": "Non-negative number of seconds.",
    "DenyTechnologyList": "Parenthesized list of Technology IDs, for example (\"PALBOX\",\"RepairBench\").",
    "GuildPlayerMaxNum": "Whole number greater than 0.",
    "GuildRejoinCooldownMinutes": "Non-negative whole number of minutes.",
    "PalEggDefaultHatchingTime": "Non-negative number of hours. 0 disables incubation time.",
    "SupplyDropSpan": "Non-negative number of minutes.",
    "AutoSaveSpan": "Non-negative number of seconds.",
    "AutoResetGuildTimeNoOnlinePlayers": "Non-negative duration value used by Palworld for inactive guild reset timing.",
    "ItemContainerForceMarkDirtyInterval": "Non-negative number of seconds.",
    "AdditionalDropItemNumWhenPlayerKillingInPvPMode": "Whole-number item quantity.",
    "AdditionalDropItemWhenPlayerKillingInPvPMode": "A valid Palworld item ID string.",
}


def humanize_key(key: str) -> str:
    text = re.sub(r"^b(?=[A-Z])", "", key)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = text.replace("_", " ")
    replacements = {
        "Hp": "HP", "HP": "HP", "Api": "API", "Uid": "UID", "U Id": "UID",
        "Id": "ID", "Pvp": "PvP", "Rcon": "RCON", "Exp": "EXP", "Palbox": "Palbox",
    }
    for old, new in replacements.items():
        text = re.sub(rf"\b{re.escape(old)}\b", new, text, flags=re.IGNORECASE if old in {"Api", "Rcon", "Pvp", "Exp"} else 0)
    return text.strip()


def display_name_for(key: str) -> str:
    return DISPLAY_NAMES.get(key, humanize_key(key))


def description_for(key: str) -> str:
    if key in DESCRIPTIONS:
        return DESCRIPTIONS[key]
    return (
        f"The current Pocketpair server guide does not document this parameter. "
        f"Its technical name is {key}; treat changes to this setting cautiously and verify behavior on the active Palworld server version."
    )


def allowed_values_for(key: str, raw_value: str = "") -> str:
    if key in VALUE_HINTS:
        return VALUE_HINTS[key]
    if key in CHOICES:
        return ", ".join(CHOICES[key]) + "."
    raw = str(raw_value or "").strip()
    if raw in {"True", "False"} or key.startswith("b"):
        return "True = enabled; False = disabled."
    if re.fullmatch(r"-?\d+", raw):
        return "Whole number. Use the current/default value as a safe baseline unless a documented limit is shown above."
    if re.fullmatch(r"-?(?:\d+\.\d+|\d+\.\d*|\.\d+)", raw):
        return "Decimal number/multiplier. For rate settings, 1.0 is normally the baseline; values above 1.0 increase the effect and values below 1.0 reduce it unless the description says otherwise."
    if raw.startswith("(") and raw.endswith(")"):
        return "Parenthesized list/tuple in Palworld INI syntax."
    if is_quoted(raw):
        return "Text value. Empty text is allowed only where Palworld accepts a blank setting."
    return "Value format is determined by Palworld for this parameter; preserve the existing format when editing."

def find_option_line(text: str) -> re.Match[str]:
    match = re.search(r"(?ms)^\s*OptionSettings\s*=\s*\((.*)\)\s*$", text)
    if not match:
        raise ValueError("Could not find OptionSettings=(...) in the config file.")
    return match


def split_top_level(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escape = False
    for index, char in enumerate(value):
        if quote:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                quote = None
        else:
            if char in ('"', "'"):
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth = max(0, depth - 1)
            elif char == "," and depth == 0:
                parts.append(value[start:index].strip())
                start = index + 1
    tail = value[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def parse_settings_text(text: str) -> tuple[list[list[str | None]], re.Match[str]]:
    match = find_option_line(text)
    settings: list[list[str | None]] = []
    for token in split_top_level(match.group(1)):
        if "=" not in token:
            settings.append([token, None])
            continue
        key, value = token.split("=", 1)
        settings.append([key.strip(), value.strip()])
    return settings, match


def settings_dict(settings: list[list[str | None]]) -> dict[str, str]:
    return {str(k): str(v) for k, v in settings if v is not None}


def is_quoted(value: str) -> bool:
    return len(value) >= 2 and value[0] == '"' and value[-1] == '"'


def unquote(value: str) -> str:
    if is_quoted(value):
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def format_value(current: str, raw: Any) -> str:
    if isinstance(raw, bool):
        raw = "True" if raw else "False"
    else:
        raw = str(raw).strip()

    if current in ("True", "False"):
        low = raw.lower()
        if low in ("1", "true", "t", "yes", "y", "on"):
            return "True"
        if low in ("0", "false", "f", "no", "n", "off"):
            return "False"
        raise ValueError("Expected a boolean value.")
    if re.fullmatch(r"-?\d+", current):
        if not re.fullmatch(r"-?\d+", raw):
            raise ValueError("Expected a whole number.")
        return raw
    if re.fullmatch(r"-?(?:\d+\.\d+|\d+\.\d*|\.\d+)", current):
        return f"{float(raw):.6f}"
    if is_quoted(current):
        return quote(raw)
    if current.startswith("(") and current.endswith(")"):
        return raw if raw.startswith("(") else f"({raw})"
    return raw


@dataclass
class SettingRecord:
    key: str
    raw_value: str
    display_value: str
    category: str
    description: str
    readable_name: str
    allowed_values: str
    choices: list[str]
    secret: bool = False


class IniManager:
    def __init__(self, config_path: str | Path, backup_dir: str | Path | None = None):
        self.path = Path(config_path)
        self.backup_dir = Path(backup_dir) if backup_dir else self.path.parent

    def _read(self) -> tuple[str, list[list[str | None]], re.Match[str]]:
        text = self.path.read_text(encoding="utf-8")
        settings, match = parse_settings_text(text)
        return text, settings, match

    def values(self, reveal_secrets: bool = False) -> dict[str, str]:
        _, settings, _ = self._read()
        result = settings_dict(settings)
        if not reveal_secrets:
            for key in SECRET_KEYS:
                if key in result and unquote(result[key]):
                    result[key] = '"********"'
        return result

    def records(self, query: str = "") -> list[SettingRecord]:
        values = self.values(reveal_secrets=True)
        used: set[str] = set()
        categories: dict[str, str] = {}
        for category, keys in CATEGORIES.items():
            for key in keys:
                if key not in used:
                    categories[key] = category
                    used.add(key)
        rows: list[SettingRecord] = []
        q = query.lower().strip()
        for key, raw in values.items():
            desc = description_for(key)
            readable = display_name_for(key)
            allowed = allowed_values_for(key, raw)
            if q and q not in key.lower() and q not in readable.lower() and q not in desc.lower() and q not in allowed.lower():
                continue
            secret = key in SECRET_KEYS
            display = '"********"' if secret and unquote(raw) else raw
            choices = list(CHOICES.get(key, []))
            if raw in {"True", "False"} or key.startswith("b"):
                choices = ["True", "False"]
            rows.append(SettingRecord(key, raw, display, categories.get(key, "Other / New Settings"), desc, readable, allowed, choices, secret))
        return rows

    def backup(self, label: str = "config") -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.backup_dir / f"{self.path.name}.{label}.{stamp}.bak"
        shutil.copy2(self.path, path)
        return path

    def set_value(self, key: str, new_value: Any) -> dict[str, str]:
        text, settings, match = self._read()
        matches = [item for item in settings if item[0] == key]
        if len(matches) != 1:
            raise ValueError(f"Setting {key!r} appears {len(matches)} times; refusing ambiguous write.")
        current = str(matches[0][1])
        formatted = format_value(current, new_value)
        backup = self.backup("pre-edit")
        for item in settings:
            if item[0] == key:
                item[1] = formatted
                break
        body = ",".join(f"{k}={v}" if v is not None else str(k) for k, v in settings)
        replacement = f"OptionSettings=({body})"
        new_text = text[:match.start()] + replacement + text[match.end():]
        self._atomic_write(new_text)
        verified = self.values(reveal_secrets=True)
        if verified.get(key) != formatted:
            raise IOError(f"Save verification failed for {key}: expected {formatted}, found {verified.get(key)}")
        return {"key": key, "value": formatted, "backup": str(backup), "verified": True}

    def set_many(self, changes: dict[str, Any]) -> dict[str, Any]:
        text, settings, match = self._read()
        current_map = settings_dict(settings)
        missing = [key for key in changes if key not in current_map]
        if missing:
            raise KeyError(f"Settings not found: {', '.join(missing)}")
        formatted = {key: format_value(current_map[key], value) for key, value in changes.items()}
        backup = self.backup("pre-batch")
        for item in settings:
            key = str(item[0])
            if key in formatted:
                item[1] = formatted[key]
        body = ",".join(f"{k}={v}" if v is not None else str(k) for k, v in settings)
        replacement = f"OptionSettings=({body})"
        self._atomic_write(text[:match.start()] + replacement + text[match.end():])
        verified = self.values(reveal_secrets=True)
        failures = {key: {"expected": value, "actual": verified.get(key)} for key, value in formatted.items() if verified.get(key) != value}
        if failures:
            raise IOError(f"Batch save verification failed: {failures}")
        return {"backup": str(backup), "changes": [{"key": key, "value": value, "verified": True} for key, value in formatted.items()]}

    def reset_to_defaults(self, default_path: str | Path, keys: list[str] | None = None) -> dict[str, Any]:
        default_text = Path(default_path).read_text(encoding="utf-8")
        default_settings, _ = parse_settings_text(default_text)
        defaults = settings_dict(default_settings)
        current_text, settings, match = self._read()
        current = settings_dict(settings)
        selected = set(keys or current.keys())
        changes = {key: defaults[key] for key in selected if key in defaults and key in current and defaults[key] != current[key]}
        if not changes:
            return {"backup": None, "changes": []}
        backup = self.backup("pre-reset")
        for item in settings:
            key = str(item[0])
            if key in changes:
                item[1] = changes[key]
        body = ",".join(f"{k}={v}" if v is not None else str(k) for k, v in settings)
        replacement = f"OptionSettings=({body})"
        self._atomic_write(current_text[:match.start()] + replacement + current_text[match.end():])
        verified = self.values(reveal_secrets=True)
        failures = {key: {"expected": value, "actual": verified.get(key)} for key, value in changes.items() if verified.get(key) != value}
        if failures:
            raise IOError(f"Reset verification failed: {failures}")
        return {"backup": str(backup), "changes": [{"key": key, "value": value, "verified": True} for key, value in changes.items()]}

    def compare_default(self, default_path: str | Path) -> list[dict[str, str]]:
        default_text = Path(default_path).read_text(encoding="utf-8")
        default_settings, _ = parse_settings_text(default_text)
        current = self.values(reveal_secrets=True)
        defaults = settings_dict(default_settings)
        rows = []
        for key in sorted(set(current) | set(defaults)):
            if current.get(key) != defaults.get(key):
                rows.append({"key": key, "default": defaults.get(key, "<missing>"), "current": current.get(key, "<missing>")})
        return rows

    def _atomic_write(self, new_text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".PalWorldSettings.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(new_text)
                handle.flush()
                os.fsync(handle.fileno())
            if self.path.exists():
                shutil.copymode(self.path, temp_name)
            os.replace(temp_name, self.path)
            if os.name != "nt":
                try:
                    dir_fd = os.open(str(self.path.parent), os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except OSError:
                    pass
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
