import json

from fastapi.testclient import TestClient

from palserver_manager.config import load_config


def test_agent_token_required(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("PALSERVER_MANAGER_CONFIG", str(path))
    from palserver_manager.agent import create_app
    app = create_app()
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    assert client.get("/v1/tools").status_code == 401
    cfg = load_config(path)
    response = client.get("/v1/tools", headers={"X-PalManager-Token": cfg.agent.token})
    assert response.status_code == 200
    assert len(response.json()) > 8
