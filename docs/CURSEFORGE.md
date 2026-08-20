# CurseForge Integration

PalServer Manager is developing CurseForge integration for Palworld server administrators.

The integration is intended to allow administrators to:

- Search supported Palworld mods
- View mod metadata and author information
- Check Linux dedicated-server compatibility
- Identify dependencies
- Download files through the official CurseForge API
- Install supported mods onto Palworld servers
- Track installed mod versions
- Detect available updates

## Distribution

PalServer Manager respects CurseForge project distribution settings.

Files are downloaded only when made available through the official CurseForge API. PalServer Manager does not independently mirror or rehost CurseForge mod files.

Project names, authorship, source information, and links back to the original CurseForge project are retained.

## Linux Compatibility

PalServer Manager currently targets native Linux Palworld dedicated servers.

Because Palworld's official server-side mod workflow is primarily Windows-focused, PalServer Manager performs additional compatibility checks before allowing automated installation on Linux.

Packages may be classified as:

- Linux Verified
- Linux Candidate
- Untested
- Windows Only
- Client Only
- Unsupported

Windows-only binaries are not installed onto native Linux Palworld servers.
