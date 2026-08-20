# Changelog

## 0.7.0 - Native Linux Mod Catalog and One-Click Installs

- Added a **Browse Linux Mods** tab to the per-server Mods & Runtime page.
- Added a built-in **Curated Linux** catalog that works without third-party credentials and starts with a verified native-Linux Admin Commands entry.
- Added live CurseForge REST API search using the administrator's own API key; the key stays on the desktop manager and is never sent to Linux agents.
- Palworld's CurseForge game ID is resolved dynamically and cached locally instead of being hard-coded.
- Added Linux compatibility labels for verified, structurally compatible/candidate, untested, Windows-only, and unsupported packages.
- Added a pre-install ZIP scanner that maps common Windows UE4SS Lua layouts to native Linux `Pal/Binaries/Linux/Mods`, normalizes `Scripts` to `scripts`, accepts native `.so` libraries, and rejects DLL-only packages.
- Added archive path-traversal and executable/script-installer rejection before any upstream package is sent to a server.
- Added one-click catalog installation that automatically enables the managed UE4SS runtime when needed and reuses the existing transactional managed-package, backup, restart, and rollback system.
- Added required CurseForge dependency resolution with dependency-first installation and already-installed package skipping.
- One-click catalog installation is gated to agent 0.7.0+ so multi-folder catalog packages cannot be partially enabled by older agents; catalog browsing remains desktop-side.
- Catalog-generated packages retain provider/project/file metadata so later update detection can be layered onto the same manifest.
- Managed packages can now declare multiple `ue4ss_mod_names`, allowing one upstream package to manage more than one UE4SS mod folder.
- Added native Linux mod-catalog tests covering curated metadata, CurseForge API behavior, Lua path normalization, native `.so` mapping, Windows DLL rejection, and ZIP path traversal.
- Added `docs/MOD_CATALOG.md` documenting providers, API-key behavior, compatibility levels, safety scanning, dependency handling, and limitations.

## 0.6.1 - Mod Agent Compatibility UX

- Detects pre-0.6.0 Linux agents that do not expose the new mod-management API instead of surfacing raw HTTP 404 errors.
- The Mods & Runtime page now shows `AGENT UPDATE REQUIRED`, disables unsupported mod actions, and provides a clear Remote Hosts → Update Agent recovery path.
- All remote mod operations translate missing mod API routes into an actionable node/agent-version compatibility message.
- Restores normal mod controls automatically after the selected node is upgraded and the page is refreshed.

## 0.6.0 - Per-Server Mod Runtime and Managed Mod Packages

- Added a new per-server **Mods & Runtime** page that follows the selected Node -> Server context.
- Added Linux x86-64 UE4SS runtime installation, enable/disable state, version/source/hash tracking, runtime health validation, process-load verification, and UE4SS log collection.
- Mod-runtime changes now use the existing operation lock, create a safety backup, save/stop the running server, apply the change, restart it, and validate the runtime.
- If enabling the community runtime prevents a previously running server from returning healthy, PalServer Manager automatically reverts that instance to vanilla startup instead of leaving it in a runtime crash loop.
- Palworld updates now repair/re-stage the enabled runtime configuration and validate UE4SS after the updated server restarts.
- Added a generic `palserver-mod.json` ZIP package format with separate `server/` and `client/` payload roots, package type/runtime metadata, compatibility metadata, and server/client-required classification.
- Added managed mod install, enable, disable, remove, and modset-version tracking. Existing files overwritten by a managed package are preserved and restored on disable/removal.
- Added package preflight validation and rollback so malformed packages cannot leave partially installed live-server files behind.
- Added `type=custom-pal` package support as the first foundation for custom-Pal packages without hardcoding individual Pal behavior into the manager.
- Added client-mod-pack generation containing only enabled client-required assets plus a synchronized manifest/README.
- When an enabled managed package requires client files, the manager enables Palworld's `bAllowClientMod` setting when that setting is available.
- Added authenticated agent API endpoints and fleet/remote-manager methods for runtime control, validation, logs, managed package upload, enable/disable/remove, and client-pack download.
- Added safe cleanup of systemd service drop-in directories when uninstalling a Palworld server so old runtime overrides cannot affect a later server that reuses the service name.
- Added mod subsystem tests covering vanilla state, runtime requirements, custom-Pal package metadata, client packs, enable/disable restoration, transaction-safe validation, and path-traversal rejection.
- Linux mod support is explicitly labeled as a community UE4SS runtime rather than an officially supported Pocketpair Linux mod mechanism.

