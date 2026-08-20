from types import SimpleNamespace

import pytest

from palserver_manager.remote import RemoteError, RemoteManager


def _remote():
    remote = RemoteManager.__new__(RemoteManager)
    remote.cfg = SimpleNamespace(connection=SimpleNamespace(ssh_host="192.168.10.31"))
    return remote


def test_mod_request_translates_legacy_agent_404(monkeypatch):
    remote = _remote()

    def fail(*_args, **_kwargs):
        raise RemoteError("Remote manager error 404: Not Found")

    monkeypatch.setattr(remote, "_request", fail)
    monkeypatch.setattr(remote, "host_info", lambda: {"hostname": "palworld-node-02", "version": "0.4.9"})

    with pytest.raises(RemoteError) as exc:
        remote.mods_status()

    message = str(exc.value)
    assert "palworld-node-02" in message
    assert "0.4.9" in message
    assert "0.6.0 or newer" in message


def test_mod_request_preserves_non_404_errors(monkeypatch):
    remote = _remote()

    def fail(*_args, **_kwargs):
        raise RemoteError("Remote manager error 500: broken")

    monkeypatch.setattr(remote, "_request", fail)

    with pytest.raises(RemoteError, match="500: broken"):
        remote.mods_status()
