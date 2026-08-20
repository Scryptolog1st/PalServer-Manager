from palserver_manager.remote import RemoteManager


def test_remote_install_job_endpoints(monkeypatch):
    remote = RemoteManager.__new__(RemoteManager)
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path.endswith('/start'):
            return {'job_id': 'abc123', 'status': 'running'}
        return {'job_id': 'abc123', 'status': 'running', 'lines': ['hello'], 'next_offset': 1}

    remote._request = fake_request
    started = remote.host_install_palworld_start({'name': 'Test'})
    snapshot = remote.host_install_palworld_job('abc123', 7)

    assert started['job_id'] == 'abc123'
    assert snapshot['lines'] == ['hello']
    assert calls[0][0:2] == ('POST', '/host/install-palworld/start')
    assert calls[1][0:2] == ('GET', '/host/install-palworld/jobs/abc123')
    assert calls[1][2]['params']['offset'] == 7