## 0.5.0 - Node and Server Context Routing

- Added a persistent Node selector to the header immediately to the left of the server selector.
- Server selector is now filtered to only Palworld servers hosted by the selected node.
- Selecting a node automatically selects that node's current/first managed server; empty nodes show `No servers on this node` and do not leak another node's server data.
- Added persistent `active_fleet_host_id` configuration so node context survives manager restarts.
- Connection page is now node-scoped and loads/saves SSH/direct-agent details for the selected node instead of always showing the original host connection.
- Saving Connection settings resets and verifies only the selected node's cached connection/tunnel.
- Servers page is now filtered to the selected node; Remote Hosts remains the fleet-wide host inventory.
- Server-specific pages are guarded against stale cross-node context and route through the server selected on the active node.
- Header connection status now uses the selected node's connection mode and host identity.
- Health details no longer borrow the primary server's REST port or thresholds when another server/node is selected.
- Fleet selection, server deletion/uninstall, host removal, and server switching now keep node/server context synchronized.

## 0.4.9

- New Palworld installations now close the setup modal immediately after the user clicks Install instead of leaving a long-running modal on screen.
- Added background Palworld installation jobs to the Linux agent with incremental progress polling from the desktop manager.
- Remote Hosts now streams real SteamCMD/bootstrap/download output into the persistent Agent Provisioning Console while installation is running.
- Added explicit installation phases for host-local validation, SteamCMD preparation/self-update, Palworld app 2394010 download/update, configuration creation, systemd registration, agent registration, REST settings, and service startup.
- Live installation polling automatically retries temporary SSH/agent transport interruptions without stopping the server-side installation job.
- New-server provisioning now requires agent 0.4.9+ so the GUI can guarantee non-blocking modal behavior and live installation output.

## 0.4.8

- Fixed false agent-update failures on Windows when restarting the remote agent aborts the pre-update HTTP/SSH management connection with WSAECONNABORTED (10053).
- Agent updates now discard the stale tunnel and retry a read-only host-info request through a fresh SSH tunnel before reporting connectivity failure.
- A successfully installed agent is no longer mislabeled as a failed update merely because immediate post-restart API verification is delayed.
- Provisioning console now explains expected reconnect attempts after an agent restart.

## 0.4.6

- Fixed fleet port validation so game/REST ports are scoped to the physical agent host rather than treated as manager-global resources. Different remote hosts may now intentionally reuse 8211/8212, identical service names, and identical install paths.
- Fixed newly provisioned Linux agents creating a synthetic `Primary Server` instance that could falsely reserve 8211/8212 before any Palworld server was linked. Fresh agents now start in host-only mode with zero server instances.
- Added backward-compatible cleanup for 0.4.0-0.4.5 provisioned hosts: when installing the first manager-linked server, an untouched synthetic default instance is reused instead of causing a false local conflict.
- Added host-local preflight conflict checks before SteamCMD download so genuine collisions on the same Linux host fail immediately and clearly.
- Clarified the remote install modal that ports, service names, and install paths only need to be unique on the selected physical host.

## 0.4.5

- Hardened automatic SteamCMD execution on Ubuntu/Linux hosts. SteamCMD now runs with the Palworld service account's real HOME environment (`sudo -u USER -H` when available, with an explicit HOME/USER/LOGNAME runuser fallback).
- Split the SteamCMD self-update/bootstrap from the Palworld app installation so first-run restart behavior is handled before app 2394010 is installed.
- Added one automatic retry when SteamCMD exits non-zero immediately after its own `Restarting steamcmd by request` self-update cycle.
- Improved remote installation errors so both SteamCMD stdout and stderr are preserved instead of hiding the useful Steam error behind launcher startup messages.
- New-server installation now requires agent 0.4.5+ so older hosts cannot continue through the known-broken SteamCMD launch path.

