# PalServer Manager

[![CI](https://github.com/Scryptolog1st/PalServer-Manager/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Scryptolog1st/PalServer-Manager/actions/workflows/ci.yml)
![Version](https://img.shields.io/badge/version-0.7.0-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Status](https://img.shields.io/badge/status-alpha-orange)

**Unofficial Palworld dedicated-server administration for Windows and Linux.**

PalServer Manager combines a local server controller, a secure remote agent, a paginated terminal manager, and a real PySide6 desktop GUI. It is designed so the management computer does **not** need to be on the same LAN as the Palworld host.

> Palworld and Pocketpair are trademarks of their respective owners. This project is not affiliated with or endorsed by Pocketpair.

## What is included

- Windows desktop GUI using **PySide6** (real native window UI)
- Linux/Windows terminal manager with **paginated tools**
- Linux systemd control
- Windows Service control, with direct-process fallback
- Local or remote management
- Multi-node fleet management with independent Linux agents, a header Node selector, and per-node server filtering
- Remote management through:
  - **SSH tunnel** to a loopback-only management agent (recommended)
  - Direct **HTTPS** to the management agent when explicitly enabled and TLS configured
- Live dashboard and watchdog
- VM/host CPU, RAM and storage usage
- PalServer process CPU/RAM
- Palworld server time, service state, PID and game port
- Palworld console/journal log viewer
- Health states: healthy / warning / critical
- Steam build/update checker
- Backup -> graceful stop -> SteamCMD update/validate -> restart workflow
- Start / stop / restart tools
- Verified `PalWorldSettings.ini` editing
- Per-server **Mods & Runtime** management for native Linux Palworld hosts
- Community UE4SS Linux runtime installation, version tracking, enable/disable, validation, and log inspection
- Managed mod ZIP packages with server-only vs client-required classification, compatibility metadata, and custom-Pal package type support
- Generated client mod packs containing only the client assets required by the selected server's enabled modset
- Post-Steam-update runtime repair/validation and automatic safety backups before mod/runtime changes
- Setting search, categories and descriptions
- Compare current settings with `DefaultPalWorldSettings.ini`
- Configuration profiles
- Backup manager: create/list/restore/delete/retention
- Automatic backups from the always-on agent
- Maintenance-window automatic update support
- Player list / kick / ban / unban
- Broadcast announcements
- Immediate world save
- Network diagnostics
- Installation diagnostics
- Crash/error summary
- Self-update framework for GitHub releases
- Cross-platform configuration and autodetection defaults

## Architecture

```text
                 REMOTE WINDOWS/LINUX ADMIN PC

     +---------------------------------------------+
     | PalServer Manager                          |
     |                                             |
     | Windows: PySide6 GUI                        |
     | Linux:   CLI/TUI (GUI also optional)        |
     +---------------------+-----------------------+
                           |
                  SSH tunnel (recommended)
                           |
                           v
     +---------------------------------------------+
     | PalServer Manager Agent                     |
     | 127.0.0.1:8765 by default                   |
     | token authenticated                         |
     +---------------------+-----------------------+
                           |
             +-------------+-------------+
             |                           |
             v                           v
      OS / SteamCMD                 Palworld REST API
      service control               127.0.0.1:8212
      backups / files               players/metrics/save
      system metrics                kick/ban/announce
```

The built-in Palworld REST API stays local to the game server. PalServer Manager's own agent is the remote-management boundary.

## Why SSH is the default remote design

Pocketpair explicitly warns that Palworld's built-in REST API and RCON are not designed to be exposed directly to the Internet. PalServer Manager therefore talks to Palworld's API locally and recommends keeping the manager agent on `127.0.0.1` too, then tunneling to it with SSH or a private VPN.

Official Palworld server documentation:

- REST API: https://docs.palworldgame.com/category/rest-api/
- REST API introduction/security: https://docs.palworldgame.com/api/rest-api/palwold-rest-api/
- Server configuration: https://docs.palworldgame.com/settings-and-operation/configuration/
- Server deployment: https://docs.palworldgame.com/getting-started/deploy-dedicated-server/
- RCON deprecation: https://docs.palworldgame.com/api/rcon/

## Requirements

- Python 3.11+
- Palworld Dedicated Server
- SteamCMD for game updates
- Linux: systemd is recommended
- Windows: PalServer can be a Windows Service or launched directly by the manager
- For player/metrics/broadcast tools: Palworld REST API enabled and an admin password configured

### Python extras

```bash
# CLI/local manager
pip install .

# Remote agent
pip install ".[agent]"

# Desktop GUI
pip install ".[gui]"

# Everything
pip install ".[all]"
```

## Linux installation

From the project directory:

```bash
sudo ./scripts/install-linux.sh
```

If this host should accept the recommended SSH-tunnel connections directly, you can optionally enable/install SSH during setup:

```bash
sudo ./scripts/install-linux.sh --enable-ssh
```

The default installer does **not** enable SSH automatically.

The installer creates:

```text
/opt/palserver-manager/venv
/etc/palserver-manager/config.json
/etc/systemd/system/palserver-manager-agent.service
/usr/local/bin/palserver-manager
/usr/local/bin/palserver-agent
```

The remote agent binds only to:

```text
127.0.0.1:8765
```

Edit `/etc/palserver-manager/config.json`, especially:

- Palworld install path
- SteamCMD path
- service name
- admin password
- REST API port
- backup path

Then:

```bash
sudo systemctl restart palserver-manager-agent
sudo systemctl status palserver-manager-agent
```

## Windows installation

Open **PowerShell as Administrator**:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install-windows.ps1
```

If this Windows game-server host should accept SSH tunnels, the installer can also enable the Windows OpenSSH Server feature:

```powershell
.\scripts\install-windows.ps1 -EnableOpenSSH
```

The default installer does **not** enable OpenSSH automatically.

This installs the Python environment, creates a **PalServer Manager** desktop shortcut, and registers the loopback management agent as a startup task.

For a distributable Windows app, build the PyInstaller package:

```powershell
.\scripts\build-windows.ps1
```

The Windows release should be code-signed before broad public distribution.

## Windows GUI

Run:

```powershell
palserver-gui
```

or use the desktop shortcut created by the installer.

The GUI includes:

- dashboard cards
- quick start/stop/restart/save/backup/update actions
- paginated Tools page
- settings table/editor
- backup manager
- player manager
- live watchdog window
- log viewer
- remote connection dialog

The same GUI can also run on Linux desktops if PySide6 is installed.

## Linux mod runtime and managed mod packages

PalServer Manager 0.6+ treats modding as a **per-server runtime layer**. A server may remain vanilla or enable the community native Linux UE4SS runtime. Runtime state, managed packages, health, log output, and client-pack generation belong to the currently selected Node -> Server context.

The **Mods & Runtime** page can:

- enable/disable the Linux UE4SS runtime with a safety backup and controlled restart
- validate that runtime files exist and, when the server is running, check whether `libUE4SS.so` is actually mapped into the Palworld process
- display recent `UE4SS.log` output and runtime error/refused-hook markers
- install generic managed ZIP packages described by `palserver-mod.json`
- enable, disable, or remove individual managed packages
- distinguish server-only packages from packages that require client files
- build a versioned client mod pack from the enabled client-required packages
- preserve/restore pre-existing files when a managed package overlays a server file
- re-stage/validate the runtime after a SteamCMD Palworld update
- automatically revert to vanilla startup if a newly enabled runtime fails validation on a server that was previously running
- browse a native-Linux-focused mod catalog and install supported upstream packages with one click
- use a built-in curated Linux list without an API credential or live CurseForge search with the administrator's own API key
- inspect upstream ZIPs before upload, normalize common UE4SS Lua layouts for Linux, accept native `.so` plugins, and reject Windows DLL-only/unsafe installer packages
- resolve required CurseForge dependencies and install them dependency-first

See [`docs/MOD_PACKAGES.md`](docs/MOD_PACKAGES.md) for the managed package format and [`docs/MOD_CATALOG.md`](docs/MOD_CATALOG.md) for catalog providers, Linux compatibility labels, API-key handling, and one-click-install safety rules.

> **Linux mod support is experimental/community-backed.** Pocketpair's official server-mod support should not be assumed to cover this native Linux workflow. PalServer Manager keeps the runtime version independent from the Palworld build and surfaces validation/log failures so an incompatible runtime can be disabled without wiping the world.

## Terminal manager

Run:

```bash
palserver-manager
```

The terminal tool list is paginated. Use:

```text
N  next page
P  previous page
R  refresh
G  launch GUI
Q  quit
```

Useful non-interactive commands:

```bash
palserver-manager status --update
palserver-manager health
palserver-manager start
palserver-manager stop
palserver-manager restart
palserver-manager backup
palserver-manager update
palserver-manager logs --lines 200
palserver-manager logs --errors-only
palserver-manager players
palserver-manager gui
```

## Remote administration from another network

### Recommended: SSH tunnel mode

1. Install and run `palserver-agent` on the Palworld host.
2. Leave it bound to `127.0.0.1:8765`.
3. Make SSH reachable on the host (directly, through your firewall, or through a VPN).
4. Copy the agent token from the server's manager config.
5. On the remote Windows GUI, open **Connection**.
6. Choose `ssh`.
7. Enter:
   - public hostname/IP
   - SSH port
   - SSH user
   - private key path (optional when ssh-agent/default keys are configured)
   - agent token

PalServer Manager starts an OpenSSH local tunnel similar to:

```bash
ssh -N -L 18765:127.0.0.1:8765 user@server.example.com
```

The GUI then talks to `127.0.0.1:18765`; the agent and Palworld REST API remain private on the server.

### Direct HTTPS mode

Direct Internet access is also supported for deployments that cannot use SSH/VPN. It is intentionally disabled by default.

To enable it on the agent:

```json
{
  "agent": {
    "bind_host": "0.0.0.0",
    "port": 8765,
    "allow_direct_wan": true,
    "tls_cert": "/path/to/fullchain.pem",
    "tls_key": "/path/to/privkey.pem"
  }
}
```

Then tightly firewall the port, use a strong generated management token, and use a valid TLS certificate. **Do not use plain HTTP on the Internet.**

## Palworld REST API setup

Player/metrics/save/broadcast/graceful-shutdown tools use Pocketpair's REST API. In `PalWorldSettings.ini`, enable it and set an admin password:

```ini
RESTAPIEnabled=True
RESTAPIPort=8212
AdminPassword="a-strong-password"
```

Keep `8212` bound/firewalled for local use. Configure the same admin password in PalServer Manager's local agent config.

The manager supports these documented Palworld API operations:

- server info
- player list
- server settings
- server metrics
- announcements
- kick
- ban
- unban
- save
- graceful shutdown
- force stop

## Backups

Manual backups include the Palworld save and configuration directories in a compressed `.tar.gz` archive.

Default behavior:

- backup before game update
- backup before restore
- automatic backup every 120 minutes when agent is running
- keep 30 backups

Backup restore stops the server, restores files, and returns the service to its previous running state.

## Automatic updates

The agent can:

- periodically check the Steam public branch
- optionally install updates
- restrict installs to a maintenance window
- optionally require zero connected players
- make a backup before updating

Automatic installs are disabled by default.

## Verified configuration writes

PalServer Manager does not trust its own in-memory setting state. An edit does this:

1. Re-read the live INI from disk.
2. Confirm the requested key exists exactly once.
3. Create a backup.
4. Atomically replace the INI.
5. Flush the write.
6. Re-read the actual file.
7. Verify the exact value on disk.

This specifically prevents the stale-setting behavior that can make a UI show `True` while the real INI still contains `False`.

## Settings profiles

Built-in examples:

- Vanilla
- Casual PvE
- Hardcore
- PvP

Custom profiles are stored in the manager data directory. Applying a profile uses the same verified-write path as manual edits.

## Security model

- The management token is generated with `secrets.token_urlsafe`.
- The agent requires `X-PalManager-Token` on all management endpoints.
- The token is compared with constant-time comparison.
- Agent API docs/OpenAPI endpoints are disabled by default.
- The agent binds to loopback by default.
- A non-loopback bind is refused unless `allow_direct_wan=true`.
- Direct WAN mode is refused without a TLS certificate and key.
- Palworld passwords are never returned by the remote manager API.
- Backup restore validates archive paths to prevent path traversal.
- Config writes are verified after disk replacement.

### Privilege note

The example Linux agent service runs as root because start/stop/update/restore operations need elevated permissions on many installations. For a hardened deployment, run a dedicated service account with narrowly scoped file permissions and sudo rules.

## Public-release checklist

Before calling the project stable:

- choose the final project/repository name
- set `github_repo` in the generated default config or release documentation
- add CI for Windows and Linux
- run unit/integration tests against fresh Palworld servers
- code-sign Windows releases
- publish checksums for release assets
- create a responsible security-reporting process
- test upgrades from previous manager versions
- test Windows Service and direct-process launch modes
- test systemd/non-systemd Linux installs
- test REST API changes after every major Palworld update

## Tests

```bash
python -m pytest
```

## Status

This release is **0.7.0 alpha**. The architecture and major features are implemented, but public distribution should be preceded by real Palworld integration testing on both Windows and Linux hosts.

## Multi-instance server management

PalServer Manager 0.3.0 can manage multiple independent Palworld dedicated servers from one agent host. Existing installations are migrated automatically to a `default` instance, so the current server keeps its existing service name, ports, paths, and credentials.

Use **Servers** in the left navigation (or the server selector in the header) to switch the active server. Every dashboard action, player/ban operation, backup, world operation, setting change, health check, diagnostic, log query, update, and automation request is scoped to the selected instance.

When adding additional servers, each instance should use a unique game port, REST API port, OS service name, save/config location, backup directory, and preferably its own install directory. The manager proposes unique defaults for new instances. After creating an instance, use **Server Setup** to finish its paths and REST credentials and make sure the corresponding OS service/process is installed on the host.

The remote agent keeps one scheduler per enabled instance. Scheduled backups and update checks therefore run independently for each managed server.


## Fleet and remote Linux host provisioning (0.4.3)

The desktop manager can maintain a fleet of Palworld servers across multiple Linux hosts. Manager-visible server IDs are assigned automatically as `001`, `002`, `003`, and so on. Each server maps to an agent-local instance on a specific host, and its display/server name can be changed later.

Use **Remote Hosts** to enter the new Linux host's SSH address, port, user, and private-key path. Automatic bootstrap currently requires public-key SSH plus either root access or passwordless sudo. The bootstrapper can update Debian/Ubuntu packages, install or upgrade the PalServer Manager agent, bind that agent to `127.0.0.1:8765`, and create a private SSH-tunnel connection profile. SSH passwords are intentionally not persisted. The page includes a timestamped realtime provisioning console that streams package-update, prerequisite, Python/pip, agent-install, and systemd output while the operation runs; generated agent authentication secrets are deliberately not written to that console.

After the agent is linked, it searches common Linux locations (`/opt`, `/srv`, and `/home`) for existing `PalServer.sh` installations. Existing servers are registered automatically. If no Palworld installation is found, the manager presents a focused installation modal with defaults for `/opt/palworld`, the `palworld` systemd service, UDP 8211, REST 8212, and 32 players. The host agent then installs SteamCMD, downloads Palworld Dedicated Server app ID `2394010`, creates the systemd service, enables the loopback REST API, starts the server, and links it back to the manager. The installation workflow is persistent: on any later manager session, select an already-provisioned host and use **Install New Palworld Server** to create another server without reinstalling the agent.

Every provisioned Linux host also has a **Selected Host Controls** panel with **Verify Agent**, **Discover & Link Servers**, **Install New Palworld Server**, **Update Agent**, **Update Linux**, and **Restart Agent** actions arranged in one compact horizontal row. New-server defaults are recalculated from the Palworld installations actually discovered on that host so the manager can propose an unused install path, service name, game port, and REST port.

The bootstrapper does **not** automatically expose management ports, alter router/NAT rules, reboot Linux, or provision Windows Palworld hosts. The Palworld game UDP port still needs the network/firewall/NAT configuration appropriate for that host.
