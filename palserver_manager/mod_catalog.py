from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, asdict
from pathlib import PurePosixPath
from typing import Any, Iterable

import requests


CURSEFORGE_API = "https://api.curseforge.com"
CURSEFORGE_PAGE_SIZE = 20

# These entries are intentionally conservative.  "verified" means the author
# explicitly documents native Linux/UE4SS support (or the PalServer Manager
# project has validated it).  The live CurseForge browser can show a much
# larger set, but only verified/compatible server-side Lua packages receive the
# normal one-click install action.
CURATED_LINUX_MODS: list[dict[str, Any]] = [
    {
        "source": "curseforge",
        "mod_id": 1328795,
        "file_id": 7527447,
        "name": "Admin Commands",
        "slug": "admin-commands",
        "summary": "Server-side administrative commands for Palworld dedicated servers.",
        "author": "Kozejin",
        "version": "1.0.1",
        "file_name": "AdminCommands.zip",
        "game_versions": ["0.7.1"],
        "categories": ["Lua Code Mods", "Server Mods", "Utilities", "Gameplay"],
        "compatibility": "verified",
        "compatibility_detail": "Author explicitly documents UE4SS support on native Linux.",
        "server_required": True,
        "client_required": False,
        "runtime": "ue4ss",
        "download_count": 26900,
        "date_modified": "2026-07-26T00:00:00Z",
        "project_url": "https://www.curseforge.com/palworld/lua-code-mods/admin-commands",
        # CurseForge's CDN maps file IDs into <first four>/<last three>.  Live
        # API results prefer the official downloadUrl/download-url endpoint;
        # this URL only keeps the curated starter catalog useful before the
        # administrator adds their own API key.
        "download_url": "https://mediafilez.forgecdn.net/files/7527/447/AdminCommands.zip",
    },
]


class ModCatalogError(RuntimeError):
    pass


@dataclass
class CatalogItem:
    source: str
    mod_id: int | str
    file_id: int | str
    name: str
    slug: str = ""
    summary: str = ""
    author: str = ""
    version: str = ""
    file_name: str = ""
    game_versions: list[str] | None = None
    categories: list[str] | None = None
    compatibility: str = "untested"
    compatibility_detail: str = ""
    server_required: bool = True
    client_required: bool = False
    runtime: str = "ue4ss"
    download_count: int = 0
    date_modified: str = ""
    project_url: str = ""
    download_url: str = ""
    dependencies: list[int] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["game_versions"] = list(self.game_versions or [])
        data["categories"] = list(self.categories or [])
        data["dependencies"] = list(self.dependencies or [])
        return data


