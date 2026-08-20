# PalServer Manager

PalServer Manager is a desktop management platform for Palworld dedicated servers.

It is designed to let server administrators manage multiple Palworld servers across multiple remote Linux hosts from a single desktop application.

## Current Features

- Multi-node server management
- Multiple Palworld server instances
- Remote Linux host provisioning over SSH
- Automatic PalServer Manager agent installation
- Automatic Palworld dedicated server installation
- Start, stop, restart, and save controls
- Player management
- Ban management
- Server health monitoring
- Live watchdog and process monitoring
- Server configuration management
- World management
- Automated backups
- Scheduled updates and maintenance
- SteamCMD integration
- Diagnostics and logging
- Native Linux UE4SS mod runtime management
- Linux-compatible mod discovery
- CurseForge mod integration
- Client mod-pack generation
- Remote agent updating and management

## Platform

### Desktop Manager

PalServer Manager currently uses:

- Python
- PySide6 / Qt 6
- Qt Style Sheets
- OpenSSH

### Linux Agent

The remote management agent uses:

- Python
- FastAPI
- Uvicorn
- systemd
- psutil
- SteamCMD

Ubuntu 24.04 is currently the primary supported Palworld server host platform.

## Mod Management

PalServer Manager includes support for native Linux Palworld modding through a community UE4SS Linux runtime.

The mod-management system distinguishes between:

- Linux-compatible UE4SS Lua mods
- Native Linux UE4SS mods
- Server-only mods
- Client + server mods
- Windows-only mods
- Unsupported or unverified packages

CurseForge integration is being developed to provide searchable mod discovery, compatibility checks, dependency handling, and one-click installation for supported Palworld mods.

PalServer Manager respects mod-author distribution settings and does not rehost CurseForge mod files.

## Project Status

PalServer Manager is currently under active development.

The application source code is not currently published in this repository. This repository is presently used for project information, documentation, integration verification, releases, and future issue tracking.

## Disclaimer

PalServer Manager is an independent project and is not affiliated with or endorsed by Pocketpair, Inc., Palworld, CurseForge, Overwolf, Valve, or Steam.

Palworld and related names and trademarks belong to their respective owners.

## Developer

Created and developed by **Supr Solutions LLC**.