## 0.4.3 - Compact Host Controls and Install Modal

- Places all six Selected Host Controls actions on one compact horizontal row.
- Removes the unnecessary second controls row and large column gaps.
- Keeps the action group left-aligned with consistent 8 px spacing and a trailing stretch.
- Moves the new Palworld server installation form out of the Remote Hosts page and into a focused modal dialog.
- The modal still performs host discovery asynchronously and fills conflict-safe install path, service, game-port, and REST-port defaults before installation.
- Installation errors remain visible inside the modal while detailed host activity continues to be recorded in the provisioning console.

## 0.4.2 - Persistent Remote Host Controls

- Added a Selected Host Controls panel for every previously provisioned Linux agent host.
- Added persistent Install New Palworld Server workflow so users can return later and install a server without reprovisioning the host.
- Added Verify Agent, Discover & Link Servers, Update Agent, Update Linux, and Restart Agent controls for selected hosts.
- New-server defaults are calculated from actual Palworld installations discovered on the selected host to avoid common path/service/port collisions.
- Added host selection persistence and clear selected-host connection details.
- Palworld installation actions now report start, success, and failure in the embedded provisioning console.
- Added safe cached-tunnel reset after agent updates/restarts so the manager reconnects cleanly.

## 0.4.1 - Realtime Provisioning Console

- Added an embedded Agent Provisioning Console to the Remote Linux Hosts page.
- Streams SSH provisioning output into the GUI while Debian/Ubuntu updates, prerequisites, Python packages, and systemd setup are running.
- Added timestamped provisioning phases, automatic scrolling, a Clear button, and visible success/failure messages.
- Kept agent authentication secrets out of the streamed console output.
- Added live host-link and Palworld discovery status after the agent comes online.

## 0.4.0 - Fleet Provisioning Alpha

- Added manager-global server IDs starting at 001 with automatic incrementing.
- Added editable server names that also update Palworld ServerName when possible.
- Added multi-host fleet routing so one desktop manager can use multiple loopback agents over independent SSH tunnels.
- Added Remote Linux Hosts page with SSH connectivity testing and automatic agent installation/upgrades.
- Linux bootstrap can update Debian/Ubuntu packages, install the agent, keep it private on 127.0.0.1, and report reboot-required state without rebooting automatically.
- Added remote Palworld discovery under /opt, /srv and /home and automatic linking of discovered servers.
- Added inline new-server installation workflow when a new host has no Palworld installation.
- Added automatic SteamCMD/Palworld app 2394010 installation, systemd service creation, private REST API setup, and manager linking on supported Linux hosts.
- Remote bootstrap currently requires SSH key authentication and root or passwordless sudo. Windows Palworld host provisioning remains planned for a later release.

## 0.2.0 - 2026-08-13

- Moved routine server/API refresh work off the Qt event loop so sidebar navigation and page switching remain responsive.
- Replaced the 10-second full-dashboard polling loop with a 30-second lightweight header refresh outside the Dashboard and a single bundled overview request on the Dashboard.
- Added a lightweight Watchdog snapshot endpoint and reduced Watchdog polling to every three seconds.
- Removed global wait-cursor behavior from manager operations and added non-blocking action workers.
- Added platform-aware player labeling for Steam, Xbox/Microsoft GDK, PlayStation, Epic/EOS and unknown platform prefixes.
- Replaced the Steam-specific player ID column with generic Platform, Account and User ID columns.
- Added an integrated Ban Manager with a persistent server-side registry for bans performed through PalServer Manager and one-click unban.
- Added responsive player-table column hiding while keeping player name, platform, level, ping and user ID visible.
- Changed Save/Apply buttons on forms to normal compact sizes instead of full-width action bars.
- Kept every safe tool on one responsive Tools page using compact three-column cards at normal desktop sizes.

