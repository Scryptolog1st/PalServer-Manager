import io
import json
import zipfile

import pytest

from palserver_manager.mod_catalog import (
    ModCatalogError,
    build_managed_package,
    curated_search,
    inspect_linux_archive,
)


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_curated_catalog_has_verified_linux_server_mod():
    result = curated_search("admin")
    assert result["items"]
    row = result["items"][0]
    assert row["name"] == "Admin Commands"
    assert row["compatibility"] == "verified"
    assert row["server_required"] is True
    assert row["client_required"] is False


def test_linux_lua_archive_is_mapped_and_scripts_case_is_normalized():
    raw = make_zip({
        "AdminCommands/Scripts/main.lua": "print('ok')",
        "AdminCommands/Scripts/config.lua": "return {}",
        "README.md": "docs",
    })
    item = {
        "source": "curseforge",
        "mod_id": 1328795,
        "name": "Admin Commands",
        "slug": "admin-commands",
        "version": "1.0.1",
        "compatibility": "verified",
        "server_required": True,
        "client_required": False,
    }
    scan = inspect_linux_archive(raw, item)
    assert scan["installable"] is True
    assert scan["mod_names"] == ["AdminCommands"]
    targets = {target for _, target in scan["mapped"]}
    assert "Pal/Binaries/Linux/Mods/AdminCommands/scripts/main.lua" in targets
    assert not any("README" in target for target in targets)

    package, metadata = build_managed_package(raw, item)
    assert metadata["scan"]["installable"] is True
    with zipfile.ZipFile(io.BytesIO(package)) as zf:
        manifest = json.loads(zf.read("palserver-mod.json"))
        assert manifest["id"] == "curseforge-1328795"
        assert manifest["ue4ss_mod_names"] == ["AdminCommands"]
        assert "server/Pal/Binaries/Linux/Mods/AdminCommands/scripts/main.lua" in zf.namelist()


def test_windows_dll_only_mod_is_rejected():
    raw = make_zip({"SomeMod/dlls/SomeMod.dll": b"MZ"})
    item = {"source": "curseforge", "mod_id": 44, "name": "Windows Thing", "server_required": True}
    scan = inspect_linux_archive(raw, item)
    assert scan["installable"] is False
    assert scan["compatibility"] == "windows-only"
    with pytest.raises(ModCatalogError):
        build_managed_package(raw, item)


def test_archive_with_traversal_is_rejected():
    raw = make_zip({"../escape/Scripts/main.lua": "print('no')"})
    with pytest.raises(ModCatalogError):
        inspect_linux_archive(raw, {"name": "Unsafe", "server_required": True})


def test_native_linux_so_can_be_mapped():
    raw = make_zip({"PalChatBridge/libs/PalChatBridge.so": b"\x7fELF"})
    scan = inspect_linux_archive(raw, {"name": "PalChatBridge", "server_required": True})
    assert scan["installable"] is True
    assert scan["linux_libs"]
    assert scan["mapped"][0][1] == "Pal/Binaries/Linux/Mods/PalChatBridge/libs/PalChatBridge.so"

class _FakeResponse:
    def __init__(self, payload, status=200, content=b""):
        self._payload = payload
        self.status_code = status
        self.content = content
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        if url.endswith("/v1/games"):
            return _FakeResponse({
                "data": [{"id": 9876, "name": "Palworld", "slug": "palworld"}],
                "pagination": {"index": 0, "pageSize": 50, "resultCount": 1, "totalCount": 1},
            })
        if url.endswith("/v1/mods/search"):
            return _FakeResponse({
                "data": [{
                    "id": 222,
                    "name": "Server Lua Candidate",
                    "slug": "server-lua-candidate",
                    "summary": "Dedicated server helper",
                    "downloadCount": 123,
                    "dateModified": "2026-08-20T00:00:00Z",
                    "authors": [{"name": "Tester"}],
                    "categories": [{"name": "Lua Code Mods"}, {"name": "Server Mods"}],
                    "links": {"websiteUrl": "https://example.invalid/mod"},
                    "latestFiles": [{
                        "id": 333,
                        "displayName": "1.0.0",
                        "fileName": "candidate.zip",
                        "fileDate": "2026-08-20T00:00:00Z",
                        "releaseType": 1,
                        "gameVersions": ["1.0.1"],
                        "dependencies": [],
                    }],
                }],
                "pagination": {"index": 0, "pageSize": 20, "resultCount": 1, "totalCount": 1},
            })
        raise AssertionError(f"Unexpected URL {url}")


def test_curseforge_search_resolves_palworld_and_marks_server_lua_candidate():
    from palserver_manager.mod_catalog import CurseForgeClient

    session = _FakeSession()
    client = CurseForgeClient("test-key", session=session)
    result = client.search("server")
    assert client.game_id == 9876
    assert result["items"][0]["name"] == "Server Lua Candidate"
    assert result["items"][0]["compatibility"] == "compatible"
    assert result["items"][0]["server_required"] is True
    assert any(call[2].get("x-api-key") == "test-key" for call in session.calls)


def test_curseforge_requires_user_api_key():
    from palserver_manager.mod_catalog import CurseForgeClient

    with pytest.raises(ModCatalogError, match="API key"):
        CurseForgeClient("").resolve_palworld_game_id()