class CurseForgeClient:
    """Small read-only CurseForge REST client used by the desktop manager.

    API keys remain on the desktop.  They are never transmitted to a
    PalServer Manager agent.  CurseForge's documented REST API uses x-api-key,
    a maximum page size of 50, and exposes download URLs on the file model / a
    dedicated download-url endpoint.
    """

    def __init__(self, api_key: str, game_id: int = 0, session: requests.Session | None = None):
        self.api_key = str(api_key or "").strip()
        self.game_id = int(game_id or 0)
        self.session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ModCatalogError(
                "CurseForge browsing requires your own CurseForge API key. Add it on the Browse Mods tab first."
            )
        return {
            "Accept": "application/json",
            "x-api-key": self.api_key,
            "User-Agent": "PalServer-Manager/ModCatalog",
        }

    def _get(self, path: str, *, params: dict[str, Any] | None = None, timeout: int = 30) -> Any:
        try:
            response = self.session.get(CURSEFORGE_API + path, params=params, headers=self._headers(), timeout=timeout)
        except requests.RequestException as exc:
            raise ModCatalogError(f"CurseForge request failed: {exc}") from exc
        if response.status_code in {401, 403}:
            raise ModCatalogError("CurseForge rejected the API key. Verify the key and try again.")
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            detail = ""
            try:
                detail = str((response.json() or {}).get("message") or "")
            except Exception:
                detail = response.text[:300]
            raise ModCatalogError(f"CurseForge API returned HTTP {response.status_code}: {detail or exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ModCatalogError("CurseForge returned an invalid JSON response") from exc
        return payload

    def resolve_palworld_game_id(self) -> int:
        if self.game_id:
            return self.game_id
        index = 0
        page_size = 50
        while index < 10000:
            payload = self._get("/v1/games", params={"index": index, "pageSize": page_size}) or {}
            rows = payload.get("data") or []
            for row in rows:
                name = str(row.get("name") or "").strip().lower()
                slug = str(row.get("slug") or "").strip().lower()
                if name == "palworld" or slug == "palworld":
                    self.game_id = int(row.get("id") or 0)
                    if self.game_id:
                        return self.game_id
            page = payload.get("pagination") or {}
            count = int(page.get("resultCount") or len(rows))
            total = int(page.get("totalCount") or 0)
            if not rows or count <= 0 or (total and index + count >= total):
                break
            index += max(count, page_size)
        raise ModCatalogError("Palworld was not found in the games available to this CurseForge API key.")

    @staticmethod
    def _categories(row: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for item in row.get("categories") or []:
            if isinstance(item, dict):
                value = str(item.get("name") or "").strip()
            else:
                value = str(item or "").strip()
            if value and value not in out:
                out.append(value)
        return out

    @staticmethod
    def _authors(row: dict[str, Any]) -> str:
        values = []
        for item in row.get("authors") or []:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
            else:
                name = str(item or "").strip()
            if name:
                values.append(name)
        return ", ".join(values)

    @staticmethod
    def _latest_file(row: dict[str, Any]) -> dict[str, Any]:
        files = [item for item in (row.get("latestFiles") or []) if isinstance(item, dict)]
        if files:
            releases = [item for item in files if int(item.get("releaseType") or 0) == 1]
            pool = releases or files
            pool.sort(key=lambda item: str(item.get("fileDate") or ""), reverse=True)
            return pool[0]
        return {}

    @staticmethod
    def _classify(row: dict[str, Any], file_row: dict[str, Any] | None = None) -> tuple[str, str, bool, bool]:
        categories = [value.lower() for value in CurseForgeClient._categories(row)]
        name = str(row.get("name") or "").lower()
        summary = str(row.get("summary") or "").lower()
        combined = " ".join([name, summary, *categories])
        mod_id = int(row.get("id") or 0)
        if mod_id == 1328795:
            return "verified", "Author explicitly documents UE4SS support on native Linux.", True, False
        if "windows only" in combined or "windows-only" in combined:
            return "windows-only", "Project metadata explicitly indicates Windows-only support.", True, False
        if "c++ code mods" in categories:
            # Native Linux .so mods do exist, but the catalogue metadata alone
            # cannot prove the uploaded archive contains an ELF build.  The
            # archive scanner can promote these after download.
            return "untested", "C++ package requires a native Linux .so build; Windows DLLs are unsupported.", "server mods" in categories, False
        is_lua = "lua code mods" in categories
        is_server = "server mods" in categories or "server side" in combined or "server-side" in combined
        if is_lua and is_server:
            return "compatible", "Server-side UE4SS Lua package; archive is scanned before installation.", True, False
        if is_lua:
            return "untested", "UE4SS Lua is portable, but dedicated-server behavior is not confirmed by catalog metadata.", False, True
        return "untested", "Linux compatibility cannot be determined from CurseForge metadata alone.", is_server, not is_server

    def _item_from_mod(self, row: dict[str, Any], file_row: dict[str, Any] | None = None) -> CatalogItem:
        file_row = file_row or self._latest_file(row)
        compatibility, detail, server_required, client_required = self._classify(row, file_row)
        categories = self._categories(row)
        links = row.get("links") or {}
        dependency_ids = []
        for dep in file_row.get("dependencies") or []:
            if isinstance(dep, dict) and int(dep.get("relationType") or 0) == 3 and dep.get("modId"):
                dependency_ids.append(int(dep["modId"]))
        version = str(file_row.get("displayName") or "").strip()
        if not version:
            versions = file_row.get("gameVersions") or []
            version = str(versions[0]) if versions else "latest"
        return CatalogItem(
            source="curseforge",
            mod_id=int(row.get("id") or 0),
            file_id=int(file_row.get("id") or 0),
            name=str(row.get("name") or "Unnamed mod"),
            slug=str(row.get("slug") or ""),
            summary=str(row.get("summary") or ""),
            author=self._authors(row),
            version=version,
            file_name=str(file_row.get("fileName") or "mod.zip"),
            game_versions=[str(value) for value in (file_row.get("gameVersions") or [])],
            categories=categories,
            compatibility=compatibility,
            compatibility_detail=detail,
            server_required=server_required,
            client_required=client_required,
            runtime="ue4ss",
            download_count=int(row.get("downloadCount") or 0),
            date_modified=str(row.get("dateModified") or file_row.get("fileDate") or ""),
            project_url=str(links.get("websiteUrl") or ""),
            download_url=str(file_row.get("downloadUrl") or ""),
            dependencies=dependency_ids,
        )

    def search(self, query: str = "", *, index: int = 0, page_size: int = CURSEFORGE_PAGE_SIZE) -> dict[str, Any]:
        game_id = self.resolve_palworld_game_id()
        params: dict[str, Any] = {
            "gameId": game_id,
            "index": max(0, int(index)),
            "pageSize": max(1, min(50, int(page_size))),
            # Popularity is the safest default for a general browser.
            "sortField": 2,
            "sortOrder": "desc",
        }
        if str(query or "").strip():
            params["searchFilter"] = str(query).strip()
        payload = self._get("/v1/mods/search", params=params) or {}
        items = [self._item_from_mod(row) for row in (payload.get("data") or []) if isinstance(row, dict)]
        return {
            "items": [item.to_dict() for item in items],
            "pagination": dict(payload.get("pagination") or {}),
            "game_id": game_id,
        }

    def get_mod(self, mod_id: int) -> dict[str, Any]:
        payload = self._get(f"/v1/mods/{int(mod_id)}") or {}
        row = payload.get("data") or {}
        if not isinstance(row, dict):
            raise ModCatalogError(f"CurseForge mod {mod_id} returned no metadata")
        return row

    def get_file(self, mod_id: int, file_id: int) -> dict[str, Any]:
        payload = self._get(f"/v1/mods/{int(mod_id)}/files/{int(file_id)}") or {}
        row = payload.get("data") or {}
        if not isinstance(row, dict):
            raise ModCatalogError(f"CurseForge file {file_id} returned no metadata")
        return row

    def latest_item(self, mod_id: int) -> CatalogItem:
        row = self.get_mod(mod_id)
        file_row = self._latest_file(row)
        if not file_row or not file_row.get("id"):
            files_payload = self._get(f"/v1/mods/{int(mod_id)}/files", params={"pageSize": 50, "index": 0}) or {}
            rows = [item for item in (files_payload.get("data") or []) if isinstance(item, dict)]
            if not rows:
                raise ModCatalogError(f"CurseForge mod {mod_id} has no downloadable files")
            releases = [item for item in rows if int(item.get("releaseType") or 0) == 1]
            pool = releases or rows
            pool.sort(key=lambda item: str(item.get("fileDate") or ""), reverse=True)
            file_row = pool[0]
        return self._item_from_mod(row, file_row)

    def download(self, item: dict[str, Any] | CatalogItem) -> tuple[bytes, dict[str, Any]]:
        data = item.to_dict() if isinstance(item, CatalogItem) else dict(item)
        mod_id = int(data.get("mod_id") or 0)
        file_id = int(data.get("file_id") or 0)
        if not mod_id or not file_id:
            raise ModCatalogError("The selected CurseForge entry is missing a project/file id")
        file_row = self.get_file(mod_id, file_id)
        url = str(file_row.get("downloadUrl") or data.get("download_url") or "")
        if not url:
            payload = self._get(f"/v1/mods/{mod_id}/files/{file_id}/download-url") or {}
            url = str(payload.get("data") or "")
        if not url:
            raise ModCatalogError("CurseForge did not provide a download URL for this file")
        try:
            response = self.session.get(url, timeout=120, headers={"User-Agent": "PalServer-Manager/ModCatalog"})
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ModCatalogError(f"Unable to download {data.get('name') or 'mod'} from CurseForge: {exc}") from exc
        content = response.content
        _verify_curseforge_hashes(content, file_row.get("hashes") or [])
        merged = dict(data)
        merged.update({
            "download_url": url,
            "file_name": str(file_row.get("fileName") or data.get("file_name") or "mod.zip"),
            "game_versions": [str(v) for v in (file_row.get("gameVersions") or data.get("game_versions") or [])],
            "dependencies": [
                int(dep.get("modId")) for dep in (file_row.get("dependencies") or [])
                if isinstance(dep, dict) and int(dep.get("relationType") or 0) == 3 and dep.get("modId")
            ],
        })
        return content, merged


def _verify_curseforge_hashes(content: bytes, hashes: Iterable[dict[str, Any]]) -> None:
    for row in hashes:
        if not isinstance(row, dict):
            continue
        expected = str(row.get("value") or "").strip().lower()
        algo = int(row.get("algo") or 0)
        if not expected:
            continue
        if algo == 1:
            actual = hashlib.sha1(content).hexdigest()
        elif algo == 2:
            actual = hashlib.md5(content).hexdigest()  # noqa: S324 - used only to verify upstream-provided checksum
        else:
            continue
        if actual.lower() != expected:
            raise ModCatalogError("Downloaded CurseForge file failed its published checksum verification")


def curated_search(query: str = "") -> dict[str, Any]:
    needle = str(query or "").strip().lower()
    rows = []
    for row in CURATED_LINUX_MODS:
        haystack = " ".join([
            str(row.get("name") or ""),
            str(row.get("summary") or ""),
            str(row.get("author") or ""),
            " ".join(row.get("categories") or []),
        ]).lower()
        if not needle or needle in haystack:
            rows.append(dict(row))
    return {"items": rows, "pagination": {"index": 0, "pageSize": len(rows), "resultCount": len(rows), "totalCount": len(rows)}}


def _forgecdn_url(file_id: int, file_name: str) -> str:
    text = str(int(file_id))
    if len(text) <= 3:
        first, last = "0", text.zfill(3)
    else:
        first, last = text[:-3], text[-3:]
    from urllib.parse import quote
    return f"https://mediafilez.forgecdn.net/files/{first}/{last}/{quote(file_name)}"


def download_curated(item: dict[str, Any], session: requests.Session | None = None) -> tuple[bytes, dict[str, Any]]:
    session = session or requests.Session()
    data = dict(item)
    url = str(data.get("download_url") or "")
    if not url and data.get("file_id") and data.get("file_name"):
        url = _forgecdn_url(int(data["file_id"]), str(data["file_name"]))
    if not url:
        raise ModCatalogError("This curated entry does not provide a downloadable archive")
    try:
        response = session.get(url, timeout=120, headers={"User-Agent": "PalServer-Manager/ModCatalog"})
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ModCatalogError(f"Unable to download {data.get('name') or 'mod'}: {exc}") from exc
    data["download_url"] = url
    return response.content, data


def _clean_archive_path(name: str) -> PurePosixPath:
    raw = str(name or "").replace("\\", "/").lstrip("/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts:
        raise ModCatalogError(f"Unsafe path in upstream mod archive: {name}")
    return path


def _normalized_parts(path: PurePosixPath) -> list[str]:
    out = []
    for part in path.parts:
        low = part.lower()
        if low == "scripts":
            out.append("scripts")
        elif low in {"libs", "dlls"}:
            out.append("libs" if low == "libs" else "dlls")
        else:
            out.append(part)
    return out


def inspect_linux_archive(archive_bytes: bytes, item: dict[str, Any]) -> dict[str, Any]:
    """Classify an upstream archive and map safe UE4SS files to Linux paths.

    V1 intentionally auto-installs only structures we can reason about safely:
    UE4SS Lua mod folders and native Linux .so libraries inside a mod folder.
    Windows DLL-only, Blueprint, executable, and ambiguous packages stay visible
    in the browser but are blocked from one-click installation.
    """
    if not archive_bytes:
        raise ModCatalogError("Downloaded mod archive is empty")
    try:
        zf = zipfile.ZipFile(io.BytesIO(archive_bytes))
    except zipfile.BadZipFile as exc:
        raise ModCatalogError("Downloaded mod is not a ZIP archive") from exc

    mapped: list[tuple[str, str]] = []
    windows_dlls: list[str] = []
    linux_libs: list[str] = []
    lua_files: list[str] = []
    blocked_execs: list[str] = []
    mod_names: list[str] = []

    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            path = _clean_archive_path(info.filename)
            parts = list(path.parts)
            lowers = [part.lower() for part in parts]
            suffix = PurePosixPath(parts[-1]).suffix.lower()
            if suffix in {".exe", ".bat", ".cmd", ".ps1"}:
                blocked_execs.append(path.as_posix())
                continue
            if suffix == ".dll":
                windows_dlls.append(path.as_posix())
            if suffix == ".so":
                linux_libs.append(path.as_posix())
            if suffix == ".lua":
                lua_files.append(path.as_posix())

            # Locate a UE4SS Mods segment no matter whether the author packed
            # Win64/ue4ss/Mods, Win64/Mods, or just Mods/ModName.
            mods_index = next((i for i, value in enumerate(lowers) if value == "mods" and i + 1 < len(parts)), None)
            rel_parts: list[str] | None = None
            if mods_index is not None:
                rel_parts = parts[mods_index + 1:]
            else:
                scripts_index = next((i for i, value in enumerate(lowers) if value == "scripts"), None)
                libs_index = next((i for i, value in enumerate(lowers) if value in {"libs", "dlls"}), None)
                marker = scripts_index if scripts_index is not None else libs_index
                if marker is not None:
                    if marker > 0:
                        rel_parts = parts[marker - 1:]
                    else:
                        folder = re.sub(r"[^A-Za-z0-9_.-]+", "", str(item.get("slug") or item.get("name") or "CatalogMod")) or "CatalogMod"
                        rel_parts = [folder, *parts]
            if not rel_parts or len(rel_parts) < 2:
                continue
            normalized = _normalized_parts(PurePosixPath(*rel_parts))
            # A Windows dlls folder is never remapped to Linux. A package may
            # include both dlls and a Linux libs folder; only the latter is used.
            if "dlls" in [part.lower() for part in normalized] or suffix == ".dll":
                continue
            if normalized[0] not in mod_names:
                mod_names.append(normalized[0])
            target = PurePosixPath("Pal", "Binaries", "Linux", "Mods", *normalized).as_posix()
            mapped.append((path.as_posix(), target))

    compatibility = str(item.get("compatibility") or "untested")
    reason = str(item.get("compatibility_detail") or "")
    installable = bool(mapped) and (bool(lua_files) or bool(linux_libs)) and not blocked_execs
    if windows_dlls and not linux_libs and not lua_files:
        compatibility = "windows-only"
        reason = "Archive contains Windows DLLs and no portable Lua/native Linux .so implementation."
        installable = False
    elif blocked_execs:
        compatibility = "unsupported"
        reason = "Archive contains executable/script installers; PalServer Manager will not execute third-party installers automatically."
        installable = False
    elif linux_libs:
        if compatibility not in {"verified", "compatible"}:
            compatibility = "compatible"
        reason = reason or "Archive contains a native Linux .so UE4SS component."
    elif lua_files and mapped:
        if compatibility not in {"verified", "compatible"}:
            compatibility = "compatible"
        reason = reason or "Archive contains UE4SS Lua files with a recognizable Mods/<name>/scripts layout."
    elif not mapped:
        reason = "No recognizable UE4SS Mods/<name>/scripts or native Linux libs layout was found."
        installable = False

    return {
        "installable": installable,
        "compatibility": compatibility,
        "detail": reason,
        "mapped": mapped,
        "mod_names": mod_names,
        "lua_files": lua_files,
        "linux_libs": linux_libs,
        "windows_dlls": windows_dlls,
        "blocked_executables": blocked_execs,
    }


def build_managed_package(archive_bytes: bytes, item: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    scan = inspect_linux_archive(archive_bytes, item)
    if not scan.get("installable"):
        raise ModCatalogError(scan.get("detail") or "This mod cannot be safely installed on a native Linux Palworld server")
    source_zip = zipfile.ZipFile(io.BytesIO(archive_bytes))
    out_buffer = io.BytesIO()
    mod_names = list(scan.get("mod_names") or [])
    if not mod_names:
        raise ModCatalogError("Unable to determine the UE4SS mod folder name from the upstream archive")
    manifest = {
        "id": f"{str(item.get('source') or 'catalog')}-{item.get('mod_id') or item.get('slug') or mod_names[0]}",
        "name": str(item.get("name") or mod_names[0]),
        "version": str(item.get("version") or "latest"),
        "runtime": "ue4ss",
        "type": "catalog",
        "server_required": True,
        "client_required": bool(item.get("client_required", False)),
        "ue4ss_mod_name": mod_names[0],
        "ue4ss_mod_names": mod_names,
        "compatibility": {
            "linux": str(scan.get("compatibility") or item.get("compatibility") or "compatible"),
            "palworld": ", ".join(str(v) for v in (item.get("game_versions") or [])) or "unspecified",
            "source": str(item.get("source") or "catalog"),
        },
        "source": {
            "provider": str(item.get("source") or "catalog"),
            "mod_id": item.get("mod_id"),
            "file_id": item.get("file_id"),
            "project_url": str(item.get("project_url") or ""),
            "download_url": str(item.get("download_url") or ""),
        },
    }
    mapped = {src: target for src, target in scan.get("mapped") or []}
    with source_zip, zipfile.ZipFile(out_buffer, "w", compression=zipfile.ZIP_DEFLATED) as out:
        out.writestr("palserver-mod.json", json.dumps(manifest, indent=2))
        for info in source_zip.infolist():
            if info.is_dir():
                continue
            source_name = _clean_archive_path(info.filename).as_posix()
            target = mapped.get(source_name)
            if not target:
                continue
            out.writestr(f"server/{target}", source_zip.read(info))
    return out_buffer.getvalue(), {"manifest": manifest, "scan": scan}


class ModCatalogService:
    """Desktop-side catalog facade. No provider credential is sent to agents."""

    def __init__(self, api_key: str = "", game_id: int = 0, session: requests.Session | None = None):
        self.api_key = str(api_key or "").strip()
        self.game_id = int(game_id or 0)
        self.session = session or requests.Session()

    def search(self, provider: str, query: str = "", *, index: int = 0, page_size: int = CURSEFORGE_PAGE_SIZE) -> dict[str, Any]:
        provider = str(provider or "curated").strip().lower()
        if provider == "curated":
            return curated_search(query)
        if provider == "curseforge":
            client = CurseForgeClient(self.api_key, self.game_id, self.session)
            result = client.search(query, index=index, page_size=page_size)
            self.game_id = client.game_id
            return result
        raise ModCatalogError(f"Unknown mod catalog provider: {provider}")

    def download(self, item: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
        source = str(item.get("source") or "").lower()
        if source == "curseforge" and self.api_key:
            client = CurseForgeClient(self.api_key, self.game_id, self.session)
            return client.download(item)
        if source == "curseforge":
            return download_curated(item, self.session)
        raise ModCatalogError(f"Automatic downloads are not implemented for source '{source}'")

    def install_plan(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        """Resolve required CurseForge dependencies in dependency-first order."""
        if str(item.get("source") or "").lower() != "curseforge" or not self.api_key:
            return [dict(item)]
        client = CurseForgeClient(self.api_key, self.game_id, self.session)
        resolved: list[dict[str, Any]] = []
        seen: set[int] = set()

        def visit(row: dict[str, Any]) -> None:
            mod_id = int(row.get("mod_id") or 0)
            if mod_id in seen:
                return
            seen.add(mod_id)
            dependencies = list(row.get("dependencies") or [])
            if not dependencies and row.get("file_id"):
                try:
                    file_row = client.get_file(mod_id, int(row["file_id"]))
                    dependencies = [
                        int(dep.get("modId")) for dep in (file_row.get("dependencies") or [])
                        if isinstance(dep, dict) and int(dep.get("relationType") or 0) == 3 and dep.get("modId")
                    ]
                except Exception:
                    dependencies = []
            for dependency_id in dependencies:
                dep_item = client.latest_item(int(dependency_id)).to_dict()
                # A catalog UE4SS runtime package is redundant because the
                # manager owns the runtime lifecycle itself.
                if "ue4ss" in str(dep_item.get("name") or "").lower() and not dep_item.get("server_required"):
                    continue
                visit(dep_item)
            resolved.append(dict(row))

        visit(dict(item))
        self.game_id = client.game_id
        return resolved
