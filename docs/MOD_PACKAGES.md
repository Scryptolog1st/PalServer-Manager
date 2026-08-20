# PalServer Manager managed mod packages

PalServer Manager 0.6+ can install a generic managed mod package on the currently selected Palworld server. Packages are ZIP files with `palserver-mod.json` at the archive root and optional `server/` and `client/` payload trees.

## Minimal package

```text
my-mod.zip
├── palserver-mod.json
├── server/
│   └── Pal/Binaries/Linux/Mods/MyMod/scripts/main.lua
└── client/
    └── Paks/MyMod.pak
```

Everything below `server/` is copied relative to the selected Palworld installation root. Everything below `client/` is retained by PalServer Manager for client-pack generation.

## Manifest

```json
{
  "id": "void-jetragon",
  "name": "Void Jetragon",
  "version": "1.0.0",
  "runtime": "ue4ss",
  "type": "custom-pal",
  "server_required": true,
  "client_required": true,
  "ue4ss_mod_name": "VoidJetragon",
  "compatibility": {
    "palworld": "1.0.x",
    "ue4ss": ">=1.0.0"
  }
}
```

### Fields

- `id`: stable package identifier. PalServer Manager normalizes it to a safe slug.
- `name`: human-readable mod name.
- `version`: package version displayed in the manager and client-pack manifest.
- `runtime`: normally `ue4ss` or `ue4ss-linux` for native Linux UE4SS packages. Packages declaring UE4SS cannot be installed until Mod Support is enabled for the server.
- `type`: generic classification such as `generic`, `admin`, `levels`, or `custom-pal`. `custom-pal` is package support only; it does not yet provide a graphical Pal/model/stat editor.
- `server_required`: when `true`, the archive must contain at least one `server/` payload file.
- `client_required`: when `true`, the archive must contain at least one `client/` payload file and the package is included in generated client mod packs.
- `ue4ss_mod_name`: optional primary UE4SS mod name. When supplied, PalServer Manager maintains its `Mods/mods.txt` enabled/disabled entry.
- `ue4ss_mod_names`: optional list of UE4SS mod names for packages that install more than one mod folder. The single `ue4ss_mod_name` remains supported for compatibility.
- `compatibility`: free-form compatibility metadata shown in the GUI. It is descriptive metadata in 0.6.0; package authors should provide accurate Palworld/runtime constraints.

## Lifecycle and safety

Installing, enabling, disabling, or removing a managed mod uses the normal PalServer Manager controlled-change workflow: save when possible, create a safety backup, stop the server if it is running, make the change, restart it, and validate the enabled runtime.

The package is fully validated before live payload copying begins. If a package overlays an existing server file, the previous file is stored in PalServer Manager metadata and restored when the package is disabled or removed. Failed installs roll their live-file changes back.

Archive path traversal such as `../` is rejected.

## Client mod packs

`Download Client Pack` builds a ZIP for the selected server containing only assets from **enabled** packages where `client_required=true`. It also includes a generated `manifest.json` describing the server modset version and package versions.

The generated pack is intentionally a transport bundle rather than a client-side automatic installer. Players still need to put the included assets in the correct Palworld client locations for those mods.

## Custom Pal packages

A custom Pal can be distributed as a normal package by using `"type": "custom-pal"`, putting its server-side logic/data below `server/`, and any models/materials/assets needed by players below `client/`.

0.6.0 provides the package/runtime foundation. A future graphical Custom Pal editor can generate these packages without changing the underlying server-management abstraction.

## Catalog-generated packages

The 0.7+ Linux Mod Catalog converts supported upstream archives into this same managed-package format. Catalog-generated manifests also include a `source` object containing the provider, upstream project ID, file ID, and source URLs when available. This metadata is informational in 0.7.0 and is intentionally preserved so update detection can later compare the installed package with its upstream release.

See [`MOD_CATALOG.md`](MOD_CATALOG.md) for the archive scanner and provider behavior.
