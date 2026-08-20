# Native Linux mod catalog

PalServer Manager 0.7+ adds a **Browse Linux Mods** tab to the per-server **Mods & Runtime** page. The browser is intentionally conservative: it is for native Linux dedicated servers using the community UE4SS runtime, not for blindly installing Windows Palworld mods.

## Providers

### Curated Linux

The built-in curated provider works without any third-party credential. Entries are only marked **VERIFIED** when the project author explicitly documents native Linux/UE4SS server support or the PalServer Manager project has independently validated the package.

The initial curated catalog includes **Admin Commands** from CurseForge because its author explicitly documents UE4SS support on Linux and describes it as a server-side mod.

### CurseForge live search

Live CurseForge search uses the official CurseForge REST API and therefore requires the administrator's own API key. Enter the key on the **Browse Linux Mods** tab or set the desktop environment variable:

```text
CURSEFORGE_API_KEY=your-key
```

The key is used only by the desktop manager to query/download CurseForge content. It is not forwarded to PalServer Manager agents or placed in generated mod packages.

The manager resolves Palworld's CurseForge game ID dynamically rather than hard-coding it. The resolved ID is cached locally.

## Linux compatibility states

- **VERIFIED** — native Linux support is explicitly documented or validated.
- **COMPATIBLE / candidate** — catalog metadata and/or archive layout indicate a server-side UE4SS Lua/native Linux package, but this does not mean every game hook has been runtime-tested by PalServer Manager.
- **UNTESTED** — server relevance or native Linux behavior cannot be established safely from metadata alone. Installation requires an additional confirmation and the archive must still pass inspection.
- **WINDOWS ONLY** — the package is explicitly Windows-only or contains only Windows UE4SS DLLs.
- **UNSUPPORTED** — the archive contains an installer/script or layout PalServer Manager will not execute automatically.

## One-click install safety checks

Before an upstream archive is sent to a Linux node, the desktop manager opens and inspects the ZIP. Automatic installation is currently limited to layouts it can map safely into the selected server's native Linux UE4SS directory.

Recognized examples include:

```text
Mods/MyMod/scripts/main.lua
Win64/ue4ss/Mods/MyMod/Scripts/main.lua
MyMod/scripts/main.lua
MyMod/libs/main.so
```

Windows-style `Scripts` is normalized to lowercase `scripts`, because Linux paths are case-sensitive. Native C++ packages must provide `.so` files under `libs/`; Windows `.dll`-only packages are rejected.

The scanner also rejects:

- `../` ZIP path traversal
- `.exe`, `.bat`, `.cmd`, and `.ps1` installers
- DLL-only UE4SS packages
- archives with no recognizable UE4SS Lua/native-Linux layout

A successful conversion becomes an ordinary PalServer Manager `palserver-mod.json` managed package, so the same backup, enable/disable, rollback, restart, manifest, and client-pack machinery is reused.

One-click catalog installation requires a **0.7.0 or newer agent** on the selected node. Browsing/searching remains desktop-side, but 0.7.0 adds agent-side support for catalog packages that manage multiple UE4SS mod folders.

## Required dependencies

For live CurseForge results, PalServer Manager reads required file dependencies from CurseForge and builds a dependency-first installation plan. Already-installed managed catalog packages are skipped. The manager owns the UE4SS runtime itself, so an upstream runtime dependency is not used to replace the manager-controlled runtime.

If Mod Support is disabled on the selected server, a catalog installation enables the managed runtime before installing the requested package.

## Limitations

A structurally portable Lua archive can still call engine functions that do not work with a particular Palworld or native Linux UE4SS build. **VERIFIED** is therefore stronger than **COMPATIBLE**. Keep automatic backups enabled and use runtime validation/logs after changing a modset.

The first release focuses on CurseForge plus a curated native-Linux list. GitHub release/catalog integration can use the same provider abstraction later without changing the agent-side managed package format.