## 0.1.9 - 2026-08-13

- Fixed blank Installation & Service and Network Diagnostics cards caused by the human-label renderer method signature.
- Added visible diagnostic fallback cards when the remote agent cannot return data.
- Clarified Live Watchdog CPU metrics: Host CPU Load versus PalServer Process CPU.
- Added explanatory CPU text noting that PalServer process CPU can exceed 100% because 100% represents one logical CPU core.

## 0.1.7 - 2026-08-13

- Made header player count compact white text; SSH status remains green when connected and red when disconnected.
- Added Live Watchdog to the left sidebar and changed its visible-page refresh cadence to one second.
- Fixed PalServer process resolution so watchdog CPU/RAM target the real game process instead of the wrapper shell.
- Removed all direct-action tiles from the Tools page and made tool cards responsive, word-wrapped navigation cards.
- Forced all scroll/report/tool backgrounds to the application navy theme and removed gray viewport areas.
- Added responsive dashboard metric wrapping for smaller window sizes.
- Redacted server/admin passwords from non-default settings reports.


## 0.1.5 - Integrated UI consistency pass

- Replaced sidebar/tool popup dialogs with integrated pages inside the main window.
- Added full integrated Players, Worlds, Settings, Backups, Automation, Health, Diagnostics, Connection, Watchdog, Server Setup and Report pages.
- Replaced raw JSON result windows with card/table report views.
- Added human-readable Palworld setting names, detailed descriptions, and accepted-value guidance based on the current official server configuration guide.
- Added inline confirmations and notifications so normal administration no longer opens child windows.
- Reduced the top connection status to a compact inline indicator.
- Increased sidebar, toolbar, dashboard, and metric icon sizes.
- Updated About and footer credit to Supr Solutions LLC.

## 0.1.4 - Exact-reference UI merge
- Rebuilt the Windows/PySide6 dashboard around the supplied target reference.
- Full-width hero banner with bundled artwork and branded logo.
- 215px sidebar, server overview strip, six-card metric row, colored quick actions.
- Recent logs, live connected-player table, health checklist, and next-automation panels.
- Preserved the real SSH agent, Palworld REST API, settings, backups, worlds, scheduler, diagnostics, and service controls.
- Added server FPS average to status payload for the dashboard.

# Changelog

## 0.1.0-alpha - 2026-08-13

Initial public-preview architecture:

- PySide6 Windows/Linux desktop GUI
- paginated CLI and GUI tool catalogs
- local Windows/Linux server control
- secure remote manager agent
- SSH-tunnel and direct HTTPS remote modes
- verified PalWorldSettings.ini editor
- Steam update checker/updater
- backups, world archives and scheduler
- player management through Palworld REST API
- dashboard, watchdog, health, logs and diagnostics

## 0.1.6 - Integrated UI polish
- Reworked Health, Diagnostics, Tools, Crash History, and Non-default Settings pages for compact card-based layouts.
- Added live player count to the hero header next to connection status.
- Increased navigation, page, and tool icon sizing.
- Health page now exposes the exact warning/critical checks that determine overall status.
- Non-default settings reports now include human-readable names, descriptions, defaults, current values, and accepted values.

## 0.3.0 - Multi-instance management

- Added first-class multi-instance support. One agent can manage multiple independent Palworld servers.
- Added a server-instance selector in the main header and an integrated Servers page for creating, selecting, configuring, and removing instances.
- Added agent `/v1/instances` management endpoints and `X-PalManager-Instance` routing for all existing management APIs.
- Added one scheduler per enabled server instance so backups and update automation remain isolated.
- Migrates existing single-server configurations automatically into the `default` instance without changing existing paths, ports, credentials, or service name.
- New instances receive independent default install, backup, log, game-port, REST-port, and service-name values.
- Direct-process PID tracking is now instance-specific and process adoption is restricted to the selected instance install path.
- Dashboard Start is disabled while the selected server is running. Stop and Restart are disabled while it is stopped or while status is still loading.
